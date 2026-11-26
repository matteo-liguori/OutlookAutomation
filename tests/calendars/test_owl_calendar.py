"""The Owl calendar through Thunderbird: contract suite against the fake bridge, and its limits."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from cal_bridge import FakeCalendarBridge
from cal_owl_rig import OwlRig

from chamberlain.connectors.calendar_support import ics
from chamberlain.connectors.fake.world import Fault
from chamberlain.connectors.thunderbird.calendar import (
    CalendarBridgeFailure,
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


def test_p5_p16_the_capability_set_is_narrow_and_honest_and_3_related_checks() -> None:
    # the_capability_set_is_narrow_and_honest
    port, _, _ = build()
    declared = port.capabilities(SOURCE)
    write = declared.get(CapabilityName.CALENDAR_WRITE)
    assert write is not None
    assert write.level.value == "implemented"
    assert not write.absence_provable

    # the_version_handshake_withholds_capabilities_from_a_bridge_without_calendar
    for args in ({"version": 1}, {"calendar_available": False}):
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

    # p5_a_client_cache_is_stale_unless_it_says_when_it_last_synchronised
    port, bridge, clock = build()
    assert port.health(SOURCE).state.value == "stale"
    bridge.fresh_as_of = clock.now().isoformat()
    health = port.health(SOURCE)
    assert health.state.value == "ok"
    assert health.fresh_as_of is not None

    # p16_events_with_attendees_or_an_organiser_are_refused_without_sending
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


def test_p16_private_appointment_has_no_guests_and_keeps_its_class_on_update() -> None:
    port, bridge, _ = build()
    created = port.prepare(
        CalendarCreate(source=SOURCE, event=fields(visibility="private"), uid="private"), {}
    )
    assert port.dispatch(created, "create").outcome is DispatchOutcome.APPLIED
    current = port.read(SOURCE, "private")
    assert current.fields.visibility == "private"
    assert not current.fields.attendees
    assert "CLASS:PRIVATE\r\n" in bridge.items["private"]
    assert "ATTENDEE" not in bridge.items["private"]
    assert "ORGANIZER" not in bridge.items["private"]

    handle = ref_handle(HandleKind.CALENDAR_EVENT, SOURCE, "private")
    updated = port.prepare(
        CalendarUpdate(
            source=SOURCE, event_ref=handle, expected_etag=current.etag, event=fields("New")
        ),
        {handle: "private"},
    )
    assert port.dispatch(updated, "update").outcome is DispatchOutcome.APPLIED
    assert port.read(SOURCE, "private").fields.visibility == "private"


def test_p11_private_create_stays_unknown_if_the_provider_drops_classification() -> None:
    port, bridge, _ = build()
    prepared = port.prepare(
        CalendarCreate(source=SOURCE, event=fields(visibility="private"), uid="private"), {}
    )
    bridge.arm(Fault.LOSE_RESPONSE_AFTER_APPLY)
    assert port.dispatch(prepared, "k").outcome is DispatchOutcome.UNKNOWN
    bridge.items["private"] = bridge.items["private"].replace("CLASS:PRIVATE\r\n", "")
    assert port.reconcile(prepared, "k").status is ReconcileStatus.UNKNOWN


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


def test_p16_provider_created_id_is_the_locator_for_update_and_delete() -> None:
    port, bridge, _ = build()
    bridge.generated_uid = "provider-uid"
    created = port.prepare(
        CalendarCreate(source=SOURCE, event=fields(visibility="private"), uid="submitted-uid"), {}
    )
    outcome = port.dispatch(created, "create")
    assert outcome.outcome is DispatchOutcome.APPLIED
    assert outcome.receipt
    assert outcome.receipt.locator == "provider-uid"
    assert "submitted-uid" not in bridge.items

    bridge.items["provider-uid"] = bridge.items["provider-uid"].replace(
        "CLASS:PRIVATE\r\n", "CLASS:PRIVATE\r\nORGANIZER:mailto:owner@example.test\r\n"
    )
    current = port.read(SOURCE, "provider-uid")
    handle = ref_handle(HandleKind.CALENDAR_EVENT, SOURCE, current.locator)
    intent = CalendarUpdate(
        source=SOURCE, event_ref=handle, expected_etag=current.etag, event=fields("Updated")
    )
    refs = {handle: current.locator}
    with pytest.raises(ChamberlainError) as refused:
        port.prepare(intent, refs)
    assert refused.value.code is ErrorCode.CAPABILITY_UNSUPPORTED
    bridge.own_appointments.add(current.locator)
    updated = port.prepare(intent, refs)
    assert updated.data["expected_icalendar"] == bridge.items[current.locator]
    assert port.dispatch(updated, "update").outcome is DispatchOutcome.APPLIED
    after = port.read(SOURCE, current.locator)
    assert after.fields.title == "Updated"
    assert after.fields.visibility == "private"
    deleted = port.prepare(
        CalendarDelete(source=SOURCE, event_ref=handle, expected_etag=after.etag), refs
    )
    assert port.dispatch(deleted, "delete").outcome is DispatchOutcome.APPLIED
    assert not bridge.items


def test_p11_lost_provider_uid_is_not_recovered_from_an_unrelated_event() -> None:
    port, bridge, clock = build()
    bridge.generated_uid = "provider-uid"
    wanted = fields("Same title", visibility="private", description="Expected detail")
    created = port.prepare(CalendarCreate(source=SOURCE, event=wanted, uid="submitted-uid"), {})
    bridge.arm(Fault.LOSE_RESPONSE_AFTER_APPLY)
    assert port.dispatch(created, "create").outcome is DispatchOutcome.UNKNOWN
    assert "provider-uid" in bridge.items
    observed = port.reconcile(created, "create")
    assert observed.status is not ReconcileStatus.PRESENT
    assert not observed.proof

    bridge.items["submitted-uid"] = ics.build_new(
        "submitted-uid", fields("Same title", visibility="private"), clock.now()
    )
    assert port.reconcile(created, "create").status is ReconcileStatus.UNKNOWN
    assert set(bridge.items) == {"provider-uid", "submitted-uid"}


def test_p6_a_series_is_read_by_date_and_never_written() -> None:
    port, bridge, _ = build()
    master = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//x//EN\r\nBEGIN:VEVENT\r\nUID:series\r\n"
        "DTSTAMP:20270101T000000Z\r\nDTSTART:20270301T100000Z\r\nDTEND:20270301T103000Z\r\n"
        "RRULE:FREQ=DAILY;COUNT=3\r\nSUMMARY:Standup\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    bridge.items["series"] = master
    bridge.occurrences = [
        ("series", f"2027030{d}T100000Z", occurrence_text("series", d)) for d in (1, 2, 3)
    ]
    page = port.events(
        SOURCE, CalendarQuery(start=START - timedelta(days=1), end=START + timedelta(days=5))
    )
    assert [e.locator for e in page.items] == [
        "series#20270301T100000Z",
        "series#20270302T100000Z",
        "series#20270303T100000Z",
    ]
    for listed in page.items:
        assert port.read(SOURCE, listed.locator) == listed
        assert listed.is_occurrence
        assert listed.series_locator == "series"
    series = port.read(SOURCE, "series")
    assert series.is_series
    assert series.fields.recurrence is not None
    declared = port.capabilities(SOURCE)  # reads occurrences; writes none (P-26)
    assert declared.get(CapabilityName.CALENDAR_RECURRENCE) is None
    assert declared.get(CapabilityName.CALENDAR_OCCURRENCE) is None
    handle = ref_handle(HandleKind.CALENDAR_EVENT, SOURCE, page.items[1].locator)
    delete = CalendarDelete(source=SOURCE, event_ref=handle, expected_etag=page.items[1].etag)
    with pytest.raises(ChamberlainError) as raised:
        port.prepare(delete, {handle: page.items[1].locator})
    assert raised.value.code is ErrorCode.CAPABILITY_UNSUPPORTED


def occurrence_text(uid: str, day: int) -> str:
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//x//EN\r\nBEGIN:VEVENT\r\n"
        f"UID:{uid}\r\nDTSTAMP:20270101T000000Z\r\nRECURRENCE-ID:2027030{day}T100000Z\r\n"
        f"DTSTART:2027030{day}T100000Z\r\nDTEND:2027030{day}T103000Z\r\nSUMMARY:Standup\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )


def test_p4_paging_through_the_limit_is_not_reported_as_a_coverage_gap() -> None:
    port, bridge, clock = build()
    for index in range(5):
        start = START + timedelta(hours=index)
        bridge.items[f"e{index}"] = ics.build_new(
            f"e{index}",
            fields(f"E{index}", start=start, end=start + timedelta(minutes=30)),
            clock.now(),
        )
    query = CalendarQuery(start=START - timedelta(days=1), end=START + timedelta(days=1), limit=2)
    titles: list[str] = []
    page = port.events(SOURCE, query)
    while True:
        titles += [e.fields.title for e in page.items]
        assert not page.gaps
        if not page.cursor:
            break
        page = port.events(SOURCE, query, page.cursor)
    assert page.complete
    assert titles == [f"E{i}" for i in range(5)]
    bridge.arm(Fault.STALE)
    assert port.events(SOURCE, query).gaps


def test_private_freshness_probe_and_lost_create_recovery() -> None:
    port, bridge, _ = build()
    token = "a" * 32
    assert port.probe(SOURCE, token)
    assert [cmd for cmd, write in bridge.requests if write] == ["calendar_write"] * 2
    bridge.arm(Fault.LOSE_RESPONSE_AFTER_APPLY)
    with pytest.raises(CalendarBridgeFailure):
        port.probe(SOURCE, token)
    uid = f"chamberlain-freshness-{token}@chamberlain.local"
    recovered = port.read(SOURCE, uid)
    assert recovered.fields.visibility == "private"
    assert not recovered.fields.attendees
    # Exchange may have assigned a new UID before the create reply was lost.
    del bridge.items[uid]
    bridge.items["exchange-uid"] = ics.build_new("exchange-uid", recovered.fields, START)
    assert port.probe(SOURCE, token, cleanup_only=True)
    assert not bridge.items
    written = sum(write for _, write in bridge.requests)
    assert port.probe(SOURCE, token, cleanup_only=True)
    assert sum(write for _, write in bridge.requests) == written
