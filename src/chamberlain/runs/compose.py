"""A pass report as the owner reads it: the layout, the harness sections and the assistant's text.

Contract: docs/contracts/06-memory.md (the report section). Three parts meet here.

- The layout (`template.py`): which sections, in what order, who writes each.
- The harness sections, taken when the pass settles and kept in `run_reports` (35 days, with the
  run). They are facts as of that moment, so a later change of state cannot rewrite history.
- The assistant's text, which stays a stored item owned by the run (30 days unless it asks for
  less, hash-checked on every read). It is never copied into the database, so a retention rule
  for stored items holds for it too.

`outline` is what the assistant is told to write: a small stored item the harness keeps under the
running pass, naming the sections that are its own with the owner's wording for each. The
callouts (red access problem, orange important appointment) come from the layout's `style`, never
from the assistant's text, so nothing the assistant or an email says can pose as one.
"""

from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import Field

from chamberlain.app.context import Context
from chamberlain.domain.artifacts import REPORT_OUTLINE_FILENAME, ArtifactKind
from chamberlain.memory.artifacts import ArtifactService
from chamberlain.runs import report_repo as repo
from chamberlain.runs import sections
from chamberlain.runs.models import RunRow
from chamberlain.runs.repo import get_run, outline_handle, schedules
from chamberlain.runs.reports import read_reports
from chamberlain.runs.template import ReportTemplate, Slot, load_template
from chamberlain.schema.base import Model
from chamberlain.schema.errors import ChamberlainError
from chamberlain.store.db import transaction
from chamberlain.store.migrate import stamp

OUTLINE_HOURS = 72


class Block(Model):
    id: str
    by: Literal["harness", "agent"]
    heading: str
    style: Literal["plain", "red", "orange"] = "plain"
    mark: str = ""
    markdown: str | None = None
    """The harness's text. None for a section the assistant writes (read from its stored item)."""


class ComposedReport(Model):
    version: Literal[1] = 1
    title: str
    zone: str
    blocks: tuple[Block, ...] = Field(default=())
    note: str = ""


def zone_of(ctx: Context, run: RunRow) -> ZoneInfo:
    for schedule in schedules(ctx.db):
        if schedule.name == run.schedule:
            return ZoneInfo(schedule.zone)
    return ZoneInfo("UTC")


def _title(template: ReportTemplate, run: RunRow, zone: ZoneInfo) -> str:
    day = datetime.strptime(run.day, "%Y-%m-%d")  # noqa: DTZ007 (a date only)
    return (
        template.title.replace("{date}", day.strftime("%d/%m/%Y"))
        .replace("{timezone}", zone.key)
        .replace("{schedule}", run.schedule)
    )


def snapshot(ctx: Context, run: RunRow) -> ComposedReport:
    """The report's structure with the harness sections filled in as of now."""
    template, problem = load_template(ctx.data_root)
    zone = zone_of(ctx, run)
    facts = sections.Facts(ctx, run, zone)
    written = {name: build(facts) for name, build in sections.BUILDERS.items()}
    if problem and "verification" in written:
        written["verification"] += f"\n\n{problem}"
    blocks: list[Block] = []
    for slot in template.slots:
        text = written.get(slot.id, "") if slot.by == "harness" else None
        if slot.by == "harness" and not (text or "").strip():
            continue
        blocks.append(
            Block(
                id=slot.id,
                by=slot.by,
                heading=slot.heading,
                style=slot.style,
                mark=slot.mark,
                markdown=text,
            )
        )
    return ComposedReport(title=_title(template, run, zone), zone=zone.key, blocks=tuple(blocks))


def settle_snapshot(ctx: Context, run: RunRow) -> None:
    """Keep the snapshot of a pass that has just ended. A second call keeps the first."""
    report = snapshot(ctx, run)
    with transaction(ctx.db):
        repo.put_run_report(ctx.db, run.handle, stamp(ctx.clock), report.model_dump_json())


def stored_snapshot(ctx: Context, run: str) -> ComposedReport | None:
    raw = repo.get_run_report(ctx.db, run)
    return None if raw is None else ComposedReport.model_validate_json(raw)


# --- reading -----------------------------------------------------------------------------------


def _split_agent_text(text: str, slots: list[Slot]) -> dict[str, str]:
    """The assistant's text by section. With one section of its own, all of it is that section's;
    with several, text under a `## Heading` that matches one goes to it, the rest to the first."""
    if len(slots) == 1:
        return {slots[0].id: text.strip()}
    by_heading = {slot.heading.casefold(): slot.id for slot in slots}
    found: dict[str, list[str]] = {slots[0].id: []}
    current = slots[0].id
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            name = stripped.lstrip("#").strip().strip("*").strip().casefold()
            if name in by_heading:
                current = by_heading[name]
                found.setdefault(current, [])
                continue
        found.setdefault(current, []).append(line)
    return {key: "\n".join(lines).strip() for key, lines in found.items()}


def read_composed(ctx: Context, run: RunRow) -> dict[str, object] | None:
    """The report ready to draw: blocks with their markdown, or None when the run has no report.

    A run without a snapshot (it finished before reports had one) shows the assistant's text
    alone. A run whose stored text has expired shows the harness sections and says so.
    """
    parts = read_reports(ctx, run.handle)
    composed = stored_snapshot(ctx, run.handle)
    if composed is None and not parts:
        return None
    text = "\n\n".join(p.text for p in parts).strip()
    blocks: list[dict[str, Any]] = []
    note = "" if composed is None else composed.note
    if composed is None:
        blocks.append(
            {
                "id": "report",
                "by": "agent",
                "heading": "",
                "style": "plain",
                "mark": "",
                "markdown": text,
            }
        )
        title = f"{run.schedule} {run.day}"
    else:
        title = composed.title
        agent_slots = [
            Slot(id=b.id, by="agent", heading=b.heading) for b in composed.blocks if b.by == "agent"
        ]
        mine = _split_agent_text(text, agent_slots) if agent_slots and text else {}
        for block in composed.blocks:
            body = block.markdown if block.by == "harness" else mine.get(block.id, "")
            blocks.append(block.model_dump() | {"markdown": body})
        if not text:
            note = "The assistant's written part of this report has expired."
    return {
        "run": run.handle,
        "schedule": run.schedule,
        "day": run.day,
        "state": run.state.value,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "title": title,
        "zone": None if composed is None else composed.zone,
        "blocks": blocks,
        "note": note,
        "partial": run.state.value == "incomplete",
    }


# --- the outline the assistant reads ------------------------------------------------------------


def outline_text(template: ReportTemplate, run: RunRow) -> str:
    mine = [slot for slot in template.slots if slot.by == "agent"]
    ours = [slot.heading for slot in template.slots if slot.by == "harness"]
    lines = [
        f"Report for the {run.schedule} pass of {run.day}.",
        f"Write only the sections below, as plain markdown, and keep the text under {run.handle}.",
        "Say plainly what you could not cover. The owner reads this first, so lead with what "
        "they must do.",
        "",
    ]
    for slot in mine:
        lines += [
            f"## {slot.heading}",
            slot.instructions.strip() or "Write what the owner needs.",
            "",
        ]
    if len(mine) > 1:
        lines.append("Start each section with its heading as a markdown heading.")
    if ours:
        lines.append(
            "The harness writes these itself from its records, so do not repeat them: "
            + "; ".join(ours)
            + "."
        )
    return "\n".join(lines).strip() + "\n"


def ensure_outline(ctx: Context, run: RunRow) -> str | None:
    """Keep the outline under a running pass once. Returns its handle."""
    existing = outline_handle(ctx.db, run.handle)
    if existing is not None:
        return existing
    template, _ = load_template(ctx.data_root)
    try:
        kept = ArtifactService(ctx.db, ctx.clock, ctx.data_root).keep_bytes(
            data=outline_text(template, run).encode("utf-8"),
            kind=ArtifactKind.TEXT,
            owner=run.handle,
            reason=f"What to write in the report of the {run.schedule} pass",
            filename=REPORT_OUTLINE_FILENAME,
            media_type="text/plain",
            expires_in_hours=OUTLINE_HOURS,
        )
    except ChamberlainError:
        return None
    return kept.handle


def find_run(ctx: Context, handle: str) -> RunRow | None:
    return get_run(ctx.db, handle)
