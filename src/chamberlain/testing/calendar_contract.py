"""The CalendarPort contract suite.

Contract: docs/contracts/08-ports.md (P-16). Subclass and provide a `rig`
fixture returning a fresh `CalendarRig`.
"""

from datetime import UTC, datetime, timedelta

import pytest

from chamberlain.domain.calendar import CalendarQuery, EventFields, EventStatus, Response
from chamberlain.domain.capabilities import CapabilityName
from chamberlain.domain.handles import HandleKind, ref_handle
from chamberlain.domain.intents import (
    CalendarCreate,
    CalendarDelete,
    CalendarRespond,
    CalendarUpdate,
)
from chamberlain.domain.operations import DispatchOutcome, Prepared, ReconcileStatus
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.testing.rigs import CalendarRig, Fault

START = datetime(2027, 3, 1, 10, 0, tzinfo=UTC)
LATE = 600.0


def fields(title: str = "Contract event", *, hours: int = 0) -> EventFields:
    start = START + timedelta(hours=hours)
    return EventFields(title=title, start=start, end=start + timedelta(minutes=30))


def _window() -> CalendarQuery:
    return CalendarQuery(start=START - timedelta(days=1), end=START + timedelta(days=1))


def _need(rig: CalendarRig, fault: Fault) -> None:
    if not rig.inject(fault):
        pytest.skip(f"this rig cannot inject {fault.value}")


GUEST = "guest@example.test"
GUEST_TWO = "guest.two@example.test"
ORGANISER = "organiser@example.test"


def _invites(rig: CalendarRig) -> None:
    if rig.port.capabilities(rig.source).get(CapabilityName.CALENDAR_INVITE) is None:
        pytest.skip("this calendar does not send invitations")


def _invited(rig: CalendarRig) -> str:
    """The locator of an invitation the calendar's owner received, or skip."""
    locator = rig.seed_invitation(ORGANISER, fields("Invited"))
    if locator is None:
        pytest.skip("this rig cannot seed a received invitation")
    return locator


class CalendarPortContract:
    """Subclass and provide a `rig` fixture returning a fresh `CalendarRig`."""

    def _prepare_create(self, rig: CalendarRig, uid: str) -> Prepared:
        intent = CalendarCreate(source=rig.source, event=fields(), uid=uid)
        return rig.port.prepare(intent, {})

    def test_p1_capabilities_are_declared(self, rig: CalendarRig) -> None:
        declared = rig.port.capabilities(rig.source)
        assert declared.get(CapabilityName.CALENDAR_READ) is not None

    def test_p3_reading_does_not_change_the_event(self, rig: CalendarRig) -> None:
        locator = rig.seed(fields())
        before = rig.port.read(rig.source, locator)
        rig.port.events(rig.source, _window())
        assert rig.port.read(rig.source, locator) == before

    def test_p4_a_stale_calendar_is_reported_incomplete(self, rig: CalendarRig) -> None:
        _need(rig, Fault.STALE)
        page = rig.port.events(rig.source, _window())
        assert not page.complete
        assert page.gaps

    def test_p16_create_uses_the_given_uid_once(self, rig: CalendarRig) -> None:
        uid = rig.new_uid()
        prepared = self._prepare_create(rig, uid)
        assert prepared.reconcile_token == uid
        first = rig.port.dispatch(prepared, "k-create")
        assert first.outcome is DispatchOutcome.APPLIED
        second = rig.port.dispatch(prepared, "k-create-again")
        assert second.outcome is DispatchOutcome.NOT_APPLIED
        assert rig.count(uid) == 1
        assert rig.port.reconcile(prepared, "k-create").status is ReconcileStatus.PRESENT

    def test_p16_update_needs_the_current_etag(self, rig: CalendarRig) -> None:
        locator = rig.seed(fields())
        handle = ref_handle(HandleKind.CALENDAR_EVENT, rig.source, locator)
        current = rig.port.read(rig.source, locator)
        stale = CalendarUpdate(
            source=rig.source, event_ref=handle, expected_etag="stale", event=fields("Changed")
        )
        with pytest.raises(ChamberlainError) as raised:
            rig.port.prepare(stale, {handle: locator})
        assert raised.value.code is ErrorCode.REMOTE_CHANGED
        update = CalendarUpdate(
            source=rig.source,
            event_ref=handle,
            expected_etag=current.etag,
            event=fields("Changed"),
        )
        prepared = rig.port.prepare(update, {handle: locator})
        result = rig.port.dispatch(prepared, "k-update")
        assert result.outcome is DispatchOutcome.APPLIED
        after = rig.port.read(rig.source, locator)
        assert after.fields.title == "Changed"
        assert after.etag != current.etag

    def test_p16_a_deleted_event_reads_as_cancelled_or_is_gone(self, rig: CalendarRig) -> None:
        """A connector whose store keeps no tombstone may report the event as gone."""
        locator = rig.seed(fields())
        handle = ref_handle(HandleKind.CALENDAR_EVENT, rig.source, locator)
        etag = rig.port.read(rig.source, locator).etag
        delete = CalendarDelete(source=rig.source, event_ref=handle, expected_etag=etag)
        prepared = rig.port.prepare(delete, {handle: locator})
        assert rig.port.dispatch(prepared, "k-delete").outcome is DispatchOutcome.APPLIED
        after = None
        gone: ChamberlainError | None = None
        try:
            after = rig.port.read(rig.source, locator)
        except ChamberlainError as error:
            gone = error
        if gone is not None:
            assert gone.code is ErrorCode.REQUEST_UNKNOWN_HANDLE
        else:
            assert after is not None
            assert after.status is EventStatus.CANCELLED
        assert not rig.port.events(rig.source, _window()).items
        query = _window().model_copy(update={"include_cancelled": True})
        listed = rig.port.events(rig.source, query).items
        assert [e.status for e in listed] in ([], [EventStatus.CANCELLED])
        assert rig.port.reconcile(prepared, "k-delete").status is ReconcileStatus.PRESENT

    def test_w8_a_lost_response_settles_from_a_read_back(self, rig: CalendarRig) -> None:
        uid = rig.new_uid()
        prepared = self._prepare_create(rig, uid)
        _need(rig, Fault.LOSE_RESPONSE_AFTER_APPLY)
        assert rig.port.dispatch(prepared, "k-lost").outcome is DispatchOutcome.UNKNOWN
        assert rig.port.reconcile(prepared, "k-lost").status is ReconcileStatus.PRESENT
        assert rig.count(uid) == 1

    def test_p12_a_create_that_never_landed_is_proven_absent(self, rig: CalendarRig) -> None:
        uid = rig.new_uid()
        prepared = self._prepare_create(rig, uid)
        _need(rig, Fault.LOSE_BEFORE_APPLY)
        assert rig.port.dispatch(prepared, "k-never").outcome is DispatchOutcome.UNKNOWN
        settled = rig.port.reconcile(prepared, "k-never")
        assert settled.status is ReconcileStatus.ABSENT
        assert settled.proof
        assert rig.count(uid) == 0

    def test_p17_a_late_write_is_found_by_reconcile(self, rig: CalendarRig) -> None:
        uid = rig.new_uid()
        prepared = self._prepare_create(rig, uid)
        _need(rig, Fault.SLOW)
        assert rig.port.dispatch(prepared, "k-slow").outcome is DispatchOutcome.UNKNOWN
        assert rig.count(uid) == 0
        rig.advance(LATE)
        assert rig.port.reconcile(prepared, "k-slow").status is ReconcileStatus.PRESENT
        assert rig.count(uid) == 1

    def test_p10_busy_is_not_applied(self, rig: CalendarRig) -> None:
        uid = rig.new_uid()
        prepared = self._prepare_create(rig, uid)
        _need(rig, Fault.BUSY_BEFORE_APPLY)
        assert rig.port.dispatch(prepared, "k-busy").outcome is DispatchOutcome.NOT_APPLIED
        assert rig.count(uid) == 0

    # --- invitations (04-writes W-33 to W-36, 08-ports P-19) -----------------------

    def _invite(self, rig: CalendarRig, *guests: str) -> tuple[Prepared, str]:
        intent = CalendarCreate(
            source=rig.source,
            event=fields("Invitation").model_copy(update={"attendees": guests}),
            uid=rig.new_uid(),
        )
        prepared = rig.port.prepare(intent, {})
        result = rig.port.dispatch(prepared, "k-invite")
        assert result.outcome is DispatchOutcome.APPLIED, result.detail
        assert result.receipt is not None
        assert result.receipt.locator is not None
        return prepared, result.receipt.locator

    def test_p19_an_invitation_is_written_with_its_guests_and_read_back(
        self, rig: CalendarRig
    ) -> None:
        _invites(rig)
        prepared, locator = self._invite(rig, GUEST)
        event = rig.port.read(rig.source, locator)
        assert event.fields.attendees == (GUEST,)
        assert [p.address for p in event.participants] == [GUEST]
        assert event.participants[0].response is Response.NEEDS_ACTION
        assert event.my_response is None
        assert rig.port.reconcile(prepared, "k-invite").status is ReconcileStatus.PRESENT
        again = rig.port.dispatch(prepared, "k-invite-again")
        assert again.outcome is DispatchOutcome.NOT_APPLIED

    def test_p19_an_invitation_can_change_its_guests_and_be_cancelled(
        self, rig: CalendarRig
    ) -> None:
        _invites(rig)
        _, locator = self._invite(rig, GUEST)
        handle = ref_handle(HandleKind.CALENDAR_EVENT, rig.source, locator)
        etag = rig.port.read(rig.source, locator).etag
        wanted = fields("Invitation").model_copy(update={"attendees": (GUEST_TWO,)})
        update = CalendarUpdate(
            source=rig.source, event_ref=handle, expected_etag=etag, event=wanted
        )
        prepared = rig.port.prepare(update, {handle: locator})
        assert rig.port.dispatch(prepared, "k-change").outcome is DispatchOutcome.APPLIED
        after = rig.port.read(rig.source, locator)
        assert after.fields.attendees == (GUEST_TWO,)
        delete = CalendarDelete(source=rig.source, event_ref=handle, expected_etag=after.etag)
        cancel = rig.port.prepare(delete, {handle: locator})
        assert rig.port.dispatch(cancel, "k-cancel").outcome is DispatchOutcome.APPLIED

    def test_p19_a_received_invitation_is_answered_and_the_answer_reads_back(
        self, rig: CalendarRig
    ) -> None:
        _invites(rig)
        locator = _invited(rig)
        handle = ref_handle(HandleKind.CALENDAR_EVENT, rig.source, locator)
        event = rig.port.read(rig.source, locator)
        assert event.organiser == ORGANISER
        assert event.my_response is Response.NEEDS_ACTION
        answer = CalendarRespond(
            source=rig.source,
            event_ref=handle,
            expected_etag=event.etag,
            response="tentative",
            organiser=ORGANISER,
        )
        prepared = rig.port.prepare(answer, {handle: locator})
        assert rig.port.dispatch(prepared, "k-answer").outcome is DispatchOutcome.APPLIED
        after = rig.port.read(rig.source, locator)
        assert after.my_response is Response.TENTATIVE
        assert after.fields.title == "Invited"
        assert rig.port.reconcile(prepared, "k-answer").status is ReconcileStatus.PRESENT

    def test_p19_an_answer_needs_the_current_etag_and_an_invitation(self, rig: CalendarRig) -> None:
        _invites(rig)
        locator = _invited(rig)
        handle = ref_handle(HandleKind.CALENDAR_EVENT, rig.source, locator)
        stale = CalendarRespond(
            source=rig.source,
            event_ref=handle,
            expected_etag="stale",
            response="accepted",
            organiser=ORGANISER,
        )
        with pytest.raises(ChamberlainError) as raised:
            rig.port.prepare(stale, {handle: locator})
        assert raised.value.code is ErrorCode.REMOTE_CHANGED
        own = rig.seed(fields("Mine"))
        own_handle = ref_handle(HandleKind.CALENDAR_EVENT, rig.source, own)
        not_invited = CalendarRespond(
            source=rig.source,
            event_ref=own_handle,
            expected_etag=rig.port.read(rig.source, own).etag,
            response="accepted",
            organiser=ORGANISER,
        )
        with pytest.raises(ChamberlainError) as invalid:
            rig.port.prepare(not_invited, {own_handle: own})
        assert invalid.value.code is ErrorCode.REQUEST_INVALID

    def test_p19_the_invitee_cannot_change_or_cancel_the_invitation(self, rig: CalendarRig) -> None:
        _invites(rig)
        locator = _invited(rig)
        handle = ref_handle(HandleKind.CALENDAR_EVENT, rig.source, locator)
        etag = rig.port.read(rig.source, locator).etag
        for intent in (
            CalendarDelete(source=rig.source, event_ref=handle, expected_etag=etag),
            CalendarUpdate(
                source=rig.source, event_ref=handle, expected_etag=etag, event=fields("Mine now")
            ),
        ):
            with pytest.raises(ChamberlainError) as raised:
                rig.port.prepare(intent, {handle: locator})
            assert raised.value.code is ErrorCode.CAPABILITY_UNSUPPORTED
            assert raised.value.detail == "invitee-copy"
