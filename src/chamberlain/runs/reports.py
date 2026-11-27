"""A run's written output, kept as an artifact the run owns, with a lifetime.

Contract: docs/contracts/06-memory.md (M-4: a run owns artifacts; M-10: the hold ends with
the run row, or the artifact's own `expires_at` ends it first). No report is a loose file:
the bytes live in the content-addressed store, are hash-checked on every read, and the
sweeper deletes them. The scheduled agent keeps its own report with `keep(owner=run_...)`;
the harness stores one only when a run ended without any.

The newest text wins (`report_keep`): a report kept under a run releases the run's earlier ones,
so the sweeper removes them. A pass being worked and the live report (the newest settled pass's,
until the next starts) take new text from any session; an older or frozen report refuses it.
"""

from dataclasses import dataclass

from chamberlain.app.context import Context, Handler, Result
from chamberlain.domain.artifacts import REPORT_OUTLINE_FILENAME, ArtifactKind
from chamberlain.domain.handles import HandleKind, handle_kind
from chamberlain.runs import repo
from chamberlain.runs.models import RunState
from chamberlain.runs.tuning import REPORT_HOURS
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.schema.tools import KeepKind, KeepRequest, KeepResponse

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
    kept = ctx.services.artifacts.keep_bytes(
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


def _refuse_closed_report(ctx: Context, run: str) -> None:
    """A report takes new text while its pass is due or running, or while it is the live one."""
    row = repo.get_run(ctx.db, run)
    if row is None or row.state in (RunState.DUE, RunState.RUNNING):
        return  # an unknown run is refused by the store itself
    live = repo.live_run(ctx.db)
    if live is not None and live.handle == run:
        return
    if live is not None:
        fix = f"Change the live report instead: keep the new text under {live.handle}."
    else:
        fix = "No report is live while the next pass is worked; that pass writes its own."
    raise ChamberlainError(
        ErrorCode.ARTIFACT_OWNER_ENDED,
        "This report is frozen: a later pass has started, so its text can no longer change.",
        fix=fix,
        field="owner",
    )


def report_keep(inner: Handler) -> Handler:
    """Wrap the `keep` handler: a report kept under a run replaces the run's earlier ones."""

    def keep(ctx: Context, request: KeepRequest) -> Result:
        is_report = (
            request.kind is KeepKind.ARTIFACT
            and handle_kind(request.owner) is HandleKind.RUN
            and request.filename != REPORT_OUTLINE_FILENAME
        )
        if not is_report:
            return inner(ctx, request)
        _refuse_closed_report(ctx, request.owner)
        earlier = repo.report_handles(ctx.db, request.owner)
        result = inner(ctx, request)
        kept = result.data.handle if isinstance(result.data, KeepResponse) else None
        for handle in earlier:
            if handle != kept:
                ctx.services.artifacts.release(handle, request.owner)
        return result

    return keep
