"""CalDAV invitations against real Radicale: the resource text a server would schedule from.

Radicale stores and returns what is PUT. It does not do RFC 6638 scheduling: it sends no
invitation, delivers nothing to an invitee's calendar, does not carry an invitee's answer back
to the organiser's copy and does not remove a cancelled event from an invitee's calendar.
These tests prove the ICS the connector writes, the conditional writes and the read-back.
Delivery is for the live suite.
"""

from datetime import UTC, datetime, timedelta

import pytest
from cal_radicale import RadicaleServer
from cal_rigs import CALDAV_ADDRESS, CalDavRig
from test_caldav_contract import basic, build

from chamberlain.connectors.caldav.calendar import CalDavCalendar, own_address
from chamberlain.connectors.caldav.config import CalDavBinding, CalDavConfig
from chamberlain.connectors.calendar_support.http import UrllibTransport
from chamberlain.connectors.fake.world import Fault
from chamberlain.domain.calendar import EventFields, Response
from chamberlain.domain.capabilities import CapabilityName
from chamberlain.domain.handles import HandleKind, ref_handle
from chamberlain.domain.intents import (
    CalendarCreate,
    CalendarDelete,
    CalendarRespond,
    CalendarUpdate,
)
from chamberlain.domain.operations import DispatchOutcome, ReconcileStatus
from chamberlain.ports.clock import ManualClock
from chamberlain.schema.errors import ChamberlainError, ErrorCode

SOURCE = "calendar:test"
START = datetime(2027, 3, 1, 10, 0, tzinfo=UTC)
GUEST = "guest@example.test"
BOSS = "boss@example.test"
World = tuple[CalDavCalendar, CalDavRig]


def fields(*guests: str, title: str = "Invitation", start: datetime = START) -> EventFields:
    return EventFields(
        title=title, start=start, end=start + timedelta(minutes=30), attendees=guests
    )


@pytest.fixture
def world(radicale: RadicaleServer) -> World:
    clock = ManualClock()
    port, transport, collection = build(radicale, clock)
    return port, CalDavRig(port, transport, clock, collection, basic())


def stored_text(rig: CalDavRig, name: str) -> str:
    reply = rig.transport.inner.send(
        "GET", rig.collection + name, headers={"Authorization": basic()}, body=None, timeout=10
    )
    return reply.body.decode().replace("\r\n ", "")


def put(rig: CalDavRig, name: str, text: str) -> None:
    reply = rig.transport.inner.send(
        "PUT",
        rig.collection + name,
        headers={"Authorization": basic(), "Content-Type": "text/calendar"},
        body=text.encode(),
        timeout=10,
    )
    assert reply.status in (201, 204), reply.body


def invite(port: CalDavCalendar, uid: str, event: EventFields) -> str:
    prepared = port.prepare(CalendarCreate(source=SOURCE, event=event, uid=uid), {})
    result = port.dispatch(prepared, "k-" + uid)
    assert result.outcome is DispatchOutcome.APPLIED, result.detail
    assert result.receipt is not None
    assert result.receipt.locator is not None
    return result.receipt.locator


def received(rig: CalDavRig, name: str, *, mine: str = "NEEDS-ACTION", sequence: int = 3) -> None:
    put(
        rig,
        name,
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//other client//EN\r\nBEGIN:VEVENT\r\n"
        f"UID:{name}@t\r\nDTSTAMP:20270101T000000Z\r\nDTSTART:20270301T100000Z\r\n"
        f"DTEND:20270301T110000Z\r\nSUMMARY:Planning\r\nSEQUENCE:{sequence}\r\n"
        "X-KEEP-ME:yes\r\n"
        f"ORGANIZER;CN=Boss:mailto:{BOSS}\r\n"
        f"ATTENDEE;PARTSTAT=ACCEPTED:mailto:{BOSS}\r\n"
        f"ATTENDEE;PARTSTAT={mine};RSVP=TRUE:mailto:{CALDAV_ADDRESS}\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n",
    )


def test_w33_an_invitation_names_the_organiser_and_each_guest(world: World) -> None:
    port, rig = world
    name = invite(port, "uid-1@t", fields(GUEST, "Second@Example.test"))
    text = stored_text(rig, name)
    assert f"ORGANIZER:mailto:{CALDAV_ADDRESS}" in text
    assert (
        f"ATTENDEE;CUTYPE=INDIVIDUAL;PARTSTAT=ACCEPTED;ROLE=REQ-PARTICIPANT:mailto:{CALDAV_ADDRESS}"
        in text
    )
    assert (
        f"ATTENDEE;CUTYPE=INDIVIDUAL;PARTSTAT=NEEDS-ACTION;ROLE=REQ-PARTICIPANT;RSVP=TRUE:mailto:{GUEST}"
        in text
    )
    assert "mailto:second@example.test" in text
    assert "METHOD" not in text  # the server schedules; a stored event carries no iTIP method
    assert "SEQUENCE:0" in text


def test_w36_the_organiser_reads_the_guests_without_itself(world: World) -> None:
    port, rig = world
    name = invite(port, "uid-2@t", fields(GUEST))
    event = port.read(SOURCE, name)
    assert event.fields.attendees == (GUEST,)
    assert event.organiser is None
    assert event.my_response is None
    assert [(p.address, p.response) for p in event.participants] == [(GUEST, Response.NEEDS_ACTION)]
    rig_text = stored_text(rig, name).replace(
        f"PARTSTAT=NEEDS-ACTION;ROLE=REQ-PARTICIPANT;RSVP=TRUE:mailto:{GUEST}",
        f"PARTSTAT=ACCEPTED;ROLE=REQ-PARTICIPANT;RSVP=TRUE:mailto:{GUEST}",
    )
    put(rig, name, rig_text)  # what an iCloud server does when the guest answers
    assert port.read(SOURCE, name).participants[0].response is Response.ACCEPTED


def test_w34_without_an_address_the_calendar_does_not_declare_invitations(
    radicale: RadicaleServer,
) -> None:
    clock = ManualClock()
    collection = radicale.new_calendar()
    binding = CalDavBinding(url=collection, username="alice", credential="c")
    config = CalDavConfig(bindings={SOURCE: binding})
    port = CalDavCalendar(config, lambda b: "radicale-test-password", clock, UrllibTransport())
    assert own_address(binding) is None
    assert port.capabilities(SOURCE).get(CapabilityName.CALENDAR_INVITE) is None
    with pytest.raises(ChamberlainError) as raised:
        port.prepare(CalendarCreate(source=SOURCE, event=fields(GUEST), uid="x@t"), {})
    assert raised.value.code is ErrorCode.CAPABILITY_UNSUPPORTED
    assert raised.value.detail == "invitations-not-supported"
    # An event without guests is written as before.
    plain = port.prepare(CalendarCreate(source=SOURCE, event=fields(), uid="y@t"), {})
    assert port.dispatch(plain, "k").outcome is DispatchOutcome.APPLIED


def test_w34_a_username_that_is_an_address_is_used_when_none_is_set() -> None:
    binding = CalDavBinding(
        url="https://example.test/c/", username="Me@Example.test", credential="c"
    )
    assert own_address(binding) == "me@example.test"


def test_w33_a_change_keeps_answers_and_raises_the_sequence_unless_the_time_moves(
    world: World,
) -> None:
    port, rig = world
    name = invite(port, "uid-3@t", fields(GUEST))
    answered = stored_text(rig, name).replace(
        f"PARTSTAT=NEEDS-ACTION;ROLE=REQ-PARTICIPANT;RSVP=TRUE:mailto:{GUEST}",
        f"PARTSTAT=ACCEPTED;ROLE=REQ-PARTICIPANT;RSVP=TRUE:mailto:{GUEST}",
    )
    put(rig, name, answered)
    handle = ref_handle(HandleKind.CALENDAR_EVENT, SOURCE, name)

    def change(event: EventFields) -> None:
        etag = port.read(SOURCE, name).etag
        update = CalendarUpdate(source=SOURCE, event_ref=handle, expected_etag=etag, event=event)
        prepared = port.prepare(update, {handle: name})
        assert port.dispatch(prepared, "k").outcome is DispatchOutcome.APPLIED

    change(fields(GUEST, title="Renamed"))
    text = stored_text(rig, name)
    assert f"PARTSTAT=ACCEPTED;ROLE=REQ-PARTICIPANT;RSVP=TRUE:mailto:{GUEST}" in text
    assert "SEQUENCE:1" in text
    change(fields(GUEST, title="Renamed", start=START + timedelta(hours=2)))
    assert f"PARTSTAT=NEEDS-ACTION;ROLE=REQ-PARTICIPANT;RSVP=TRUE:mailto:{GUEST}" in stored_text(
        rig, name
    )


def test_w33_taking_every_guest_off_turns_it_back_into_a_plain_event(world: World) -> None:
    port, rig = world
    name = invite(port, "uid-4@t", fields(GUEST))
    handle = ref_handle(HandleKind.CALENDAR_EVENT, SOURCE, name)
    etag = port.read(SOURCE, name).etag
    update = CalendarUpdate(source=SOURCE, event_ref=handle, expected_etag=etag, event=fields())
    assert (
        port.dispatch(port.prepare(update, {handle: name}), "k").outcome is DispatchOutcome.APPLIED
    )
    text = stored_text(rig, name)
    assert "ORGANIZER" not in text
    assert "ATTENDEE" not in text


def test_w33_the_organiser_cancels_by_deleting_its_copy(world: World) -> None:
    port, rig = world
    name = invite(port, "uid-5@t", fields(GUEST))
    handle = ref_handle(HandleKind.CALENDAR_EVENT, SOURCE, name)
    delete = CalendarDelete(
        source=SOURCE, event_ref=handle, expected_etag=port.read(SOURCE, name).etag
    )
    prepared = port.prepare(delete, {handle: name})
    assert port.dispatch(prepared, "k").outcome is DispatchOutcome.APPLIED
    assert rig.count("uid-5@t") == 0


def test_w36_a_read_back_without_the_guests_is_not_verified(world: World) -> None:
    port, rig = world
    prepared = port.prepare(CalendarCreate(source=SOURCE, event=fields(GUEST), uid="lossy@t"), {})
    real = rig.transport.inner.send
    seen: dict[str, bool] = {"stripped": False}

    def strip_guests(method: str, url: str, **kwargs: object):  # type: ignore[no-untyped-def]
        reply = real(method, url, **kwargs)  # type: ignore[arg-type]
        if method == "GET" and reply.body and not seen["stripped"]:
            seen["stripped"] = True
            lines = [
                line
                for line in reply.body.decode().replace("\r\n ", "").split("\r\n")
                if not line.startswith(("ATTENDEE", "ORGANIZER"))
            ]
            return type(reply)(reply.status, reply.headers, "\r\n".join(lines).encode())
        return reply

    rig.transport.inner.send = strip_guests  # type: ignore[method-assign]
    result = port.dispatch(prepared, "k")
    rig.transport.inner.send = real  # type: ignore[method-assign]
    assert result.outcome is DispatchOutcome.UNKNOWN
    assert port.reconcile(prepared, "k").status is ReconcileStatus.PRESENT


def test_w33_a_received_invitation_reads_with_its_organiser_and_my_answer(world: World) -> None:
    port, rig = world
    received(rig, "boss.ics")
    event = port.read(SOURCE, "boss.ics")
    assert event.organiser == BOSS
    assert event.my_response is Response.NEEDS_ACTION
    assert event.fields.attendees == (BOSS,)


def test_w33_answering_changes_only_my_partstat_and_leaves_the_sequence(world: World) -> None:
    port, rig = world
    received(rig, "boss.ics")
    handle = ref_handle(HandleKind.CALENDAR_EVENT, SOURCE, "boss.ics")
    etag = port.read(SOURCE, "boss.ics").etag
    answer = CalendarRespond(
        source=SOURCE,
        event_ref=handle,
        expected_etag=etag,
        response="declined",
        organiser=BOSS,
    )
    prepared = port.prepare(answer, {handle: "boss.ics"})
    assert port.dispatch(prepared, "k").outcome is DispatchOutcome.APPLIED
    text = stored_text(rig, "boss.ics")
    assert f"PARTSTAT=DECLINED;RSVP=TRUE:mailto:{CALDAV_ADDRESS}" in text
    assert f"PARTSTAT=ACCEPTED:mailto:{BOSS}" in text
    assert "SEQUENCE:3" in text
    assert "X-KEEP-ME:yes" in text
    assert "ORGANIZER" in text
    assert port.read(SOURCE, "boss.ics").my_response is Response.DECLINED
    assert port.reconcile(prepared, "k").status is ReconcileStatus.PRESENT


def test_w36_a_stale_answer_is_refused_and_a_plain_event_cannot_be_answered(world: World) -> None:
    port, rig = world
    received(rig, "boss.ics")
    handle = ref_handle(HandleKind.CALENDAR_EVENT, SOURCE, "boss.ics")
    stale = CalendarRespond(
        source=SOURCE, event_ref=handle, expected_etag="x", response="accepted", organiser=BOSS
    )
    with pytest.raises(ChamberlainError) as raised:
        port.prepare(stale, {handle: "boss.ics"})
    assert raised.value.code is ErrorCode.REMOTE_CHANGED
    plain = invite(port, "uid-6@t", fields())
    plain_handle = ref_handle(HandleKind.CALENDAR_EVENT, SOURCE, plain)
    own = CalendarRespond(
        source=SOURCE,
        event_ref=plain_handle,
        expected_etag=port.read(SOURCE, plain).etag,
        response="accepted",
        organiser=BOSS,
    )
    with pytest.raises(ChamberlainError) as invalid:
        port.prepare(own, {plain_handle: plain})
    assert invalid.value.code is ErrorCode.REQUEST_INVALID


def test_w8_an_answer_that_never_landed_is_proven_absent(world: World) -> None:
    port, rig = world
    received(rig, "boss.ics")
    handle = ref_handle(HandleKind.CALENDAR_EVENT, SOURCE, "boss.ics")
    answer = CalendarRespond(
        source=SOURCE,
        event_ref=handle,
        expected_etag=port.read(SOURCE, "boss.ics").etag,
        response="accepted",
        organiser=BOSS,
    )
    prepared = port.prepare(answer, {handle: "boss.ics"})
    rig.inject(Fault.LOSE_BEFORE_APPLY)
    assert port.dispatch(prepared, "k").outcome is DispatchOutcome.UNKNOWN
    settled = port.reconcile(prepared, "k")
    assert (settled.status, settled.proof) == (ReconcileStatus.ABSENT, True)


def test_w33_an_event_we_organise_with_guests_can_be_changed(world: World) -> None:
    port, rig = world
    put(
        rig,
        "mine.ics",
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//other client//EN\r\nBEGIN:VEVENT\r\n"
        "UID:mine@t\r\nDTSTAMP:20270101T000000Z\r\nDTSTART:20270301T100000Z\r\n"
        "DTEND:20270301T110000Z\r\nSUMMARY:Mine\r\n"
        f"ORGANIZER:mailto:{CALDAV_ADDRESS}\r\nATTENDEE:mailto:{GUEST}\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n",
    )
    handle = ref_handle(HandleKind.CALENDAR_EVENT, SOURCE, "mine.ics")
    etag = port.read(SOURCE, "mine.ics").etag
    update = CalendarUpdate(
        source=SOURCE,
        event_ref=handle,
        expected_etag=etag,
        event=fields(GUEST, title="Mine, renamed"),
    )
    prepared = port.prepare(update, {handle: "mine.ics"})
    assert port.dispatch(prepared, "k").outcome is DispatchOutcome.APPLIED


def test_a_changed_date_of_a_series_with_guests_keeps_its_guests() -> None:
    """Owner ruling 2026-11-24: series may invite. An override without ATTENDEE would tell the
    server that date has no guests, and it would send them a cancellation."""
    from chamberlain.connectors.calendar_support import ics
    from chamberlain.domain.recurrence import expand, occurrence_id

    start = datetime(2027, 3, 1, 9, tzinfo=UTC)
    fields = EventFields.model_validate(
        {
            "title": "Weekly",
            "start": start,
            "end": start + timedelta(minutes=30),
            "attendees": ["guest@example.org"],
            "recurrence": {"frequency": "weekly", "count": 3, "zone": "Europe/London"},
        }
    )
    text = ics.build_new("uid-1", fields, start, organiser=CALDAV_ADDRESS)
    assert fields.recurrence is not None
    dates = expand(
        fields.recurrence,
        fields.start,
        fields.end,
        all_day=False,
        window_start=start,
        window_end=start + timedelta(days=30),
    )
    second = occurrence_id(dates[1][0], all_day=False)
    moved = fields.model_copy(update={"location": "Room 2", "recurrence": None})
    changed = ics.apply_occurrence(text, second, moved, start)
    override = next(p for p in ics.parse_events(changed) if p.recurrence_id is not None)
    assert override.organiser_address == CALDAV_ADDRESS.lower()
    assert "guest@example.org" in {a for a, _ in override.partstats}
