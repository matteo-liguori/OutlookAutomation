"""The viewer's calendar, card grid, timeline and daily reports, as plain JSON-ready dicts.

Contract: docs/contracts/06-memory.md (read-only views). Everything here only reads: the store
through `repo`, and each calendar source through its read-only `events` call. A source that cannot
be read is reported as such; it never fails the page and its days are never shown as empty.
Colours are per calendar (a stable tone number), never a guess about what an event is.
"""

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from chamberlain.app.context import Context
from chamberlain.domain.calendar import CalendarEvent, CalendarQuery
from chamberlain.limits import CARD_POSITION_MAX
from chamberlain.runs import repo as runs_repo
from chamberlain.runs.reports import read_reports
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.surface.viewer import repo

WINDOW_MAX_DAYS = 45
PAGES_PER_SOURCE = 4
REPORT_DAYS = 60
TONES = 8
"""How many calendar colours the stylesheet has."""
TIMELINE_DAYS_MAX = 31
TIMELINE_LIMIT = 400
CARD_LIMIT = 60
PREVIEW_ACTIONS = 3
_UTC = ZoneInfo("UTC")


def _stored(moment: datetime) -> str:
    return moment.astimezone(_UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_day(text: str | None, field: str) -> date | None:
    if text is None:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()  # noqa: DTZ007 (a date only)
    except ValueError:
        raise ChamberlainError(
            ErrorCode.REQUEST_INVALID, "A day is YYYY-MM-DD.", field=field
        ) from None


def _local_midnight(day: date, tz: ZoneInfo) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=tz)


def _stored_to_local(stamp: str, tz: ZoneInfo) -> datetime:
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_UTC).astimezone(tz)


# --- calendar -----------------------------------------------------------------------------------


def _event_row(event: CalendarEvent, source: str, tone: int, tz: ZoneInfo) -> dict[str, Any]:
    fields = event.fields
    if fields.all_day:
        first = fields.start.astimezone(_UTC).date()
        last = max(first, fields.end.astimezone(_UTC).date() - timedelta(days=1))
        times = ("", "")
    else:
        begin = fields.start.astimezone(tz)
        finish = fields.end.astimezone(tz)
        first = begin.date()
        last = (finish - timedelta(seconds=1)).date() if finish > begin else begin.date()
        times = (begin.strftime("%H:%M"), finish.strftime("%H:%M"))
    return {
        "source": source,
        "tone": tone,
        "title": fields.title[:200],
        "location": fields.location[:200],
        "all_day": fields.all_day,
        "first_day": first.isoformat(),
        "last_day": last.isoformat(),
        "start_time": times[0],
        "end_time": times[1],
        "start": _stored(fields.start),
    }


def _merge(rows: list[dict[str, Any]], labels: dict[str, str]) -> list[dict[str, Any]]:
    """The same event on two calendars is shown once, naming both."""
    merged: dict[tuple[str, str, str, str, bool], dict[str, Any]] = {}
    for row in rows:
        key = (
            " ".join(row["title"].casefold().split()),
            row["start"],
            row["last_day"] + row["end_time"],
            row["first_day"],
            row["all_day"],
        )
        name = labels.get(row["source"]) or row["source"]
        found = merged.get(key)
        if found is None:
            merged[key] = {**row, "calendars": [name]}
        elif name not in found["calendars"]:
            found["calendars"].append(name)
    result = list(merged.values())
    result.sort(key=lambda r: (r["first_day"], not r["all_day"], r["start_time"], r["title"]))
    return result


def calendar(ctx: Context, zone: str, start: str | None, until: str | None) -> dict[str, Any]:
    """Events of every calendar source in `[start, until)`, plus dated actions and report days."""
    tz = ZoneInfo(zone)
    today = ctx.clock.now().astimezone(tz).date()
    first = _parse_day(start, "start") or today - timedelta(days=2)
    last = _parse_day(until, "until") or first + timedelta(days=7)
    if not 0 < (last - first).days <= WINDOW_MAX_DAYS:
        raise ChamberlainError(
            ErrorCode.REQUEST_INVALID, f"Ask for 1 to {WINDOW_MAX_DAYS} days.", field="until"
        )
    window = CalendarQuery(
        start=_local_midnight(first, tz), end=_local_midnight(last, tz), limit=250
    )
    labels = repo.source_labels(ctx.db)
    sources: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    for index, source in enumerate(sorted(ctx.sources.calendar)):
        tone = index % TONES
        note = ""
        complete = False
        try:
            port = ctx.sources.calendar_port(source)
            cursor: str | None = None
            for _ in range(PAGES_PER_SOURCE):
                page = port.events(source, window, cursor)
                rows.extend(_event_row(e, source, tone, tz) for e in page.items)
                complete = page.complete
                cursor = page.cursor
                if cursor is None:
                    break
            if not complete:
                note = "Not all events were read."
        except ChamberlainError:
            note = "This calendar could not be read."
        sources.append(
            {
                "id": source,
                "label": labels.get(source) or source,
                "tone": tone,
                "read": not note or note.startswith("Not all"),
                "complete": complete and not note,
                "note": note,
            }
        )
    marks = [
        {
            "action": a.handle,
            "day": _stored_to_local(a.due_at, tz).date().isoformat(),
            "title": a.title,
            "card": a.card,
            "card_title": a.card_title,
            "due": a.due_at <= _stored(ctx.clock.now()),
        }
        for a in repo.dated_actions(ctx.db, _stored(window.start), _stored(window.end), 200)
        if a.due_at is not None
    ]
    return {
        "timezone": zone,
        "today": today.isoformat(),
        "start": first.isoformat(),
        "until": last.isoformat(),
        "sources": sources,
        "events": _merge(rows, labels),
        "important": marks,
        "report_days": report_days(ctx),
    }


# --- daily reports ------------------------------------------------------------------------------


def report_days(ctx: Context) -> list[str]:
    """Days (newest first) that have at least one stored report."""
    found: list[str] = []
    for run in runs_repo.recent_runs(ctx.db, REPORT_DAYS):
        if run.day not in found and runs_repo.report_handles(ctx.db, run.handle):
            found.append(run.day)
    return sorted(found, reverse=True)


def reports_of_day(ctx: Context, day: str) -> dict[str, Any]:
    """Every report of the runs of one day, oldest run first. Expired ones are left out."""
    _parse_day(day, "day")
    kinds = {s.name: s.kind.value for s in runs_repo.schedules(ctx.db)}
    runs = sorted(
        (r for r in runs_repo.recent_runs(ctx.db, REPORT_DAYS) if r.day == day),
        key=lambda r: (r.started_at or r.due_at, r.handle),
    )
    return {
        "day": day,
        "runs": [
            {
                "handle": r.handle,
                "schedule": r.schedule,
                "kind": kinds.get(r.schedule, ""),
                "state": r.state.value,
                "started_at": r.started_at,
                "reports": [x.text for x in read_reports(ctx, r.handle)],
            }
            for r in runs
        ],
    }


# --- card grid ----------------------------------------------------------------------------------


def cards(ctx: Context) -> dict[str, Any]:
    """Open cards as a grid: goal, position, the next actions and when it is next looked at."""
    now = _stored(ctx.clock.now())
    details = repo.card_details(ctx.db, CARD_LIMIT)
    by_card: dict[str, list[repo.OpenAction]] = {}
    for action in repo.open_actions_of(ctx.db, [c.handle for c in details]):
        by_card.setdefault(action.card, []).append(action)
    items: list[dict[str, Any]] = []
    for card in details:
        actions = by_card.get(card.handle, [])
        overdue = any(a.due_at is not None and a.due_at <= now for a in actions)
        if card.over_cap:
            status = "over its size cap"
        elif overdue:
            status = "action overdue"
        elif card.review_at is not None and card.review_at <= now:
            status = "review due"
        else:
            status = "open"
        items.append(
            {
                "handle": card.handle,
                "title": card.title,
                "goal": card.goal,
                "position": card.position[:CARD_POSITION_MAX],
                "status": status,
                "attention": status != "open",
                "review_at": card.review_at,
                "updated_at": card.updated_at,
                "open_actions": len(actions),
                "actions": [
                    {"title": a.title, "due_at": a.due_at, "snooze_until": a.snooze_until}
                    for a in actions[:PREVIEW_ACTIONS]
                ],
            }
        )
    return {"cards": items}


# --- timeline -----------------------------------------------------------------------------------


def timeline(ctx: Context, zone: str, until: str | None, days: int) -> dict[str, Any]:
    """What was finished and what the harness did, by local day, newest day first."""
    tz = ZoneInfo(zone)
    last = _parse_day(until, "until") or ctx.clock.now().astimezone(tz).date()
    if not 1 <= days <= TIMELINE_DAYS_MAX:
        raise ChamberlainError(
            ErrorCode.REQUEST_INVALID, f"Ask for 1 to {TIMELINE_DAYS_MAX} days.", field="days"
        )
    first = last - timedelta(days=days - 1)
    start = _stored(_local_midnight(first, tz))
    end = _stored(_local_midnight(last + timedelta(days=1), tz))
    grouped: dict[str, list[dict[str, Any]]] = {}
    for action in repo.closed_actions_between(ctx.db, start, end, TIMELINE_LIMIT):
        day = _stored_to_local(action.closed_at, tz).date().isoformat()
        grouped.setdefault(day, []).append(
            {
                "at": action.closed_at,
                "kind": "action",
                "title": action.title,
                "detail": (action.outcome or action.state),
                "card": action.card_title,
            }
        )
    for row in repo.audit_between(ctx.db, start, end, TIMELINE_LIMIT):
        day = _stored_to_local(row.at, tz).date().isoformat()
        grouped.setdefault(day, []).append(
            {
                "at": row.at,
                "kind": "audit",
                "title": row.summary,
                "detail": f"{row.actor}, {row.kind}",
                "card": None,
            }
        )
    return {
        "timezone": zone,
        "days": [
            {"day": day, "items": sorted(grouped[day], key=lambda i: i["at"], reverse=True)}
            for day in sorted(grouped, reverse=True)
        ],
    }
