"""The Owl calendar through Thunderbird: contract suite against the fake bridge, and its limits."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from cal_bridge import FakeCalendarBridge
from cal_owl_rig import OwlRig

from chamberlain.connectors.fake.world import Fault
from chamberlain.connectors.thunderbird.calendar import (
    OwlBinding,
    OwlCalendarConfig,
    ThunderbirdCalendar,
)
from chamberlain.domain.calendar import CalendarQuery, EventFields
from chamberlain.domain.capabilities import CapabilityName
from chamberlain.domain.handles import HandleKind, ref_handle
from chamberlain.domain.intents import CalendarCreate, CalendarDelete, CalendarUpdate
from chamberlain.domain.operations import DispatchOutcome, ReconcileStatus
from chamberlain.ports.calendar import CalendarPort
from chamberlain.ports.clock import ManualClock
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.testing.calendar_contract import CalendarPortContract
from chamberlain.testing.rigs import CalendarRig

SOURCE = "calendar:test"
START = datetime(2027, 3, 1, 10, 0, tzinfo=UTC)


def build(**bridge_args: Any) -> tuple[ThunderbirdCalendar, FakeCalendarBridge, ManualClock]:
    clock = ManualClock()
    bridge = FakeCalendarBridge(clock, **bridge_args)
    config = OwlCalendarConfig(bindings={SOURCE: OwlBinding(calendar_id=bridge.calendar_id)})
    return ThunderbirdCalendar(config, bridge, clock), bridge, clock


def fields(title: str = "Event", **extra: Any) -> EventFields:
    return EventFields.model_validate(
        {"title": title, "start": START, "end": START + timedelta(minutes=30)} | extra
    )


class TestOwlContract(CalendarPortContract):
    @pytest.fixture
    def rig(self) -> CalendarRig:
        port, bridge, clock = build()
        assert isinstance(port, CalendarPort)
        return OwlRig(port, bridge, clock)


def test_the_capability_set_is_narrow_and_honest() -> None:
    port, _, _ = build()
    declared = port.capabilities(SOURCE)
    write = declared.get(CapabilityName.CALENDAR_WRITE)
    assert write is not None
    assert write.level.value == "implemented"
    assert not write.absence_provable


def test_the_version_handshake_withholds_capabilities_from_a_bridge_without_calendar() -> None:
    for args in ({"version": 1}, {"capabilities": frozenset({"read"})}):
        port, _, _ = build(**args)
        assert port.capabilities(SOURCE).names() == []
        assert port.health(SOURCE).state.value == "unavailable"
    port, bridge, _ = build()
    bridge.offline = True
    with pytest.raises(ChamberlainError) as raised:
        port.capabilities(SOURCE)
    assert raised.value.code is ErrorCode.SOURCE_UNAVAILABLE
    assert "add-on" in raised.value.fix
    with pytest.raises(ChamberlainError):
        port.events(SOURCE, CalendarQuery(start=START, end=START + timedelta(days=1)))


def test_p5_a_client_cache_is_stale_unless_it_says_when_it_last_synchronised() -> None:
    port, bridge, clock = build()
    assert port.health(SOURCE).state.value == "stale"
    bridge.fresh_as_of = clock.now().isoformat()
    health = port.health(SOURCE)
    assert health.state.value == "ok"
    assert health.fresh_as_of is not None


def test_p16_events_with_attendees_or_an_organiser_are_refused_without_sending() -> None:
    port, bridge, _ = build()
    bridge.items["invite"] = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//x//EN\r\nBEGIN:VEVENT\r\nUID:invite\r\n"
        "DTSTAMP:20270101T000000Z\r\nDTSTART:20270301T100000Z\r\nDTEND:20270301T110000Z\r\n"
        "SUMMARY:Invite\r\nORGANIZER:mailto:boss@example.test\r\n"
        "ATTENDEE:mailto:me@example.test\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    current = port.read(SOURCE, "invite")
    assert current.fields.attendees == ("me@example.test",)
    handle = ref_handle(HandleKind.CALENDAR_EVENT, SOURCE, "invite")
    before = [r for r in bridge.requests if r[1]]
    for intent in (
        CalendarDelete(source=SOURCE, event_ref=handle, expected_etag=current.etag),
        CalendarUpdate(
            source=SOURCE, event_ref=handle, expected_etag=current.etag, event=fields("x")
        ),
    ):
        with pytest.raises(ChamberlainError) as raised:
            port.prepare(intent, {handle: "invite"})
        assert raised.value.code is ErrorCode.CAPABILITY_UNSUPPORTED
    invite = fields(attendees=("a@example.test",))
    with pytest.raises(ChamberlainError):
        port.prepare(CalendarCreate(source=SOURCE, event=invite, uid="n"), {})
    assert [r for r in bridge.requests if r[1]] == before


def test_p10_a_bridge_that_never_connected_is_not_applied_a_lost_answer_is_unknown() -> None:
    port, bridge, _ = build()
    prepared = port.prepare(CalendarCreate(source=SOURCE, event=fields(), uid="u1"), {})
    bridge.offline = True
    assert port.dispatch(prepared, "k").outcome is DispatchOutcome.NOT_APPLIED
    bridge.offline = False
    bridge.arm(Fault.LOSE_RESPONSE_AFTER_APPLY)
    assert port.dispatch(prepared, "k").outcome is DispatchOutcome.UNKNOWN
    settled = port.reconcile(prepared, "k")
    assert settled.status is ReconcileStatus.PRESENT
    assert settled.receipt
    assert not settled.receipt.verification.server_verified


def test_p12_an_absent_create_is_never_claimed_as_proven() -> None:
    port, _, _ = build()
    prepared = port.prepare(CalendarCreate(source=SOURCE, event=fields(), uid="u2"), {})
    settled = port.reconcile(prepared, "k")
    assert (settled.status, settled.proof) == (ReconcileStatus.ABSENT, False)


def test_p16_an_edit_by_the_user_after_prepare_stops_the_update() -> None:
    port, bridge, _ = build()
    created = port.prepare(CalendarCreate(source=SOURCE, event=fields("Mine"), uid="e1"), {})
    assert port.dispatch(created, "k0").outcome is DispatchOutcome.APPLIED
    current = port.read(SOURCE, "e1")
    handle = ref_handle(HandleKind.CALENDAR_EVENT, SOURCE, "e1")
    update = CalendarUpdate(
        source=SOURCE, event_ref=handle, expected_etag=current.etag, event=fields("New")
    )
    prepared = port.prepare(update, {handle: "e1"})
    bridge.items["e1"] = bridge.items["e1"].replace("Mine", "Theirs")
    assert port.dispatch(prepared, "k").outcome is DispatchOutcome.NOT_APPLIED
    assert port.reconcile(prepared, "k").status is ReconcileStatus.UNKNOWN
    assert port.read(SOURCE, "e1").fields.title == "Theirs"
