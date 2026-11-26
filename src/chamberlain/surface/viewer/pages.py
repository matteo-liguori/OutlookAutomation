"""The viewer's calendar, card grid, timeline and daily reports, as plain JSON-ready dicts.

Contract: docs/contracts/06-memory.md (read-only views). Everything here only reads: the store
through `repo`, and each calendar source through its read-only `events` call. A source that cannot
be read is reported as such; it never fails the page and its days are never shown as empty.
Colours are per calendar (a stable tone number), never a guess about what an event is.
"""

from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from chamberlain.app.context import Context
from chamberlain.connections import repo as connections_repo
from chamberlain.domain.calendar import CalendarEvent, CalendarQuery
from chamberlain.domain.operations import OUTSIDE_STATE_WORDS
from chamberlain.domain.same_event import same_event
from chamberlain.domain.timefmt import parse, ts
from chamberlain.limits import CARD_POSITION_MAX
from chamberlain.memory import repo as memory_repo
from chamberlain.memory.labels import event_identity
from chamberlain.runs import appointments, compose
from chamberlain.runs import repo as runs_repo
from chamberlain.runs import report_repo as local_repo
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.surface.viewer.report_api import due_appointments
from chamberlain.surface.viewer.views import pick
from chamberlain.writes import repo as writes_repo

WINDOW_MAX_DAYS = 45
PAGES_PER_SOURCE = 4
REPORT_DAYS = 60
TONES = 8
"""How many calendar colours the stylesheet has."""
CATEGORIES: tuple[tuple[str, str], ...] = (
    ("work", "Work and study"),
    ("personal", "Personal admin"),
    ("social", "Social"),
    ("travel", "Travel"),
    ("health", "Health"),
    ("occasion", "Occasions"),
    ("home", "Home"),
)
"""The words an event can be given (`memory.labels.CATEGORIES`) and what the legend calls them."""
LOCAL_SOURCE = "local"
LOCAL_NAME = "Local (kept only in this viewer)"
TIMELINE_DAYS_MAX = 31
TIMELINE_LIMIT = 400
CARD_LIMIT = 60
PREVIEW_ACTIONS = 3


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
    return parse(stamp).astimezone(tz)


# --- calendar -----------------------------------------------------------------------------------


def _link(location: str) -> str:
    """The location when it is a web link the page may offer, else nothing."""
    return location if location.startswith(("https://", "http://")) else ""


def _row(
    source: str,
    tone: int,
    title: str,
    location: str,
    all_day: bool,
    days: tuple[str, str],
    times: tuple[str, str],
    start: str,
    *,
    display_title: str = "",
    category: str = "",
    **extra: Any,
) -> dict[str, Any]:
    """One calendar row of the page, whether it came from a source or from the local store."""
    return {
        "source": source,
        "tone": tone,
        "title": title,
        "display_title": display_title,
        "category": category,
        "location": location,
        "url": _link(location),
        "all_day": all_day,
        "first_day": days[0],
        "last_day": days[1],
        "start_time": times[0],
        "end_time": times[1],
        "start": start,
        **extra,
    }


def _event_row(
    event: CalendarEvent, source: str, tone: int, tz: ZoneInfo, labels: dict[str, tuple[str, str]]
) -> dict[str, Any]:
    fields = event.fields
    if fields.all_day:
        first = fields.start.astimezone(UTC).date()
        last = max(first, fields.end.astimezone(UTC).date() - timedelta(days=1))
        times = ("", "")
    else:
        begin = fields.start.astimezone(tz)
        finish = fields.end.astimezone(tz)
        first = begin.date()
        last = (finish - timedelta(seconds=1)).date() if finish > begin else begin.date()
        times = (begin.strftime("%H:%M"), finish.strftime("%H:%M"))
    category, short = labels.get(event_identity(fields), ("", ""))
    return _row(
        source,
        tone,
        fields.title[:200],
        fields.location[:200],
        fields.all_day,
        (first.isoformat(), last.isoformat()),
        times,
        ts(fields.start),
        display_title=short,
        category=category,
        repeats=event.series_locator is not None,
        local=False,
    )


def _local_row(row: local_repo.LocalEventRow, tz: ZoneInfo) -> dict[str, Any]:
    start = parse(row.start_at)
    end = parse(row.end_at)
    times = (
        ("", "")
        if row.all_day
        else (start.astimezone(tz).strftime("%H:%M"), end.astimezone(tz).strftime("%H:%M"))
    )
    return _row(
        LOCAL_SOURCE,
        0,
        row.title,
        row.location,
        row.all_day,
        (row.start_day, row.end_day),
        times,
        row.start_at,
        id=row.id,
        local=True,
    )


def _when(row: dict[str, Any]) -> tuple[datetime, datetime]:
    """Local wall times of a row, for comparing two rows only."""
    start = datetime.fromisoformat(f"{row['first_day']}T{row['start_time'] or '00:00'}")
    end = datetime.fromisoformat(f"{row['last_day']}T{row['end_time'] or '00:00'}")
    return start, end


def _same(a: dict[str, Any], b: dict[str, Any]) -> bool:
    if a["all_day"] != b["all_day"] or a["first_day"] != b["first_day"]:
        return False
    if a.get("source") == b.get("source"):
        return " ".join(a["title"].casefold().split()) == " ".join(
            b["title"].casefold().split()
        ) and _when(a) == _when(b)
    a_start, a_end = _when(a)
    b_start, b_end = _when(b)
    return same_event(
        a_start,
        a_end,
        a["title"],
        a.get("location", ""),
        b_start,
        b_end,
        b["title"],
        b.get("location", ""),
    ) or (
        " ".join(a["title"].casefold().split()) == " ".join(b["title"].casefold().split())
        and (a_start, a_end) == (b_start, b_end)
    )


def _merge(rows: list[dict[str, Any]], labels: dict[str, str]) -> list[dict[str, Any]]:
    """The same event on two calendars is shown once, naming both: an exact copy, or one real
    event entered separately on each (owner ruling 2026-11-25, domain/same_event.py)."""
    merged: list[dict[str, Any]] = []
    for row in rows:
        name = labels.get(row["source"]) or row["source"]
        found = next((m for m in merged if _same(m, row)), None)
        if found is None:
            merged.append({**row, "calendars": [name]})
        elif name not in found["calendars"]:
            found["calendars"].append(name)
    result = merged
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
    labels = {**connections_repo.source_labels(ctx.db), LOCAL_SOURCE: LOCAL_NAME}
    given = {
        str(r["identity"]): (str(r["category"]), str(r["title"]))
        for r in memory_repo.event_labels_since(ctx.db, first.isoformat())
    }
    sources: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = [
        _local_row(r, tz)
        for r in local_repo.local_events_between(ctx.db, first.isoformat(), last.isoformat())
    ]
    for index, source in enumerate(sorted(ctx.sources.calendar)):
        tone = index % TONES
        note = ""
        complete = False
        try:
            port = ctx.sources.calendar_port(source)
            cursor: str | None = None
            for _ in range(PAGES_PER_SOURCE):
                page = port.events(source, window, cursor)
                rows.extend(_event_row(e, source, tone, tz, given) for e in page.items)
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
    everything = appointments.upcoming(ctx)
    wanted = [a for a in everything if ts(window.start) <= a.start < ts(window.end)]
    important_handles = {a.action for a in everything}
    marks = [
        {
            **pick(a, "action", "start", "title", "card", "card_title", "due"),
            "day": _stored_to_local(a.start, tz).date().isoformat(),
        }
        for a in wanted
    ]
    dated = [
        {
            "action": a.handle,
            "day": _stored_to_local(a.due_at, tz).date().isoformat(),
            "title": a.title,
            "card": a.card,
            "card_title": a.card_title,
            "due": a.due_at <= ts(ctx.clock.now()),
        }
        for a in memory_repo.dated_actions(ctx.db, ts(window.start), ts(window.end), 200)
        if a.due_at is not None and a.handle not in important_handles
    ]
    return {
        "timezone": zone,
        "today": today.isoformat(),
        "start": first.isoformat(),
        "until": last.isoformat(),
        "sources": sources,
        "categories": [{"id": key, "label": name} for key, name in CATEGORIES],
        "events": _merge(rows, labels),
        "important": marks,
        "appointments_due": due_appointments(ctx),
        "dated": dated,
        "report_days": report_days(ctx),
    }


# --- daily reports ------------------------------------------------------------------------------


def report_days(ctx: Context) -> list[str]:
    """Days (newest first) that have at least one stored report."""
    found: list[str] = []
    for run in runs_repo.recent_runs(ctx.db, REPORT_DAYS):
        if run.day not in found and (
            runs_repo.report_handles(ctx.db, run.handle)
            or compose.stored_snapshot(ctx, run.handle) is not None
        ):
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
                **pick(r, "handle", "schedule", "started_at"),
                "kind": kinds.get(r.schedule, ""),
                "state": r.state.value,
                "report": compose.read_composed(ctx, r),
            }
            for r in runs
        ],
    }


# --- card grid ----------------------------------------------------------------------------------


def cards(ctx: Context) -> dict[str, Any]:
    """Open cards as a grid: goal, position, the next actions and when it is next looked at."""
    now = ts(ctx.clock.now())
    details = memory_repo.card_details(ctx.db, CARD_LIMIT)
    by_card: dict[str, list[memory_repo.ActionLine]] = {}
    for action in memory_repo.open_actions_of(ctx.db, [c.handle for c in details]):
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
                    pick(a, "title", "due_at", "snooze_until") for a in actions[:PREVIEW_ACTIONS]
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
    start = ts(_local_midnight(first, tz))
    end = ts(_local_midnight(last + timedelta(days=1), tz))
    grouped: dict[str, list[dict[str, Any]]] = {}
    for action in memory_repo.closed_actions_between(ctx.db, start, end, TIMELINE_LIMIT):
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
    for row in writes_repo.audit_between(ctx.db, start, end, TIMELINE_LIMIT):
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
    for line in writes_repo.outside_between(ctx.db, start, end, TIMELINE_LIMIT):
        day = _stored_to_local(line.at, tz).date().isoformat()
        said = f" It said: {line.reported}" if line.reported else ""
        grouped.setdefault(day, []).append(
            {
                "at": line.at,
                "kind": "outside",
                "title": f"Outside: {line.where} - {line.what}",
                "detail": OUTSIDE_STATE_WORDS.get(line.state, line.state) + "." + said,
                "card": line.card_title,
            }
        )
    return {
        "timezone": zone,
        "days": [
            {"day": day, "items": sorted(grouped[day], key=lambda i: i["at"], reverse=True)}
            for day in sorted(grouped, reverse=True)
        ],
    }
