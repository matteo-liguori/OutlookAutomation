"""iCalendar text to and from `EventFields`, for CalDAV and the Owl bridge.

Contract: docs/contracts/08-ports.md (P-16). Reading never raises on odd data
(floating times are read as UTC). Writing changes only what `EventFields`
describes and keeps every other property, alarm, recurrence rule and exception
exactly as the server sent them.
"""

import hashlib
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo

import icalendar

from chamberlain.connectors.calendar_support.fields import clean_one, clip_fields
from chamberlain.connectors.calendar_support.rrule import format_rrule, parse_rrule
from chamberlain.domain.calendar import EventFields, EventStatus, Recurrence
from chamberlain.domain.recurrence import parse_occurrence_id
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
    organiser_address: str | None = None
    """The address in ORGANIZER, lower-cased, when there is one."""
    partstats: tuple[tuple[str, str], ...] = ()
    """`(address, PARTSTAT)` of every ATTENDEE, the organiser's own entry included."""
    rule: Recurrence | None = None
    """The plain repeat rule of a series' own component; None when absent or not plain."""
    zone: str | None = None
    """The IANA zone DTSTART is written in, when it is not UTC."""
    cancelled_dates: tuple[str, ...] = ()
    """Occurrence ids (`domain.recurrence.occurrence_id`) taken out of the series by EXDATE."""


def text_etag(text: str) -> str:
    """The version of an event for stores that keep none: a hash of its exact text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _utc(value: date | datetime) -> tuple[datetime, bool]:
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=UTC)
        return moment.astimezone(UTC), False
    return datetime(value.year, value.month, value.day, tzinfo=UTC), True


def _prop(component: Any, name: str) -> Any:
    """One property's value. A property written twice (seen live in an iCloud event's
    LAST-MODIFIED) gives its first value, so one odd event never breaks a calendar."""
    value = component.get(name)
    if isinstance(value, list):
        value = cast(list[Any], value)[0] if value else None
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


def _zone_of(component: Any) -> str | None:
    """The IANA zone DTSTART is written in: its TZID parameter when that names one (a server may
    rewrite the VTIMEZONE), else the zone of the parsed value."""
    start = component.get("DTSTART")
    named = getattr(start, "params", {}).get("TZID") if start is not None else None
    if isinstance(named, str):
        try:
            ZoneInfo(named)
        except (ValueError, OSError, KeyError):
            named = None
        else:
            return None if named == "UTC" else named
    key = getattr(getattr(_prop(component, "DTSTART"), "tzinfo", None), "key", None)
    return key if isinstance(key, str) and key != "UTC" else None


def _exdates(component: Any) -> tuple[str, ...]:
    found: list[str] = []
    for entry in _list(component, "EXDATE"):
        for item in getattr(entry, "dts", []):
            text = _recurrence_text(getattr(item, "dt", item))
            if text:
                found.append(text)
    return tuple(found)


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
        attendees=(cal_address(a) for a in _list(component, "ATTENDEE")),
        visibility="private" if str(_prop(component, "CLASS")).upper() == "PRIVATE" else None,
    )
    try:
        sequence = int(component.get("SEQUENCE", 0))
    except (TypeError, ValueError):
        sequence = 0
    organiser_text = component.get("ORGANIZER")
    zone = _zone_of(component)
    rrule = component.get("RRULE")
    return ParsedEvent(
        rule=parse_rrule(rrule.to_ical().decode(), zone=zone) if rrule is not None else None,
        zone=zone,
        cancelled_dates=_exdates(component),
        uid=str(component.get("UID", "")),
        recurrence_id=_recurrence_text(_prop(component, "RECURRENCE-ID")),
        fields=fields,
        status=status,
        updated=updated,
        recurring=component.get("RRULE") is not None or component.get("RDATE") is not None,
        organiser=organiser_text is not None,
        sequence=sequence,
        organiser_address=(
            cal_address(organiser_text) or None if organiser_text is not None else None
        ),
        partstats=tuple(
            (address, str(getattr(a, "params", {}).get("PARTSTAT", "NEEDS-ACTION")).upper())
            for a in _list(component, "ATTENDEE")
            if (address := cal_address(a))
        ),
    )


def cal_address(value: Any) -> str:
    """The email address of an ORGANIZER or ATTENDEE, cleaned; empty if it has none.

    iCloud rewrites its own user to a principal path (`/a.../principal/`) and keeps the address
    in the EMAIL parameter (found live, 2026-11-25), so a value that is not `mailto:` falls
    back to EMAIL.
    """
    text = str(value)
    if not text.lower().startswith("mailto:"):
        email = getattr(value, "params", {}).get("EMAIL")
        if email:
            return clean_one(str(email))
    return clean_one(text)


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


def _set_times(component: Any, fields: EventFields, zone: str | None = None) -> None:
    """Times in UTC, or on `zone`'s clock when a series repeats on it (nine o'clock stays)."""
    for name in ("DTSTART", "DTEND", "DURATION"):
        component.pop(name, None)
    if fields.all_day:
        component.add("dtstart", fields.start.date())
        component.add("dtend", fields.end.date())
    else:
        clock = ZoneInfo(zone) if zone and zone != "UTC" else UTC
        component.add("dtstart", fields.start.astimezone(clock))
        component.add("dtend", fields.end.astimezone(clock))


def _set_rule(component: Any, rule: Recurrence | None, *, all_day: bool) -> None:
    if rule is None:
        return
    component.pop("RRULE", None)
    component.add("rrule", icalendar.vRecur.from_ical(format_rrule(rule, all_day=all_day)))


def _with_zones(calendar: Any) -> str:
    """The text of the calendar, with VTIMEZONE blocks for any zone it names."""
    if any(_zone_of(c) for c in calendar.walk("VEVENT")):
        calendar.add_missing_timezones()
    return calendar.to_ical().decode("utf-8")


def _set_text(component: Any, name: str, value: str) -> None:
    component.pop(name.upper(), None)
    if value:
        component.add(name.lower(), value)


def _add_guest(event: Any, address: str, partstat: str, *, organiser: bool = False) -> None:
    params = {"PARTSTAT": partstat, "CUTYPE": "INDIVIDUAL", "ROLE": "REQ-PARTICIPANT"}
    if not organiser:
        params["RSVP"] = "TRUE"
    event.add("attendee", f"mailto:{address}", parameters=params)


def _set_invitation(
    event: Any, guests: tuple[str, ...], organiser: str, kept: dict[str, str]
) -> None:
    """ORGANIZER and ATTENDEE lines for an invitation (RFC 5545; the server schedules, RFC 6638).

    `kept` maps an address to the PARTSTAT it already has; anyone else starts NEEDS-ACTION.
    The organiser is listed as accepted, as calendar clients do.
    """
    for name in ("ORGANIZER", "ATTENDEE"):
        event.pop(name, None)
    if not guests:
        return
    event.add("organizer", f"mailto:{organiser}")
    _add_guest(event, organiser, "ACCEPTED", organiser=True)
    for address in guests:
        if address != organiser:
            _add_guest(event, address, kept.get(address, "NEEDS-ACTION"))


def build_new(uid: str, fields: EventFields, now: datetime, organiser: str | None = None) -> str:
    """A fresh single-event resource, or a series when the fields carry a rule. Times are written
    in UTC, or on the rule's clock for a series.

    With guests, `organiser` is the sending account's address and the result is an invitation.
    """
    event = Event()
    event.add("uid", uid)
    event.add("dtstamp", now.astimezone(UTC))
    event.add("summary", fields.title)
    rule = fields.recurrence
    _set_times(event, fields, rule.zone if rule is not None else None)
    _set_rule(event, rule, all_day=fields.all_day)
    _set_text(event, "location", fields.location)
    _set_text(event, "description", fields.description)
    if fields.visibility == "private":
        _set_text(event, "class", "PRIVATE")
    event.add("sequence", 0)
    if fields.attendees and organiser is not None:
        _set_invitation(event, fields.attendees, organiser, {})
    calendar = Calendar()
    calendar.add("prodid", PRODID)
    calendar.add("version", "2.0")
    calendar.add_component(event)
    return _with_zones(calendar)


def apply_fields(
    text: str, fields: EventFields, now: datetime, organiser: str | None = None
) -> str:
    """The resource with the master's described fields replaced and nothing else touched.

    Guests are written only when `organiser` is given (the sending account). Guests who stay
    keep their answers, unless the time changed (RFC 5546: a new time asks everyone again).
    """
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
    moved = (current.fields.start, current.fields.end, current.fields.all_day) != (
        fields.start,
        fields.end,
        fields.all_day,
    )
    rule = fields.recurrence
    zone = rule.zone if rule is not None else current.zone
    reclocked = (
        rule is not None and (rule.zone or None) != current.zone and current.rule is not None
    )
    if moved or reclocked:
        _set_times(event, fields, zone)
    _set_rule(event, rule, all_day=fields.all_day)
    if organiser is not None:
        kept = {} if moved else dict(current.partstats)
        _set_invitation(event, fields.attendees, organiser, kept)
    _set_text(event, "location", fields.location)
    _set_text(event, "description", fields.description)
    if fields.visibility == "private":
        _set_text(event, "class", "PRIVATE")
    for name in ("DTSTAMP", "LAST-MODIFIED", "SEQUENCE"):
        event.pop(name, None)
    event.add("dtstamp", now.astimezone(UTC))
    event.add("last-modified", now.astimezone(UTC))
    event.add("sequence", current.sequence + 1)
    return _with_zones(calendar)


def _series_parts(calendar: Any) -> tuple[Any, list[Any]]:
    """The series' own component and its overrides."""
    masters = [c for c in calendar.walk("VEVENT") if c.get("RECURRENCE-ID") is None]
    if len(masters) != 1 or (masters[0].get("RRULE") is None and masters[0].get("RDATE") is None):
        raise ChamberlainError(
            ErrorCode.CAPABILITY_UNSUPPORTED, "This calendar item is not a repeating event."
        )
    overrides = [c for c in calendar.walk("VEVENT") if c.get("RECURRENCE-ID") is not None]
    return masters[0], overrides


def _in_form_of(master: Any, occurrence: str) -> Any:
    """The moment an occurrence id names, written as the series' own start is (RFC 5545 asks
    RECURRENCE-ID and EXDATE to match DTSTART's form)."""
    named = parse_occurrence_id(occurrence)
    if named is None:
        raise ChamberlainError(ErrorCode.REQUEST_INVALID, "That is not an occurrence.")
    moment, all_day = named
    if all_day:
        return moment.date()
    start = _prop(master, "DTSTART")
    clock = getattr(start, "tzinfo", None)
    if clock is None or getattr(clock, "key", "UTC") == "UTC":
        return moment
    return moment.astimezone(clock)


def _stamp(event: Any, now: datetime, sequence: int) -> None:
    for name in ("DTSTAMP", "LAST-MODIFIED", "SEQUENCE"):
        event.pop(name, None)
    event.add("dtstamp", now.astimezone(UTC))
    event.add("last-modified", now.astimezone(UTC))
    event.add("sequence", sequence)


def _override_for(overrides: list[Any], occurrence: str) -> Any | None:
    for component in overrides:
        if _recurrence_text(_prop(component, "RECURRENCE-ID")) == occurrence:
            return component
    return None


def apply_occurrence(text: str, occurrence: str, fields: EventFields, now: datetime) -> str:
    """The series with one date changed: an override component for that date, the series itself
    and every other date untouched."""
    calendar = Calendar.from_ical(text)
    master, overrides = _series_parts(calendar)
    event = _override_for(overrides, occurrence)
    sequence = 0
    if event is None:
        event = Event()
        event.add("uid", str(master.get("UID", "")))
        event.add("recurrence-id", _in_form_of(master, occurrence))
        # A date of a series with guests keeps them: an override without ATTENDEE would tell
        # the server that date has no guests, and it would send them a cancellation.
        for name in ("ORGANIZER", "ATTENDEE", "CLASS"):
            for value in _list(master, name):
                event.add(name.lower(), value)
        calendar.add_component(event)
    else:
        sequence = _parse_component(event).sequence + 1
    event.pop("SUMMARY", None)
    event.add("summary", fields.title)
    _set_times(event, fields)
    _set_text(event, "location", fields.location)
    _set_text(event, "description", fields.description)
    if fields.visibility == "private":
        _set_text(event, "class", "PRIVATE")
    _stamp(event, now, sequence)
    return _with_zones(calendar)


def cancel_occurrence(text: str, occurrence: str, now: datetime) -> str:
    """The series with one date taken out (EXDATE), and any override of that date dropped."""
    calendar = Calendar.from_ical(text)
    master, overrides = _series_parts(calendar)
    gone = _override_for(overrides, occurrence)
    if gone is not None:
        calendar.subcomponents = [c for c in calendar.subcomponents if c is not gone]
    if occurrence not in _exdates(master):
        master.add("exdate", _in_form_of(master, occurrence))
    _stamp(master, now, _parse_component(master).sequence + 1)
    return _with_zones(calendar)


def occurrence_of(text: str, occurrence: str) -> ParsedEvent | None:
    """The override component for one date, as written (None when that date is not changed)."""
    for parsed in parse_events(text):
        if parsed.recurrence_id == occurrence:
            return parsed
    return None


def apply_answer(text: str, address: str, partstat: str, now: datetime) -> str:
    """The resource with only `address`'s PARTSTAT changed. SEQUENCE stays: it is the
    organiser's counter, and an invitee must never move it."""
    calendar = Calendar.from_ical(text)
    masters = [c for c in calendar.walk("VEVENT") if c.get("RECURRENCE-ID") is None]
    if len(masters) != 1:
        raise ChamberlainError(
            ErrorCode.CAPABILITY_UNSUPPORTED, "This calendar item is not a single event."
        )
    event = masters[0]
    changed = False
    for entry in _list(event, "ATTENDEE"):
        if cal_address(entry) == address:
            entry.params["PARTSTAT"] = partstat
            changed = True
    if not changed:
        raise ChamberlainError(
            ErrorCode.REQUEST_INVALID, "That event is not an invitation to this calendar."
        )
    event.pop("DTSTAMP", None)
    event.add("dtstamp", now.astimezone(UTC))
    return calendar.to_ical().decode("utf-8")


def instance_locator(base: str, recurrence_id: str | None) -> str:
    return base if recurrence_id is None else f"{base}#{recurrence_id}"


def split_locator(locator: str) -> tuple[str, str | None]:
    base, _, recurrence = locator.partition("#")
    return base, recurrence or None
