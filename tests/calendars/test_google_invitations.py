"""Google invitations: insert with sendUpdates, change, cancel and answer (W-33 to W-36)."""

from datetime import UTC, datetime, timedelta

import pytest
from cal_fake_google import FakeGoogleApi
from cal_helpers import prepare_answer
from test_google_contract import build

from chamberlain.connectors.fake.world import Fault
from chamberlain.connectors.google_calendar.calendar import GoogleCalendar, event_id_for
from chamberlain.domain.calendar import EventFields, Response
from chamberlain.domain.handles import HandleKind, ref_handle
from chamberlain.domain.intents import (
    CalendarCreate,
    CalendarDelete,
    CalendarUpdate,
)
from chamberlain.domain.operations import DispatchOutcome, Prepared, ReconcileStatus
from chamberlain.ports.clock import ManualClock
from chamberlain.schema.errors import ChamberlainError

SOURCE = "calendar:test"
START = datetime(2027, 3, 1, 10, 0, tzinfo=UTC)
GUEST = "guest@example.test"
World = tuple[GoogleCalendar, FakeGoogleApi, ManualClock]


def fields(*guests: str, title: str = "Invitation") -> EventFields:
    return EventFields(
        title=title, start=START, end=START + timedelta(minutes=30), attendees=guests
    )


@pytest.fixture
def world() -> World:
    clock = ManualClock()
    port, api = build(clock)
    return port, api, clock


def invite(port: GoogleCalendar, uid: str, *guests: str) -> tuple[Prepared, str]:
    prepared = port.prepare(CalendarCreate(source=SOURCE, event=fields(*guests), uid=uid), {})
    result = port.dispatch(prepared, "k-" + uid)
    assert result.outcome is DispatchOutcome.APPLIED, result.detail
    assert result.receipt is not None
    assert result.receipt.locator is not None
    return prepared, result.receipt.locator


def mutations(api: FakeGoogleApi) -> list[tuple[str, str, dict[str, str]]]:
    return [p for p in api.params if p[0] != "GET"]


def test_w33_an_invitation_is_inserted_with_send_updates_not_imported(world: World) -> None:
    port, api, _ = world
    prepared, locator = invite(port, "uid-one@t", GUEST)
    (method, path, params) = mutations(api)[-1]
    assert method == "POST"
    assert not path.endswith("/import")
    assert params == {"sendUpdates": "all"}
    sent = api.bodies[-1]
    assert sent["id"] == event_id_for("uid-one@t")
    assert sent["attendees"] == [{"email": GUEST}]
    stored = api.events[locator]
    assert [a["email"] for a in stored["attendees"]] == [api.owner, GUEST]
    assert prepared.reconcile_token == "uid-one@t"


def test_w33_an_event_without_guests_is_still_imported_and_sends_nothing(world: World) -> None:
    port, api, _ = world
    prepared = port.prepare(CalendarCreate(source=SOURCE, event=fields(), uid="plain@t"), {})
    assert port.dispatch(prepared, "k").outcome is DispatchOutcome.APPLIED
    method, path, params = mutations(api)[-1]
    assert (method, path.endswith("/import"), params) == ("POST", True, {})


def test_w36_the_read_back_carries_the_guest_list_and_the_organiser_sees_answers(
    world: World,
) -> None:
    port, api, _ = world
    _, locator = invite(port, "uid-two@t", GUEST, "second@example.test")
    api.events[locator]["attendees"][1]["responseStatus"] = "accepted"
    event = port.read(SOURCE, locator)
    assert event.fields.attendees == (GUEST, "second@example.test")
    assert event.organiser is None
    assert event.my_response is None
    assert [(p.address, p.response) for p in event.participants] == [
        (GUEST, Response.ACCEPTED),
        ("second@example.test", Response.NEEDS_ACTION),
    ]


def test_w8_an_invitation_that_never_landed_is_proven_absent_and_a_repeat_is_refused(
    world: World,
) -> None:
    port, api, _ = world
    prepared = port.prepare(CalendarCreate(source=SOURCE, event=fields(GUEST), uid="lost@t"), {})
    api.arm(Fault.LOSE_BEFORE_APPLY)
    assert port.dispatch(prepared, "k1").outcome is DispatchOutcome.UNKNOWN
    settled = port.reconcile(prepared, "k1")
    assert settled.status is ReconcileStatus.ABSENT
    assert settled.proof
    assert port.dispatch(prepared, "k2").outcome is DispatchOutcome.APPLIED
    assert port.dispatch(prepared, "k3").outcome is DispatchOutcome.NOT_APPLIED
    assert port.reconcile(prepared, "k2").status is ReconcileStatus.PRESENT
    assert len(api.events) == 1


def test_w8_a_lost_reply_to_an_invitation_settles_present(world: World) -> None:
    port, api, _ = world
    prepared = port.prepare(CalendarCreate(source=SOURCE, event=fields(GUEST), uid="reply@t"), {})
    api.arm(Fault.LOSE_RESPONSE_AFTER_APPLY)
    assert port.dispatch(prepared, "k").outcome is DispatchOutcome.UNKNOWN
    assert port.reconcile(prepared, "k").status is ReconcileStatus.PRESENT


def test_w33_a_change_keeps_the_answers_already_given_and_notifies(world: World) -> None:
    port, api, _ = world
    _, locator = invite(port, "uid-three@t", GUEST)
    api.events[locator]["attendees"][1]["responseStatus"] = "accepted"
    handle = ref_handle(HandleKind.CALENDAR_EVENT, SOURCE, locator)
    etag = port.read(SOURCE, locator).etag
    wanted = fields(GUEST, "new@example.test", title="Moved")
    update = CalendarUpdate(source=SOURCE, event_ref=handle, expected_etag=etag, event=wanted)
    prepared = port.prepare(update, {handle: locator})
    assert port.dispatch(prepared, "k-up").outcome is DispatchOutcome.APPLIED
    stored = api.events[locator]
    by_mail = {a["email"]: a for a in stored["attendees"]}
    assert by_mail[GUEST]["responseStatus"] == "accepted"
    assert by_mail["new@example.test"] == {"email": "new@example.test"}
    assert by_mail[api.owner]["self"] is True
    assert stored["organizer"] == {"email": api.owner, "self": True}
    assert mutations(api)[-1][2] == {"sendUpdates": "all"}


def test_w33_removing_every_guest_drops_the_attendee_list(world: World) -> None:
    port, api, _ = world
    _, locator = invite(port, "uid-four@t", GUEST)
    handle = ref_handle(HandleKind.CALENDAR_EVENT, SOURCE, locator)
    etag = port.read(SOURCE, locator).etag
    update = CalendarUpdate(source=SOURCE, event_ref=handle, expected_etag=etag, event=fields())
    prepared = port.prepare(update, {handle: locator})
    assert port.dispatch(prepared, "k-up").outcome is DispatchOutcome.APPLIED
    assert "attendees" not in api.events[locator]
    assert mutations(api)[-1][2] == {"sendUpdates": "all"}


def test_w33_cancelling_an_invitation_sends_the_cancellation(world: World) -> None:
    port, api, _ = world
    _, locator = invite(port, "uid-five@t", GUEST)
    handle = ref_handle(HandleKind.CALENDAR_EVENT, SOURCE, locator)
    etag = port.read(SOURCE, locator).etag
    prepared = port.prepare(
        CalendarDelete(source=SOURCE, event_ref=handle, expected_etag=etag), {handle: locator}
    )
    assert port.dispatch(prepared, "k-del").outcome is DispatchOutcome.APPLIED
    assert mutations(api)[-1] == (
        "DELETE",
        f"/calendar/v3/calendars/{api.calendar_id}/events/{locator}",
        {"sendUpdates": "all"},
    )


def test_w33_an_event_without_guests_is_deleted_without_notifying(world: World) -> None:
    port, api, _ = world
    locator = api.seed(
        {
            "summary": "Solo",
            "start": {"dateTime": "2027-03-01T10:00:00Z"},
            "end": {"dateTime": "2027-03-01T11:00:00Z"},
        }
    )
    handle = ref_handle(HandleKind.CALENDAR_EVENT, SOURCE, locator)
    etag = port.read(SOURCE, locator).etag
    prepared = port.prepare(
        CalendarDelete(source=SOURCE, event_ref=handle, expected_etag=etag), {handle: locator}
    )
    port.dispatch(prepared, "k")
    assert mutations(api)[-1][2] == {}


def seed_received(api: FakeGoogleApi, status: str = "needsAction") -> str:
    return api.seed(
        {
            "summary": "Planning",
            "start": {"dateTime": "2027-03-01T10:00:00Z"},
            "end": {"dateTime": "2027-03-01T11:00:00Z"},
            "organizer": {"email": "Boss@Example.test"},
            "attendees": [
                {"email": "boss@example.test", "organizer": True, "responseStatus": "accepted"},
                {"email": "peer@example.test", "responseStatus": "needsAction", "comment": "hi"},
                {"email": api.owner, "self": True, "responseStatus": status},
            ],
        }
    )


def test_w33_answering_patches_only_my_entry_and_tells_the_organiser(world: World) -> None:
    port, api, _ = world
    locator = seed_received(api)
    prepared = prepare_answer(port, SOURCE, locator, "boss@example.test", "declined")
    assert port.dispatch(prepared, "k-answer").outcome is DispatchOutcome.APPLIED
    method, _, params = mutations(api)[-1]
    assert (method, params) == ("PATCH", {"sendUpdates": "all"})
    sent = api.bodies[-1]
    assert set(sent) == {"attendees"}
    assert len(sent["attendees"]) == 3
    assert sent["attendees"][1]["comment"] == "hi"
    stored = api.events[locator]
    assert stored["summary"] == "Planning"
    assert port.read(SOURCE, locator).my_response is Response.DECLINED
    assert port.reconcile(prepared, "k-answer").status is ReconcileStatus.PRESENT


def test_w33_an_invitation_that_lists_only_me_as_guest_can_be_answered(world: World) -> None:
    """Google's copy when the organiser is not a guest (found live, 2026-11-25)."""
    port, api, _ = world
    locator = api.seed(
        {
            "summary": "Just me",
            "start": {"dateTime": "2027-03-02T10:00:00Z"},
            "end": {"dateTime": "2027-03-02T11:00:00Z"},
            "organizer": {"email": "boss@example.test"},
            "attendees": [{"email": api.owner, "self": True, "responseStatus": "needsAction"}],
        }
    )
    prepared = prepare_answer(port, SOURCE, locator, "boss@example.test", "accepted")
    assert port.dispatch(prepared, "k-only-me").outcome is DispatchOutcome.APPLIED
    assert port.read(SOURCE, locator).my_response is Response.ACCEPTED


@pytest.mark.parametrize(
    ("fault", "expected"),
    [
        (Fault.LOSE_RESPONSE_AFTER_APPLY, ReconcileStatus.PRESENT),
        (Fault.LOSE_BEFORE_APPLY, ReconcileStatus.ABSENT),
    ],
    ids=("reply-lost", "not-applied"),
)
def test_w8_uncertain_answer_reconciles_by_observation(
    world: World, fault: Fault, expected: ReconcileStatus
) -> None:
    port, api, _ = world
    prepared = prepare_answer(port, SOURCE, seed_received(api), "boss@example.test", "accepted")
    api.arm(fault)
    assert port.dispatch(prepared, "k").outcome is DispatchOutcome.UNKNOWN
    settled = port.reconcile(prepared, "k")
    assert settled.status is expected
    if expected is ReconcileStatus.ABSENT:
        assert settled.proof


def test_w35_the_invitee_copy_is_refused_for_change_and_cancel(world: World) -> None:
    port, api, _ = world
    locator = seed_received(api)
    handle = ref_handle(HandleKind.CALENDAR_EVENT, SOURCE, locator)
    etag = port.read(SOURCE, locator).etag
    sent = len(mutations(api))
    for intent in (
        CalendarDelete(source=SOURCE, event_ref=handle, expected_etag=etag),
        CalendarUpdate(source=SOURCE, event_ref=handle, expected_etag=etag, event=fields()),
    ):
        with pytest.raises(ChamberlainError) as raised:
            port.prepare(intent, {handle: locator})
        assert raised.value.detail == "invitee-copy"
    assert len(mutations(api)) == sent
