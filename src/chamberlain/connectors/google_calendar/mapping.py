"""Google Calendar event resources to and from the harness event types.

Contract: docs/contracts/08-ports.md (P-16). A PUT replaces the whole resource, so
`merge_update` carries over everything `EventFields` does not describe (reminders,
colour, the recurrence rules and their EXDATE/RDATE lines, guest settings).
"""

from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from chamberlain.connectors.calendar_support.fields import clip_fields
from chamberlain.domain.calendar import CalendarEvent, EventFields, EventStatus
from chamberlain.schema.errors import ChamberlainError, ErrorCode

Resource = dict[str, Any]

KEEP_ON_UPDATE = (
    "reminders",
    "colorId",
    "extendedProperties",
    "attachments",
    "source",
    "recurrence",
    "transparency",
    "visibility",
    "guestsCanInviteOthers",
    "guestsCanModify",
    "guestsCanSeeOtherGuests",
    "anyoneCanAddSelf",
    "eventType",
    "conferenceData",
)


def _zone(name: str | None) -> ZoneInfo | None:
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def parse_time(value: Resource) -> tuple[datetime, bool]:
    """A Google start or end object as a UTC instant and an all-day flag."""
    try:
        return _parse_time(value)
    except (KeyError, ValueError, TypeError):
        raise ChamberlainError(
            ErrorCode.SOURCE_UNAVAILABLE,
            "Google returned a time this connector cannot read.",
            detail=f"google time: {value!r}",
        ) from None


def _parse_time(value: Resource) -> tuple[datetime, bool]:
    if "date" in value:
        day = date.fromisoformat(value["date"])
        return datetime(day.year, day.month, day.day, tzinfo=UTC), True
    moment = datetime.fromisoformat(str(value["dateTime"]))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_zone(value.get("timeZone")) or UTC)
    return moment.astimezone(UTC), False


def parse_stamp(text: object, fallback: datetime) -> datetime:
    try:
        return datetime.fromisoformat(str(text)).astimezone(UTC)
    except ValueError:
        return fallback


def has_start(item: Resource) -> bool:
    return bool(item.get("start") or item.get("originalStartTime"))


def fields_of(item: Resource, fallback: datetime) -> EventFields:
    first = item.get("start") or item.get("originalStartTime")
    start, all_day = parse_time(first) if first else (fallback, False)
    end = parse_time(item["end"])[0] if item.get("end") else start
    return clip_fields(
        title=str(item.get("summary", "")),
        start=start,
        end=end,
        all_day=all_day,
        location=str(item.get("location", "")),
        description=str(item.get("description", "")),
        attendees=(str(a.get("email", "")) for a in item.get("attendees", []) if not a.get("self")),
    )


def event_of(source: str, item: Resource, fallback: datetime) -> CalendarEvent:
    status = {
        "cancelled": EventStatus.CANCELLED,
        "tentative": EventStatus.TENTATIVE,
    }.get(str(item.get("status", "confirmed")), EventStatus.CONFIRMED)
    return CalendarEvent(
        source=source,
        locator=str(item["id"]),
        uid=str(item.get("iCalUID") or item["id"]),
        etag=str(item.get("etag") or item.get("updated") or item["id"]),
        status=status,
        fields=fields_of(item, fallback),
        updated=parse_stamp(item.get("updated"), fallback),
    )


def refuse_if_unwritable(item: Resource) -> None:
    """Instances of a series, events with guests and special event types are never written."""
    if item.get("recurringEventId"):
        raise _unsupported("an occurrence of a recurring event")
    if any(not a.get("self") for a in item.get("attendees", [])):
        raise _unsupported("an event with attendees")
    if item.get("eventType", "default") != "default":
        raise _unsupported("a special event type")


def _unsupported(what: str) -> ChamberlainError:
    return ChamberlainError(
        ErrorCode.CAPABILITY_UNSUPPORTED,
        "This calendar write is not supported.",
        detail=f"google: {what}",
    )


def time_object(moment: datetime, all_day: bool, before: Resource | None) -> Resource:
    """A start or end object; keeps the zone name the event already had."""
    if all_day:
        return {"date": moment.astimezone(UTC).date().isoformat()}
    zone_name = before.get("timeZone") if before else None
    zone = _zone(zone_name)
    if zone is not None:
        local = moment.astimezone(zone).replace(tzinfo=None)
        return {"dateTime": local.isoformat(), "timeZone": str(zone_name)}
    return {"dateTime": moment.astimezone(UTC).isoformat().replace("+00:00", "Z")}


def wanted_body(fields: EventFields, before: Resource | None = None) -> Resource:
    body: Resource = {
        "summary": fields.title,
        "start": time_object(fields.start, fields.all_day, before.get("start") if before else None),
        "end": time_object(fields.end, fields.all_day, before.get("end") if before else None),
    }
    if before and before.get("recurrence"):
        for key in ("start", "end"):  # Google wants an explicit zone for a series
            if "dateTime" in body[key] and "timeZone" not in body[key]:
                body[key] = {"dateTime": body[key]["dateTime"].rstrip("Z"), "timeZone": "UTC"}
    if fields.location:
        body["location"] = fields.location
    if fields.description:
        body["description"] = fields.description
    return body


def merge_update(before: Resource, wanted: Resource) -> Resource:
    """A PUT replaces the resource, so carry over what the described fields do not cover."""
    merged: Resource = {key: before[key] for key in KEEP_ON_UPDATE if key in before}
    merged.update(wanted)
    merged["iCalUID"] = before.get("iCalUID", "")
    if before.get("status") and before["status"] != "cancelled":
        merged.setdefault("status", before["status"])
    return merged


def import_body(uid: str, fields: EventFields) -> Resource:
    body = wanted_body(fields)
    body["iCalUID"] = uid
    return body


def fields_match(item: Resource, wanted: EventFields, fallback: datetime) -> bool:
    actual = fields_of(item, fallback)
    return (
        actual.title == wanted.title
        and actual.start == wanted.start
        and actual.end == wanted.end
        and actual.all_day == wanted.all_day
        and actual.location == wanted.location
        and actual.description == wanted.description
    )


def window_params(start: datetime, end: datetime) -> dict[str, str]:
    def stamp(moment: datetime) -> str:
        return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")

    return {"timeMin": stamp(start), "timeMax": stamp(end)}
