"""The report as the viewer's front page, and the owner's own actions on the calendar.

Contract: docs/contracts/06-memory.md (the report section) and 05-grants.md (G-17: every route is
behind the viewer's token and Origin checks). Routes:

- `GET /api/report[?run=<handle>]`: the newest report (or the named one) as blocks, the earlier
  reports, passes that have no report yet, and the important appointments waiting for the owner.
- `POST /api/local-events` and `POST /api/local-events/<id>/remove`: events only the viewer keeps.
- `POST /api/appointments/<action>/acknowledge`: the owner holds the button; the reminder action
  finishes and the audit log names the owner. No tool of the agent reaches these two calls.
"""

import re
from typing import Any

from chamberlain.app.context import Context
from chamberlain.domain.timefmt import ts
from chamberlain.runs import appointments, compose, local_events
from chamberlain.runs import repo as runs_repo
from chamberlain.runs.models import RunRow, RunState
from chamberlain.schema.errors import ChamberlainError, ErrorCode

EARLIER_LIMIT = 40
RUN_WINDOW = 90
_ACK = re.compile(r"^/api/appointments/(act_[a-z0-9]{8,32})/acknowledge$")
_LOCAL_REMOVE = re.compile(r"^/api/local-events/(local_[a-z0-9]{8,32})/remove$")
_RUN = re.compile(r"^run_[a-z0-9]{8,32}$")


def appointment_line(item: appointments.Appointment) -> dict[str, Any]:
    return {
        "action": item.action,
        "card": item.card,
        "card_title": item.card_title,
        "title": item.title,
        "start": item.start,
        "reminder_from": item.reminder_from,
        "due": item.due,
    }


def due_appointments(ctx: Context) -> list[dict[str, Any]]:
    return [appointment_line(a) for a in appointments.upcoming(ctx) if a.due]


def _has_report(ctx: Context, run: RunRow) -> bool:
    return bool(runs_repo.report_handles(ctx.db, run.handle)) or (
        compose.stored_snapshot(ctx, run.handle) is not None
    )


def _entry(run: RunRow) -> dict[str, Any]:
    return {
        "run": run.handle,
        "schedule": run.schedule,
        "day": run.day,
        "state": run.state.value,
        "started_at": run.started_at,
    }


def front(ctx: Context, chosen: str | None, zone: str) -> dict[str, Any]:
    """Everything the front page draws."""
    if chosen is not None and _RUN.match(chosen) is None:
        raise ChamberlainError(ErrorCode.REQUEST_INVALID, "That is not a run.", field="run")
    runs = runs_repo.recent_runs(ctx.db, RUN_WINDOW)
    with_report = [r for r in runs if _has_report(ctx, r)]
    shown = next((r for r in with_report if r.handle == chosen), None)
    if chosen is not None and shown is None:
        raise ChamberlainError(ErrorCode.REQUEST_UNKNOWN_HANDLE, "There is no such report.")
    shown = shown or (with_report[0] if with_report else None)
    waiting = [
        _entry(r) for r in runs if r.state in (RunState.DUE, RunState.RUNNING) and r is not shown
    ]
    return {
        "timezone": zone,
        "report": None if shown is None else compose.read_composed(ctx, shown),
        "earlier": [_entry(r) for r in with_report if r is not shown][:EARLIER_LIMIT],
        "waiting": waiting,
        "appointments_due": due_appointments(ctx),
        "now": ts(ctx.clock.now()),
    }


def local_event_line(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "title": row.title,
        "location": row.location,
        "all_day": row.all_day,
        "start": row.start_at,
        "end": row.end_at,
    }


def post(ctx: Context, path: str, body: dict[str, Any], zone: str) -> dict[str, Any] | None:
    """Handle the owner's POSTs of this module. None when the path is not one of them."""
    if path == "/api/local-events":
        return {"event": local_event_line(local_events.add(ctx, body, zone))}
    removal = _LOCAL_REMOVE.match(path)
    if removal:
        local_events.remove(ctx, removal.group(1))
        return {"removed": removal.group(1)}
    ack = _ACK.match(path)
    if ack:
        appointments.acknowledge(ctx, ack.group(1))
        return {"acknowledged": ack.group(1)}
    return None
