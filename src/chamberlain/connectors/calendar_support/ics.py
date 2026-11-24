"""iCalendar text to and from `EventFields`, for CalDAV and the Owl bridge.

Contract: docs/contracts/08-ports.md (P-16). Reading never raises on odd data
(floating times are read as UTC). Writing changes only what `EventFields`
describes and keeps every other property, alarm, recurrence rule and exception
exactly as the server sent them.
"""

import hashlib
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import icalendar

from chamberlain.connectors.calendar_support.fields import clip_fields
from chamberlain.domain.calendar import EventFields, EventStatus
from chamberlain.schema.errors import ChamberlainError, ErrorCode

Calendar: Any = icalendar.Calendar
Event: Any = icalendar.Event

PRODID = "-//Chamberlain//Calendar connector//EN"


@dataclass(frozen=True)
class ParsedEvent:
    uid: str
    recurrence_id: str | None
    fields: EventFields
    status: EventStatus
    updated: datetime
    recurring: bool
    """The component carries an RRULE or RDATE."""
    organiser: bool
    sequence: int


def text_etag(text: str) -> str:
    """The version of an event for stores that keep none: a hash of its exact text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _utc(value: date | datetime) -> tuple[datetime, bool]:
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=UTC)
        return moment.astimezone(UTC), False
    return datetime(value.year, value.month, value.day, tzinfo=UTC), True


def _prop(component: Any, name: str) -> Any:
    value = component.get(name)
    return None if value is None else getattr(value, "dt", value)


def _list(component: Any, name: str) -> list[Any]:
    value = component.get(name)
    if value is None:
        return []
    return [*value] if isinstance(value, list) else [value]


def _recurrence_text(value: Any) -> str | None:
    if value is None:
        return None
    moment, all_day = _utc(value)
    return moment.strftime("%Y%m%d") if all_day else moment.strftime("%Y%m%dT%H%M%SZ")


def _components(text: str) -> list[Any]:
    try:
        calendar: Any = Calendar.from_ical(text)
    except ValueError as error:
        raise ChamberlainError(
            ErrorCode.SOURCE_UNAVAILABLE,
            "The calendar returned unreadable data.",
            detail=str(error),
        ) from None
    return list(calendar.walk("VEVENT"))


def _parse_component(component: Any) -> ParsedEvent:
    raw_start = _prop(component, "DTSTART")
    if raw_start is None:
        start, all_day = datetime(1970, 1, 1, tzinfo=UTC), False
    else:
        start, all_day = _utc(raw_start)
    raw_end = _prop(component, "DTEND")
    duration = _prop(component, "DURATION")
    if raw_end is not None:
        end = _utc(raw_end)[0]
    elif isinstance(duration, timedelta):
        end = start + duration
    else:
        end = start + timedelta(days=1) if all_day else start
    stamp = _prop(component, "LAST-MODIFIED") or _prop(component, "DTSTAMP")
    updated = _utc(stamp)[0] if stamp is not None else start
    status_text = str(component.get("STATUS", "CONFIRMED")).upper()
    status = {
        "CANCELLED": EventStatus.CANCELLED,
        "TENTATIVE": EventStatus.TENTATIVE,
    }.get(status_text, EventStatus.CONFIRMED)
    fields = clip_fields(
        title=str(component.get("SUMMARY", "")),
        start=start,
        end=end,
        all_day=all_day,
        location=str(component.get("LOCATION", "")),
        description=str(component.get("DESCRIPTION", "")),
        attendees=(str(a) for a in _list(component, "ATTENDEE")),
    )
    try:
        sequence = int(component.get("SEQUENCE", 0))
    except (TypeError, ValueError):
        sequence = 0
    return ParsedEvent(
        uid=str(component.get("UID", "")),
        recurrence_id=_recurrence_text(_prop(component, "RECURRENCE-ID")),
        fields=fields,
        status=status,
        updated=updated,
        recurring=component.get("RRULE") is not None or component.get("RDATE") is not None,
        organiser=component.get("ORGANIZER") is not None,
        sequence=sequence,
    )


def parse_events(text: str) -> list[ParsedEvent]:
    """Every VEVENT in the resource, masters and overrides alike."""
    return [_parse_component(c) for c in _components(text)]


def master_of(text: str) -> ParsedEvent:
    """The one event that is not an override. Raises when there is not exactly one."""
    masters = [e for e in parse_events(text) if e.recurrence_id is None]
    if len(masters) != 1:
        raise ChamberlainError(
            ErrorCode.CAPABILITY_UNSUPPORTED, "This calendar item is not a single event."
        )
    return masters[0]


def no_invitations() -> ChamberlainError:
    """The clean refusal of a write that would reach other people, for a connector that cannot."""
    return ChamberlainError(
        ErrorCode.CAPABILITY_UNSUPPORTED,
        "This calendar cannot send or answer invitations.",
        detail="invitations-not-supported",
    )


def refuse_invitations(parsed: ParsedEvent | None, wanted: EventFields | None = None) -> None:
    """Nothing here sends invitations, so events with other people are never written."""
    if (parsed is not None and (parsed.fields.attendees or parsed.organiser)) or (
        wanted is not None and wanted.attendees
    ):
        raise ChamberlainError(
            ErrorCode.CAPABILITY_UNSUPPORTED,
            "Events with attendees cannot be written by this connector.",
            detail="invitations-not-supported",
        )


def _set_times(component: Any, fields: EventFields) -> None:
    for name in ("DTSTART", "DTEND", "DURATION"):
        component.pop(name, None)
    if fields.all_day:
        component.add("dtstart", fields.start.date())
        component.add("dtend", fields.end.date())
    else:
        component.add("dtstart", fields.start.astimezone(UTC))
        component.add("dtend", fields.end.astimezone(UTC))


def _set_text(component: Any, name: str, value: str) -> None:
    component.pop(name.upper(), None)
    if value:
        component.add(name.lower(), value)


def build_new(uid: str, fields: EventFields, now: datetime) -> str:
    """A fresh single-event resource. Times are written in UTC."""
    event = Event()
    event.add("uid", uid)
    event.add("dtstamp", now.astimezone(UTC))
    event.add("summary", fields.title)
    _set_times(event, fields)
    _set_text(event, "location", fields.location)
    _set_text(event, "description", fields.description)
    event.add("sequence", 0)
    calendar = Calendar()
    calendar.add("prodid", PRODID)
    calendar.add("version", "2.0")
    calendar.add_component(event)
    return calendar.to_ical().decode("utf-8")


def apply_fields(text: str, fields: EventFields, now: datetime) -> str:
    """The resource with the master's described fields replaced and nothing else touched."""
    calendar = Calendar.from_ical(text)
    masters = [c for c in calendar.walk("VEVENT") if c.get("RECURRENCE-ID") is None]
    if len(masters) != 1:
        raise ChamberlainError(
            ErrorCode.CAPABILITY_UNSUPPORTED, "This calendar item is not a single event."
        )
    event = masters[0]
    current = _parse_component(event)
    event.pop("SUMMARY", None)
    event.add("summary", fields.title)
    if (current.fields.start, current.fields.end, current.fields.all_day) != (
        fields.start,
        fields.end,
        fields.all_day,
    ):
        _set_times(event, fields)
    _set_text(event, "location", fields.location)
    _set_text(event, "description", fields.description)
    for name in ("DTSTAMP", "LAST-MODIFIED", "SEQUENCE"):
        event.pop(name, None)
    event.add("dtstamp", now.astimezone(UTC))
    event.add("last-modified", now.astimezone(UTC))
    event.add("sequence", current.sequence + 1)
    return calendar.to_ical().decode("utf-8")


def instance_locator(base: str, recurrence_id: str | None) -> str:
    return base if recurrence_id is None else f"{base}#{recurrence_id}"


def split_locator(locator: str) -> tuple[str, str | None]:
    base, _, recurrence = locator.partition("#")
    return base, recurrence or None
