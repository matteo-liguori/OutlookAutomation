"""What the viewer shows, as plain JSON-ready dicts built from the store and the services.

Contract: docs/contracts/06-memory.md (read-only views), 10-targets.md (storage figures),
04-writes.md (uncertain operations, W-41). Operation text is available in the
read-only history.
"""

import sqlite3
from collections import Counter
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from chamberlain import __version__
from chamberlain.app.context import Context
from chamberlain.app.metrics import collect_metrics
from chamberlain.attention import repo as attention_repo
from chamberlain.domain.grants import GrantState
from chamberlain.domain.operations import OperationState, recheck_text
from chamberlain.domain.timefmt import ts
from chamberlain.memory import repo as memory_repo
from chamberlain.runs import repo as runs_repo
from chamberlain.runs.reports import read_reports
from chamberlain.runs.tuning import NOTE_KEEP
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.store.migrate import stamp
from chamberlain.writes import repo as write_repo
from chamberlain.writes import repo as writes_repo
from chamberlain.writes.models import OperationRow
from chamberlain.writes.render import describe_intent

LIST_LIMIT = 50
ACTIVITY_LIMIT = 200
DUE_WINDOW = timedelta(days=7)


def pick(row: object, *names: str) -> dict[str, Any]:
    """The named attributes of a row, as a JSON-ready dict."""
    return {name: getattr(row, name) for name in names}


def overview(ctx: Context) -> dict[str, Any]:
    """Counts the person looks at first, and what needs them."""
    db = ctx.db
    grants = write_repo.grants_in_states(db, list(GrantState))
    waiting = [g for g in grants if g.state is GrantState.PROPOSED]
    now = stamp(ctx.clock)
    review = [
        g
        for g in grants
        if g.state in (GrantState.ACTIVE, GrantState.SUSPENDED)
        and g.review_at is not None
        and ts(g.review_at) <= now
    ]
    return {
        "version": __version__,
        "now": now,
        "needs_you": {
            "grants_to_confirm": len(waiting),
            "grants_to_review": len(review),
        },
        "counts": {
            "active_grants": sum(1 for g in grants if g.state is GrantState.ACTIVE),
            "open_cards": db.execute("SELECT count(*) FROM cards WHERE state = 'open'").fetchone()[
                0
            ],
            "live_events": attention_repo.count_live_events(db),
        },
        "known_recipients": {
            source: write_repo.recipient_counts(db, source) for source in sorted(ctx.sources.mail)
        },
        "over_budget": memory_repo.over_budget_flag(db),
    }


def _open_cards(db: sqlite3.Connection) -> list[memory_repo.CardRow]:
    return memory_repo.card_rows(
        db.execute(
            "SELECT c.handle, c.title, c.review_at, c.over_cap, (SELECT count(*) FROM actions a "
            "WHERE a.card = c.handle AND a.state = 'open') AS open_actions "
            "FROM cards c WHERE c.state = 'open' ORDER BY c.updated_at DESC, c.handle LIMIT ?",
            (LIST_LIMIT,),
        )
    )


def work(ctx: Context) -> dict[str, Any]:
    """Open cards and the actions due within a week."""
    until = ts(ctx.clock.now() + DUE_WINDOW)
    return {
        "cards": [
            pick(c, "handle", "title", "review_at", "over_cap", "open_actions")
            for c in _open_cards(ctx.db)
        ],
        "actions": [
            pick(a, "handle", "card", "card_title", "title", "due_at", "snooze_until")
            for a in memory_repo.action_lines(ctx.db, "a.due_at <= ?", (until,), limit=LIST_LIMIT)
        ],
    }


def _iso(moment: datetime | None) -> str | None:
    return None if moment is None else ts(moment)


def uncertain(ctx: Context) -> dict[str, Any]:
    """Operations that may or may not have happened: what each was, when, and how to check."""
    found: list[dict[str, Any]] = []
    states = (OperationState.UNCERTAIN, OperationState.RECONCILING)
    for op in write_repo.operations_in_states(ctx.db, states):
        calendar = op.kind.value.startswith("calendar.")
        found.append(
            {
                "handle": op.handle,
                "kind": op.kind.value,
                "state": op.state.value,
                "source": op.source_id,
                "attempted_at": _iso(op.dispatched_at),
                "advice": "Check the calendar for this event."
                if calendar
                else "Check the Sent folder for this message.",
                "rendered": describe_intent(op.intent(), limit=None, refs=op.refs())[0],
                **_resend_facts(ctx, op),
            }
        )
    return {"operations": found}


def _resend_facts(ctx: Context, op: OperationRow) -> dict[str, Any]:
    """What the harness's one 24-hour look found for a send, and any resend (W-37, W-38)."""
    resent = write_repo.resend_of(ctx.db, op.handle)
    return {
        "recheck": recheck_text(op.recheck_result, op.recheck_json),
        "rechecked_at": _iso(op.rechecked_at),
        "redo_of": op.redo_of,
        "resend": None
        if resent is None
        else {"handle": resent.handle, "state": resent.state.value},
    }


def _day_window(ctx: Context, zone: str, day: str | None) -> tuple[str, str, str]:
    tz = ZoneInfo(zone)
    if day is None:
        local = ctx.clock.now().astimezone(tz).date()
    else:
        try:
            local = datetime.strptime(day, "%Y-%m-%d").date()  # noqa: DTZ007 (a date only)
        except ValueError:
            raise ChamberlainError(
                ErrorCode.REQUEST_INVALID, "The day is YYYY-MM-DD.", field="day"
            ) from None
    start = datetime(local.year, local.month, local.day, tzinfo=tz)
    return local.isoformat(), ts(start), ts(start + timedelta(days=1))


def activity(ctx: Context, zone: str, day: str | None) -> dict[str, Any]:
    """What the assistant and the harness did on one local day, from the audit log."""
    shown, start, end = _day_window(ctx, zone, day)
    calls: Counter[str] = Counter()
    for summary in writes_repo.tool_call_summaries(ctx.db, start, end):
        calls[summary.partition(":")[0]] += 1
    rows = writes_repo.audit_between(ctx.db, start, end, ACTIVITY_LIMIT)
    return {
        "day": shown,
        "tool_calls": dict(sorted(calls.items())),
        "events": [pick(r, "seq", "at", "actor", "kind", "subject", "summary") for r in rows],
    }


def runs(ctx: Context) -> dict[str, Any]:
    kinds = {s.name: s.kind.value for s in runs_repo.schedules(ctx.db)}
    return {
        "schedules": [
            {
                **pick(s, "name", "at_local", "zone", "enabled"),
                "kind": s.kind.value,
                "weekdays": sorted(s.weekdays),
            }
            for s in runs_repo.schedules(ctx.db)
        ],
        "runs": [
            {
                **pick(r, "handle", "schedule", "day", "started_at", "finished_at"),
                "kind": kinds.get(r.schedule, ""),
                "state": r.state.value,
                "reports": len(runs_repo.report_handles(ctx.db, r.handle)),
            }
            for r in runs_repo.recent_runs(ctx.db, 30)
        ],
    }


def run_reports(ctx: Context, handle: str) -> dict[str, Any]:
    run = runs_repo.get_run(ctx.db, handle)
    if run is None:
        raise ChamberlainError(ErrorCode.REQUEST_UNKNOWN_HANDLE, "There is no such run.")
    return {
        "run": handle,
        "schedule": run.schedule,
        "day": run.day,
        "state": run.state.value,
        "reports": [pick(r, "handle", "filename", "text") for r in read_reports(ctx, handle)],
    }


def storage(ctx: Context) -> dict[str, Any]:
    metrics = collect_metrics(ctx)
    return {
        "artifacts_live": metrics.artifacts_live,
        "artifact_bytes": metrics.artifact_bytes,
        "artifacts_without_owner": metrics.artifacts_without_owner,
        "orphans_quarantined": metrics.orphans_quarantined,
        "db_bytes": metrics.db_bytes,
        "data_root_bytes": metrics.data_root_bytes,
        "open_cards": metrics.open_cards,
        "evidence_rows": metrics.evidence_rows,
        "over_budget": memory_repo.over_budget_flag(ctx.db),
    }


def notes(ctx: Context) -> dict[str, Any]:
    since = ts(ctx.clock.now() - NOTE_KEEP)
    return {
        "notes": [
            pick(n, "id", "text", "state", "created_at")
            for n in runs_repo.notes_since(ctx.db, since)
        ]
    }
