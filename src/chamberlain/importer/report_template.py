"""The old report template becomes the owner's private report layout.

The old harness kept one prose template (`templates.report`) in the profile or the private
customisations. The new harness fills the mechanical sections itself and tells the assistant to
write only the rest, so the prose is turned into a layout (`chamberlain.runs.template`): each
heading becomes a section, the red and orange callouts keep their style and label, and the
wording under a heading the harness fills is kept as a description. Wording that is for the
writer before the first heading is kept as notes. Wording in a section the harness fills that
tells the writer what to put in the written report moves to the first section the assistant writes.

The result is `config/report-template.yaml` in the data root, written only when there is none
(like the connection profile), and never into Git. A line that uses a placeholder the new harness
cannot fill (the weekly roundup) is dropped and named in the import report.
"""

import re
from pathlib import Path
from typing import Any

import yaml

from chamberlain.importer.old import OldInstallation
from chamberlain.importer.report import ImportReport
from chamberlain.runs.template import (
    HARNESS_SLOTS,
    ReportTemplate,
    Slot,
    parse_template,
    private_path,
)

_CALLOUT = re.compile(r"^\*\*\s*(?P<mark>\U0001F7E5|\U0001F7E7)\s*(?P<text>.+?)\s*\*\*\s*$")
_HEADING = re.compile(r"^##\s+(?P<text>.+?)\s*$")
_TITLE = re.compile(r"^#\s+(?P<text>.+?)\s*$")
_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z0-9_.]+)\s*\}\}")
_FOR_THE_WRITER = re.compile(r"written report|in the report", re.IGNORECASE)
_STYLE = {"\U0001f7e5": "red", "\U0001f7e7": "orange"}
_IDS = (
    ("access problem", "access_problems"),
    ("important appointment", "important_appointments"),
    ("permission review", "permission_reviews"),
    ("verification", "verification"),
)


def _old_text(old: OldInstallation) -> str | None:
    for layer in (old.customisations, old.profile):
        templates: Any = layer.get("templates")
        if isinstance(templates, dict):
            text: Any = templates.get("report")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            if isinstance(text, str) and text.strip():
                return text
    return None


def _slug(text: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "_", text.casefold()).strip("_")
    return (cleaned or "section")[:40]


def _slot_id(heading: str, taken: set[str]) -> tuple[str, bool]:
    """`(id, written by the harness)`. A heading the harness does not know is the assistant's."""
    folded = heading.casefold()
    for words, name in _IDS:
        if words in folded and name not in taken:
            return name, True
    base = _slug(heading)
    name, number = base, 2
    while name in taken or name in HARNESS_SLOTS:
        name = f"{base}_{number}"[:40]
        number += 1
    return name, False


def _clean(lines: list[str], zone: str, dropped: list[str]) -> list[str]:
    """Fill `{{timezone}}`; drop a line that needs a value the new harness does not have."""
    kept: list[str] = []
    for line in lines:
        names = _PLACEHOLDER.findall(line)
        if any(name != "timezone" for name in names):
            dropped.append(", ".join(sorted({n for n in names if n != "timezone"})))
            continue
        kept.append(_PLACEHOLDER.sub(zone, line))
    return kept


def _paragraphs(text: str) -> list[str]:
    return [p.strip("\n") for p in re.split(r"\n\s*\n", text) if p.strip()]


def convert(text: str, zone: str, dropped: list[str]) -> ReportTemplate:
    """The layout for the old prose template `text`."""
    lines = _clean(text.strip().splitlines(), zone, dropped)
    title = "Mail check {date}"
    notes: list[str] = []
    sections: list[tuple[str, str, str, list[str]]] = []  # heading, style, mark, body lines
    for line in lines:
        stripped = line.strip()
        callout, heading, head = (
            _CALLOUT.match(stripped),
            _HEADING.match(stripped),
            _TITLE.match(stripped),
        )
        if callout:
            mark = callout.group("mark")
            sections.append((callout.group("text"), _STYLE[mark], mark, []))
        elif heading:
            sections.append((heading.group("text"), "plain", "", []))
        elif head and not sections:
            title = re.sub(r"\bDD/MM/YYYY\b", "{date}", head.group("text"))
        elif sections:
            sections[-1][3].append(line)
        else:
            notes.append(line)
    taken: set[str] = set()
    built: list[tuple[Slot, list[str]]] = []
    for heading, style, mark, body in sections:
        name, by_harness = _slot_id(heading, taken)
        taken.add(name)
        slot = Slot(
            id=name,
            by="harness" if by_harness else "agent",
            heading=heading,
            style=style,  # type: ignore[arg-type]
            mark=mark,
            instructions="\n".join(body).strip(),
        )
        built.append((slot, body))
    first_agent = next((i for i, (slot, _) in enumerate(built) if slot.by == "agent"), None)
    if first_agent is None:
        agent_id, _ = _slot_id("Email and tasks", taken)
        built.append((Slot(id=agent_id, by="agent", heading="Email and tasks"), []))
        first_agent = len(built) - 1
    moved: list[str] = []
    final: list[Slot] = []
    for slot, body in built:
        if slot.by == "harness":
            keep: list[str] = []
            for paragraph in _paragraphs("\n".join(body)):
                (moved if _FOR_THE_WRITER.search(paragraph) else keep).append(paragraph)
            slot = slot.model_copy(update={"instructions": "\n\n".join(keep)})
        final.append(slot)
    if moved:
        agent = final[first_agent]
        joined = "\n\n".join([p for p in (agent.instructions, *moved) if p])
        final[first_agent] = agent.model_copy(update={"instructions": joined})
    return ReportTemplate(title=title, notes="\n".join(notes).strip(), slots=tuple(final))


def write_template(old: OldInstallation, data_root: Path, zone: str, report: ImportReport) -> None:
    """Carry the owner's report template into the private config, once."""
    text = _old_text(old)
    if text is None:
        return
    dropped: list[str] = []
    template = convert(text, zone, dropped)
    document = yaml.safe_dump(  # pyright: ignore[reportUnknownMemberType]
        template.model_dump(), allow_unicode=True, sort_keys=False
    )
    parse_template(document)  # what is written must load back
    path = private_path(data_root)
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        pending = path.with_suffix(".tmp")
        pending.write_text(document, encoding="utf-8")
        pending.replace(path)
        report.report_template = "created"
    elif path.read_text(encoding="utf-8-sig") == document:
        report.report_template = "already identical"
    else:
        report.report_template = "kept existing (differs from the old template)"
        report.warnings.append("config/report-template.yaml already exists and was not changed.")
    for names in sorted(set(dropped)):
        report.warnings.append(
            f"report template: a line that used {{{{{names}}}}} was left out (the new harness has "
            "no value for it)."
        )
    templates: Any = report.unmapped_settings.get("templates")
    if isinstance(templates, dict):
        rest = {k: v for k, v in templates.items() if k != "report"}  # pyright: ignore[reportUnknownVariableType]
        if rest:
            report.unmapped_settings["templates"] = rest
        else:
            del report.unmapped_settings["templates"]
