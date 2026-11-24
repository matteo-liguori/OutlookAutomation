"""A run's written output, kept as an artifact the run owns, with a lifetime.

Contract: docs/contracts/06-memory.md (M-4: a run owns artifacts; M-10: the hold ends with
the run row, or the artifact's own `expires_at` ends it first). No report is a loose file:
the bytes live in the content-addressed store, are hash-checked on every read, and the
sweeper deletes them. The scheduled agent keeps its own report with `keep(owner=run_...)`;
the harness stores one only when a run ended without any.
"""

from dataclasses import dataclass

from chamberlain.app.context import Context
from chamberlain.domain.artifacts import ArtifactKind
from chamberlain.memory.artifacts import ArtifactService
from chamberlain.runs import repo
from chamberlain.runs.tuning import REPORT_HOURS
from chamberlain.schema.errors import ChamberlainError

REPORT_MAX_BYTES = 20_000


@dataclass(frozen=True)
class Report:
    handle: str
    text: str
    media_type: str
    filename: str | None


def store_report(ctx: Context, run: str, text: str) -> str | None:
    """Keep `text` as the run's report unless it already has one. Returns the artifact handle."""
    existing = repo.report_handles(ctx.db, run)
    if existing:
        return existing[0]
    row = repo.get_run(ctx.db, run)
    if row is None:
        return None
    data = text.encode("utf-8")[:REPORT_MAX_BYTES]
    kept = ArtifactService(ctx.db, ctx.clock, ctx.data_root).keep_bytes(
        data=data,
        kind=ArtifactKind.TEXT,
        owner=run,
        reason=f"Report of the {row.schedule} pass for {row.day}",
        filename=f"report-{row.schedule}-{row.day}.txt",
        media_type="text/plain",
        expires_in_hours=REPORT_HOURS,
    )
    return kept.handle


def read_reports(ctx: Context, run: str) -> list[Report]:
    """The run's readable reports, oldest first. An expired or damaged one is left out."""
    found: list[Report] = []
    for handle in repo.report_handles(ctx.db, run):
        try:
            info = ctx.services.artifacts.info(handle)
            data = ctx.services.artifacts.read_bytes(handle)
        except ChamberlainError:
            continue
        found.append(
            Report(
                handle=handle,
                text=data.decode("utf-8", errors="replace"),
                media_type=info.media_type,
                filename=info.filename,
            )
        )
    return found
