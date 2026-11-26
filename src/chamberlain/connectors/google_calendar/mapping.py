"""Google Calendar event resources to and from the harness event types.

Contract: docs/contracts/08-ports.md (P-16). A PUT replaces the whole resource, so
`merge_update` carries over everything `EventFields` does not describe (reminders,
colour, the recurrence rules and their EXDATE/RDATE lines, guest settings).
"""

from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from chamberlain.connectors.calendar_support.fields import (
    attendees_match,
    clean_one,
    clip_fields,
)
from chamberlain.connectors.calendar_support.rrule import format_rrule, parse_rrule, same_rule
from chamberlain.domain.calendar import (
    CalendarEvent,
    EventFields,
    EventStatus,
    Participant,
    Recurrence,
    Response,
)
from chamberlain.schema.errors import ChamberlainError, ErrorCode

Resource = dict[str, Any]

KEEP_ON_UPDATE = (
    "reminders",
    "colorId",
    "extendedProperties",
    "attachments",
    "source",
    "recurrence",
    "recurringEventId",
    "originalStartTime",
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


def series_rule(item: Resource) -> Recurrence | None:
    """The plain rule of a series' master, or None (not a series, or a rule that is not plain)."""
    start: Resource = item.get("start") or {}
    name = str(start.get("timeZone") or "")
    for line in item.get("recurrence", []):
        if str(line).upper().startswith("RRULE:"):
            return parse_rrule(str(line), zone=name if _zone(name) is not None else None)
    return None


def series_locator(item: Resource) -> str | None:
    """The series an item belongs to: its own id for a master, the master's for an instance."""
    if item.get("recurringEventId"):
        return str(item["recurringEventId"])
    return str(item["id"]) if item.get("recurrence") else None


def fields_of(item: Resource, fallback: datetime) -> EventFields:
    first = item.get("start") or item.get("originalStartTime")
    start, all_day = parse_time(first) if first else (fallback, False)
    end = parse_time(item["end"])[0] if item.get("end") else start
    fields = clip_fields(
        title=str(item.get("summary", "")),
        start=start,
        end=end,
        all_day=all_day,
        location=str(item.get("location", "")),
        description=str(item.get("description", "")),
        attendees=(str(a.get("email", "")) for a in item.get("attendees", []) if not a.get("self")),
        visibility="private" if item.get("visibility") == "private" else None,
    )
    rule = series_rule(item)
    return fields.model_copy(update={"recurrence": rule}) if rule is not None else fields


_RESPONSES = {
    "accepted": Response.ACCEPTED,
    "tentative": Response.TENTATIVE,
    "declined": Response.DECLINED,
}


def _answer(entry: Resource) -> Response:
    return _RESPONSES.get(str(entry.get("responseStatus", "")), Response.NEEDS_ACTION)


def is_invitee_copy(item: Resource) -> bool:
    """Someone else organised this event: only its owner's own answer may change.

    Google's copy of an invitation may list only this calendar's owner as a guest (the
    organiser need not be a guest; found live, 2026-11-25), so the organiser decides.
    """
    organiser: Resource = item.get("organizer") or {}
    if organiser.get("email"):
        return not organiser.get("self")
    return any(not a.get("self") for a in item.get("attendees", []))


def own_entry(item: Resource) -> Resource | None:
    return next((a for a in item.get("attendees", []) if a.get("self")), None)


def event_of(source: str, item: Resource, fallback: datetime) -> CalendarEvent:
    invitee = is_invitee_copy(item)
    mine = own_entry(item)
    organiser = clean_one(str(item.get("organizer", {}).get("email", "")))
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
        organiser=organiser if invitee and organiser else None,
        my_response=_answer(mine) if invitee and mine is not None else None,
        participants=tuple(
            Participant(address=address, response=_answer(a))
            for a in item.get("attendees", [])
            if not a.get("self") and (address := clean_one(str(a.get("email", ""))))
        ),
        series_locator=series_locator(item),
    )


def refuse_if_unwritable(item: Resource, scope: str | None) -> None:
    """Events with guests and special event types are never written, and a part of a series is
    written only with the scope that names it (an instance: occurrence; the master: series)."""
    part = (
        "occurrence"
        if item.get("recurringEventId")
        else "series"
        if item.get("recurrence")
        else None
    )
    if scope != part:
        raise _unsupported("a repeating event is written only with the scope that names it")
    if is_invitee_copy(item):
        raise ChamberlainError(
            ErrorCode.CAPABILITY_UNSUPPORTED,
            "Only the organiser can change that event.",
            detail="invitee-copy",
        )
    if item.get("eventType", "default") != "default":
        raise _unsupported("a special event type")


def _unsupported(what: str) -> ChamberlainError:
    return ChamberlainError(
        ErrorCode.CAPABILITY_UNSUPPORTED,
        "This calendar write is not supported.",
        detail=f"google: {what}",
    )


def time_object(
    moment: datetime, all_day: bool, before: Resource | None, zone: str | None = None
) -> Resource:
    """A start or end object; keeps the zone name the event already had."""
    if all_day:
        return {"date": moment.astimezone(UTC).date().isoformat()}
    zone_name = (before.get("timeZone") if before else None) or zone
    found = _zone(zone_name)
    if found is not None:
        local = moment.astimezone(found).replace(tzinfo=None)
        return {"dateTime": local.isoformat(), "timeZone": str(zone_name)}
    return {"dateTime": moment.astimezone(UTC).isoformat().replace("+00:00", "Z")}


def wanted_body(fields: EventFields, before: Resource | None = None) -> Resource:
    rule = fields.recurrence
    zone = (rule.zone if rule is not None else None) or None
    body: Resource = {
        "summary": fields.title,
        "start": time_object(
            fields.start, fields.all_day, before.get("start") if before else None, zone
        ),
        "end": time_object(fields.end, fields.all_day, before.get("end") if before else None, zone),
    }
    if rule is not None or (before and before.get("recurrence")):
        for key in ("start", "end"):  # Google wants an explicit zone for a series
            if "dateTime" in body[key] and "timeZone" not in body[key]:
                body[key] = {"dateTime": body[key]["dateTime"].rstrip("Z"), "timeZone": "UTC"}
    if rule is not None:
        kept = [
            x
            for x in (before or {}).get("recurrence", [])
            if not str(x).upper().startswith("RRULE:")
        ]
        body["recurrence"] = [f"RRULE:{format_rrule(rule, all_day=fields.all_day)}", *kept]
    if fields.location:
        body["location"] = fields.location
    if fields.description:
        body["description"] = fields.description
    if fields.visibility is not None:
        if (
            before
            and before.get("recurringEventId")
            and before.get("visibility") != fields.visibility
        ):
            raise _unsupported("changing an occurrence's visibility can change the series")
        body["visibility"] = fields.visibility
    attendees = guest_entries(fields, before)
    if attendees:
        body["attendees"] = attendees
    return body


def guest_entries(fields: EventFields, before: Resource | None) -> list[Resource]:
    """The `attendees` array to write: wanted guests (keeping the answers they already gave),
    plus the owner's own entry when the event already had one."""
    if not fields.attendees:
        return []
    existing = {clean_one(str(a.get("email", ""))): a for a in (before or {}).get("attendees", [])}
    entries: list[Resource] = []
    own = [a for a in (before or {}).get("attendees", []) if a.get("self")]
    entries.extend(dict(a) for a in own)
    for address in fields.attendees:
        kept = existing.get(address)
        if kept is not None and not kept.get("self"):
            entries.append(dict(kept))
        elif kept is None:
            entries.append({"email": address})
    return entries


def merge_update(before: Resource, wanted: Resource) -> Resource:
    """A PUT replaces the resource, so carry over what the described fields do not cover."""
    merged: Resource = {key: before[key] for key in KEEP_ON_UPDATE if key in before}
    merged.update(wanted)
    merged["iCalUID"] = before.get("iCalUID", "")
    if before.get("status") and before["status"] != "cancelled":
        merged.setdefault("status", before["status"])
    return merged


def insert_body(event_id: str, fields: EventFields) -> Resource:
    """A new event the calendar will send as an invitation: the client chooses the id."""
    body = wanted_body(fields)
    body["id"] = event_id
    return body


def answer_entries(item: Resource, response: str) -> list[Resource]:
    """The whole `attendees` array with only the owner's own answer changed (a patch replaces
    arrays whole, and a guest may change nothing else)."""
    return [
        {**a, "responseStatus": response} if a.get("self") else dict(a)
        for a in item.get("attendees", [])
    ]


def import_body(uid: str, fields: EventFields) -> Resource:
    body = wanted_body(fields)
    body["iCalUID"] = uid
    return body


def fields_match(item: Resource, wanted: EventFields, fallback: datetime) -> bool:
    actual = fields_of(item, fallback)
    if wanted.recurrence is not None and not same_rule(
        actual.recurrence, wanted.recurrence, actual.start.date(), all_day=wanted.all_day
    ):
        return False  # a series that lost its rule is not what was asked (P-27)
    return (
        actual.title == wanted.title
        and actual.start == wanted.start
        and actual.end == wanted.end
        and actual.all_day == wanted.all_day
        and actual.location == wanted.location
        and actual.description == wanted.description
        and (wanted.visibility is None or actual.visibility == wanted.visibility)
        and attendees_match(
            actual.attendees,
            wanted.attendees,
            ignore=[str(a.get("email", "")) for a in item.get("attendees", []) if a.get("self")],
        )
    )


def window_params(start: datetime, end: datetime) -> dict[str, str]:
    def stamp(moment: datetime) -> str:
        return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")

    return {"timeMin": stamp(start), "timeMax": stamp(end)}
