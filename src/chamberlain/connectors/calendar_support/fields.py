"""Never-raising mapping from remote event values to `EventFields`.

Contract: docs/contracts/08-ports.md (P-18). A real calendar holds untitled,
oversized and odd events; reading one must not raise a validation error, so
values are clipped to the field limits instead.
"""

import re
from collections.abc import Iterable
from datetime import datetime

from chamberlain.domain.calendar import EventFields

TITLE_MAX = 300
LOCATION_MAX = 300
DESCRIPTION_MAX = 4000
UNTITLED = "(no title)"
_ADDRESS = re.compile(r"^[^\s<>@,;]+@[^\s<>@,;]+\.[^\s<>@,;]+$")


def clean_addresses(values: Iterable[str]) -> tuple[str, ...]:
    """Lower-cased, valid, de-duplicated addresses in order."""
    seen: list[str] = []
    for value in values:
        address = value.strip().removeprefix("mailto:").removeprefix("MAILTO:").lower()
        if _ADDRESS.match(address) and address not in seen:
            seen.append(address)
    return tuple(seen)


def clean_one(value: str) -> str:
    """One address, cleaned as `clean_addresses` does, or an empty string if it is not one."""
    found = clean_addresses([value])
    return found[0] if found else ""


def attendees_match(
    actual: Iterable[str], wanted: Iterable[str], *, ignore: Iterable[str] = ()
) -> bool:
    """Every wanted attendee is on the event. Organisers and the owner's own entry never count."""
    skip = set(clean_addresses(ignore))
    have = set(clean_addresses(actual)) - skip
    return (
        set(clean_addresses(wanted)) - skip <= have and have <= set(clean_addresses(wanted)) | skip
    )


def clip_fields(
    *,
    title: str,
    start: datetime,
    end: datetime,
    all_day: bool = False,
    location: str = "",
    description: str = "",
    attendees: Iterable[str] = (),
) -> EventFields:
    return EventFields(
        title=(title.strip() or UNTITLED)[:TITLE_MAX],
        start=start,
        end=max(start, end),
        all_day=all_day,
        location=location[:LOCATION_MAX],
        description=description[:DESCRIPTION_MAX],
        attendees=clean_addresses(attendees),
    )


def same_content(left: EventFields, right: EventFields) -> bool:
    """Equal as a write would leave them (attendees are never written, so ignored)."""
    return (
        left.title == right.title
        and left.start == right.start
        and left.end == right.end
        and left.all_day == right.all_day
        and left.location == right.location
        and left.description == right.description
    )
