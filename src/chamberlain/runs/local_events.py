"""Events the owner adds in the viewer. They exist only here: no calendar is written.

Contract: docs/contracts/06-memory.md (the report section). They show on the viewer's calendar
as local events and are invisible to the agent's availability checks, which read the real
calendars. Adding is idempotent by a key the page makes (a double click saves one event).
Each add and remove is audited as the owner.
"""

import hashlib
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from chamberlain.app.context import Context
from chamberlain.domain.canonical import sha256_text
from chamberlain.domain.timefmt import ts
from chamberlain.runs import report_repo as repo
from chamberlain.runs.report_repo import LocalEventRow
from chamberlain.runs.tuning import VIEWER_ACTOR
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.store.db import transaction
from chamberlain.store.migrate import stamp

TITLE_MAX = 180
LOCATION_MAX = 600
KEEP_DAYS = 365


def _invalid(message: str, field: str) -> ChamberlainError:
    return ChamberlainError(ErrorCode.REQUEST_INVALID, message, field=field)


def _text(value: object, field: str, limit: int, *, required: bool) -> str:
    if not isinstance(value, str):
        raise _invalid(f"Give the {field} as text.", field)
    one = value.strip() if field == "location" else " ".join(value.split())
    if (required and not one) or len(one) > limit or (one and not one.isprintable()):
        low = 1 if required else 0
        raise _invalid(f"The {field} is {low} to {limit} printable characters.", field)
    return one


def _instant(value: str, zone: ZoneInfo, field: str) -> datetime:
    """A wall time in the owner's zone, or one with an offset. A time that does not exist or
    happens twice (clocks changing) is refused."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise _invalid("A time is YYYY-MM-DDTHH:MM.", field) from None
    if "T" not in value:
        raise _invalid("A timed entry needs both a date and a time.", field)
    if parsed.tzinfo is not None:
        return parsed.astimezone(UTC)
    early = parsed.replace(tzinfo=zone, fold=0)
    late = parsed.replace(tzinfo=zone, fold=1)
    roundtrip = early.astimezone(UTC).astimezone(zone).replace(tzinfo=None)
    if early.utcoffset() != late.utcoffset() or roundtrip != parsed:
        raise _invalid("That local time is skipped or happens twice. Give an offset.", field)
    return early.astimezone(UTC)


def _day(value: str, field: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise _invalid("A date is YYYY-MM-DD.", field) from None


def build(payload: dict[str, Any], zone: ZoneInfo) -> LocalEventRow:
    """Check a request and make the row. All-day events give the first day and the day after the
    last (end exclusive); timed events give wall times in the owner's zone."""
    title = _text(payload.get("title"), "title", TITLE_MAX, required=True)
    location = _text(payload.get("location", ""), "location", LOCATION_MAX, required=False)
    key = _text(payload.get("key"), "key", 100, required=True)
    all_day = payload.get("all_day")
    first, last = payload.get("start"), payload.get("end")
    if not isinstance(all_day, bool) or not isinstance(first, str) or not isinstance(last, str):
        raise _invalid("Give start, end and all_day.", "start")
    event_id = "local_" + hashlib.sha256(key.encode()).hexdigest()[:24]
    if all_day:
        begin, after = _day(first, "start"), _day(last, "end")
        if after <= begin:
            raise _invalid("The end must be after the start.", "end")
        start = datetime(begin.year, begin.month, begin.day, tzinfo=UTC)
        end = datetime(after.year, after.month, after.day, tzinfo=UTC)
        start_day, end_day = begin, after - timedelta(days=1)
    else:
        start, end = _instant(first, zone, "start"), _instant(last, zone, "end")
        if end <= start:
            raise _invalid("The end must be after the start.", "end")
        start_day = start.astimezone(zone).date()
        end_day = (end - timedelta(seconds=1)).astimezone(zone).date()
    return LocalEventRow(
        event_id,
        title,
        location,
        all_day,
        ts(start),
        ts(end),
        start_day.isoformat(),
        end_day.isoformat(),
    )


def add(ctx: Context, payload: dict[str, Any], zone_name: str) -> LocalEventRow:
    """Save one local event. The same key and content saves nothing new; the same key with other
    content is refused."""
    zone = ZoneInfo(zone_name)
    event = build(payload, zone)
    oldest = (ctx.clock.now().astimezone(zone).date() - timedelta(days=KEEP_DAYS)).isoformat()
    if event.end_day < oldest:
        raise _invalid("That event is too far in the past to keep.", "start")
    with transaction(ctx.db):
        existing = repo.get_local_event(ctx.db, event.id)
        if existing is not None:
            if existing != event:
                raise _invalid("This save key already belongs to a different event.", "key")
            return existing
        repo.insert_local_event(ctx.db, event, stamp(ctx.clock))
        ctx.services.audit.append(
            actor=VIEWER_ACTOR,
            kind="local_event.added",
            subject=event.id,
            summary="The owner added a local calendar event.",
            payload_sha256=sha256_text(event.title),
        )
    return event


def remove(ctx: Context, event_id: str) -> None:
    with transaction(ctx.db):
        if repo.delete_local_event(ctx.db, event_id) == 0:
            raise ChamberlainError(ErrorCode.REQUEST_UNKNOWN_HANDLE, "There is no such event.")
        ctx.services.audit.append(
            actor=VIEWER_ACTOR,
            kind="local_event.removed",
            subject=event_id,
            summary="The owner removed a local calendar event.",
        )


def trim(ctx: Context, now: datetime) -> int:
    """Delete events that ended more than a year ago."""
    edge = (now - timedelta(days=KEEP_DAYS)).date().isoformat()
    with transaction(ctx.db):
        return repo.delete_local_events_before(ctx.db, edge)
