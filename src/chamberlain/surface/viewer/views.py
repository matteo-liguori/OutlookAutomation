"""What the viewer shows, as plain JSON-ready dicts built from the store and the services.

Contract: docs/contracts/06-memory.md (read-only views), 10-targets.md (storage figures),
05-grants.md (uncertain operations, G-19). Mail bodies and recipient lists never appear here
except in `rendered`, the exact text of an operation the person is asked to decide on.
"""

import json
from collections import Counter
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from chamberlain import __version__
from chamberlain.app.context import Context
from chamberlain.app.metrics import collect_metrics
from chamberlain.domain.grants import GrantState
from chamberlain.domain.operations import OperationState
from chamberlain.runs import repo as runs_repo
from chamberlain.runs.reports import read_reports
from chamberlain.runs.tuning import NOTE_KEEP
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.store.migrate import stamp
from chamberlain.surface.viewer import repo
from chamberlain.writes import repo as write_repo
from chamberlain.writes.engine import Engine
from chamberlain.writes.imported import advice_for, parse_summary
from chamberlain.writes.render import describe_intent

LIST_LIMIT = 50
ACTIVITY_LIMIT = 200
DUE_WINDOW = timedelta(days=7)
_UTC = ZoneInfo("UTC")


def _stored(moment: datetime) -> str:
    return moment.astimezone(_UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


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
        and g.review_at.strftime("%Y-%m-%dT%H:%M:%SZ") <= now
    ]
    uncertain = write_repo.operations_in_states(
        db, (OperationState.UNCERTAIN, OperationState.RECONCILING)
    )
    return {
        "version": __version__,
        "now": now,
        "needs_you": {
            "grants_to_confirm": len(waiting),
            "grants_to_review": len(review),
            "uncertain_operations": len(uncertain),
        },
        "counts": {
            "active_grants": sum(1 for g in grants if g.state is GrantState.ACTIVE),
            "open_cards": repo.count_open_cards(db),
            "live_events": repo.count_live_events(db),
        },
        "known_recipients": {
            source: write_repo.recipient_counts(db, source) for source in sorted(ctx.sources.mail)
        },
        "over_budget": repo.over_budget_flag(db),
    }


def work(ctx: Context) -> dict[str, Any]:
    """Open cards and the actions due within a week."""
    until = _stored(ctx.clock.now() + DUE_WINDOW)
    return {
        "cards": [
            {
                "handle": c.handle,
                "title": c.title,
                "review_at": c.review_at,
                "over_cap": c.over_cap,
                "open_actions": c.open_actions,
            }
            for c in repo.open_cards(ctx.db, LIST_LIMIT)
        ],
        "actions": [
            {
                "handle": a.handle,
                "card": a.card,
                "card_title": a.card_title,
                "title": a.title,
                "due_at": a.due_at,
                "snooze_until": a.snooze_until,
            }
            for a in repo.due_actions(ctx.db, until, LIST_LIMIT)
        ],
    }


def _iso(moment: datetime | None) -> str | None:
    return None if moment is None else _stored(moment)


def uncertain(ctx: Context) -> dict[str, Any]:
    """Operations that may or may not have happened: what each was, when, and how to check.

    An operation imported from an old journal has no stored text (it can never be sent); it shows
    the title, time and calendar or recipients its journal gave, and the time the old journal says
    it was tried (or, when that is unknown, only the time it was imported, labelled as such).
    """
    found: list[dict[str, Any]] = []
    states = (OperationState.UNCERTAIN, OperationState.RECONCILING)
    for op in write_repo.operations_in_states(ctx.db, states):
        imported = op.intent_json is None
        rendered: str | None = None
        if not imported:
            rendered = describe_intent(
                Engine.intent_of(op), limit=None, refs=json.loads(op.refs_json or "{}")
            )[0]
        summary = parse_summary(op.summary_json)
        found.append(
            {
                "handle": op.handle,
                "kind": op.kind.value,
                "state": op.state.value,
                "source": op.source_id,
                "imported": imported,
                "attempted_at": _iso(op.attempted_at if imported else op.dispatched_at),
                "imported_at": _iso(op.created_at) if imported else None,
                "summary": None if summary is None else summary.model_dump(exclude_none=True),
                "advice": advice_for(op.kind),
                "rendered": rendered,
            }
        )
    return {"operations": found}


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
    return local.isoformat(), _stored(start), _stored(start + timedelta(days=1))


def activity(ctx: Context, zone: str, day: str | None) -> dict[str, Any]:
    """What the assistant and the harness did on one local day, from the audit log."""
    shown, start, end = _day_window(ctx, zone, day)
    calls: Counter[str] = Counter()
    for summary in repo.tool_call_summaries(ctx.db, start, end):
        calls[summary.partition(":")[0]] += 1
    rows = repo.audit_between(ctx.db, start, end, ACTIVITY_LIMIT)
    return {
        "day": shown,
        "tool_calls": dict(sorted(calls.items())),
        "events": [
            {
                "seq": r.seq,
                "at": r.at,
                "actor": r.actor,
                "kind": r.kind,
                "subject": r.subject,
                "summary": r.summary,
            }
            for r in rows
        ],
    }


def runs(ctx: Context) -> dict[str, Any]:
    kinds = {s.name: s.kind.value for s in runs_repo.schedules(ctx.db)}
    return {
        "schedules": [
            {
                "name": s.name,
                "kind": s.kind.value,
                "at_local": s.at_local,
                "weekdays": sorted(s.weekdays),
                "zone": s.zone,
                "enabled": s.enabled,
            }
            for s in runs_repo.schedules(ctx.db)
        ],
        "runs": [
            {
                "handle": r.handle,
                "schedule": r.schedule,
                "kind": kinds.get(r.schedule, ""),
                "day": r.day,
                "state": r.state.value,
                "started_at": r.started_at,
                "finished_at": r.finished_at,
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
        "reports": [
            {"handle": r.handle, "filename": r.filename, "text": r.text}
            for r in read_reports(ctx, handle)
        ],
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
        "over_budget": repo.over_budget_flag(ctx.db),
    }


def notes(ctx: Context) -> dict[str, Any]:
    since = _stored(ctx.clock.now() - NOTE_KEEP)
    return {
        "notes": [
            {"id": n.id, "text": n.text, "state": n.state, "created_at": n.created_at}
            for n in runs_repo.notes_since(ctx.db, since)
        ]
    }
