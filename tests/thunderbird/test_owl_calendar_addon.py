"""The Owl calendar connector against the real add-on under Node (bridge contract, calendar).

The shared `CalendarPortContract` runs in `test_contract.py`; these tests cover what only the
add-on can show: occurrences, the refusal of events with attendees, and what health says.
"""

from datetime import UTC, datetime, timedelta

import pytest
from thunderbird_rig import CALENDAR_ID, Stack

from chamberlain.connectors.calendar_support import ics
from chamberlain.domain.calendar import CalendarQuery, EventFields
from chamberlain.domain.capabilities import CapabilityName
from chamberlain.domain.handles import HandleKind, ref_handle
from chamberlain.domain.intents import CalendarCreate, CalendarUpdate
from chamberlain.schema.errors import ChamberlainError, ErrorCode

SOURCE = "calendar:test"
START = datetime(2027, 3, 1, 10, 0, tzinfo=UTC)
WINDOW = CalendarQuery(start=START - timedelta(days=1), end=START + timedelta(days=2))


def fields(title: str = "Event", **extra: object) -> EventFields:
    return EventFields.model_validate(
        {"title": title, "start": START, "end": START + timedelta(minutes=30)} | extra
    )


def test_the_connector_sees_the_add_ons_calendar_capability(stack: Stack) -> None:
    declared = stack.calendar.capabilities(SOURCE)
    assert declared.get(CapabilityName.CALENDAR_WRITE) is not None
    assert declared.get(CapabilityName.CALENDAR_READ) is not None


def test_p5_health_is_stale_because_the_client_cannot_say_when_it_synchronised(
    stack: Stack,
) -> None:
    assert stack.calendar.health(SOURCE).state.value == "stale"


def test_an_unknown_calendar_binding_is_not_ok(stack: Stack) -> None:
    other = stack.calendar_for("no-such-calendar")
    assert other.health(SOURCE).state.value == "unavailable"


def test_occurrences_of_a_series_come_back_with_their_recurrence_id(stack: Stack) -> None:
    master = ics.build_new("series", fields("Weekly"), START)
    second = START + timedelta(days=1)
    occurrence = ics.build_new(
        "series", fields("Weekly", start=second, end=second + timedelta(minutes=30)), START
    )
    stack.control(
        "calendar_occurrence",
        calendar_id=CALENDAR_ID,
        id="series",
        recurrence_id=second.strftime("%Y%m%dT%H%M%SZ"),
        icalendar=occurrence,
    )
    stack.control("calendar_seed", calendar_id=CALENDAR_ID, id="series", icalendar=master)
    page = stack.calendar.events(SOURCE, WINDOW)
    assert [e.locator for e in page.items] == ["series#" + second.strftime("%Y%m%dT%H%M%SZ")]
    found = stack.calendar.read(SOURCE, page.items[0].locator)
    assert found.fields.start == second


def test_p16_an_event_with_an_organiser_is_refused_before_anything_is_sent(stack: Stack) -> None:
    text = ics.build_new("held", fields("Meeting"), START).replace(
        "END:VEVENT", "ORGANIZER:mailto:boss@example.test\r\nEND:VEVENT"
    )
    stack.control("calendar_seed", calendar_id=CALENDAR_ID, id="held", icalendar=text)
    current = stack.calendar.read(SOURCE, "held")
    handle = ref_handle(HandleKind.CALENDAR_EVENT, SOURCE, "held")
    update = CalendarUpdate(
        source=SOURCE, event_ref=handle, expected_etag=current.etag, event=fields("Moved")
    )
    with pytest.raises(ChamberlainError) as raised:
        stack.calendar.prepare(update, {handle: "held"})
    assert raised.value.code is ErrorCode.CAPABILITY_UNSUPPORTED
    assert stack.control("calendar_text", calendar_id=CALENDAR_ID, id="held") == text


def test_a_create_with_attendees_is_refused_by_the_connector(stack: Stack) -> None:
    intent = CalendarCreate(source=SOURCE, event=fields(attendees=("a@example.test",)), uid="inv")
    with pytest.raises(ChamberlainError):
        stack.calendar.prepare(intent, {})
    assert stack.event_count("inv") == 0
