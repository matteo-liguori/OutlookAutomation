"""The fake provider's fault switches: docs/contracts/08-ports.md (The fake provider)."""

from datetime import UTC, datetime, timedelta

import pytest

from chamberlain.connectors.fake import FakeProvider, Fault
from chamberlain.domain.calendar import EventFields
from chamberlain.domain.capabilities import HealthState
from chamberlain.domain.handles import HandleKind, ref_handle
from chamberlain.domain.intents import CalendarCreate, MailMove, MailSend
from chamberlain.domain.mail import MailQuery
from chamberlain.domain.operations import DispatchOutcome, Prepared, ReconcileStatus
from chamberlain.ports.clock import ManualClock
from chamberlain.schema.errors import ChamberlainError, ErrorCode

MAIL = "mail:test"
CAL = "calendar:test"
MSG = "<m1@example.org>"


def _send(fake: FakeProvider, message_id: str = "<out1@example.org>") -> Prepared:
    intent = MailSend.model_validate(
        {
            "source": MAIL,
            "to": ["someone@example.org"],
            "subject": "Hello",
            "body": "Body.",
            "message_id": message_id,
        }
    )
    return fake.mail.prepare(intent, {})


def _move(fake: FakeProvider, locator: str) -> Prepared:
    handle = ref_handle(HandleKind.MESSAGE, MAIL, locator)
    intent = MailMove.model_validate(
        {"source": MAIL, "messages": [handle], "to_folder": "f-archive"}
    )
    return fake.mail.prepare(intent, {handle: locator})


def test_apply_then_lose_response(fake: FakeProvider) -> None:
    prepared = _send(fake)
    fake.arm(Fault.LOSE_RESPONSE_AFTER_APPLY)
    assert fake.mail.dispatch(prepared, "k1", {}).outcome is DispatchOutcome.UNKNOWN
    assert fake.mail.copies(MAIL, "<out1@example.org>", "f-sent") == 1
    assert fake.dispatch_log[-1].applied


def test_lose_before_apply(fake: FakeProvider) -> None:
    prepared = _send(fake)
    fake.arm(Fault.LOSE_BEFORE_APPLY)
    assert fake.mail.dispatch(prepared, "k1", {}).outcome is DispatchOutcome.UNKNOWN
    assert fake.mail.copies(MAIL, "<out1@example.org>") == 0
    assert not fake.dispatch_log[-1].applied
    assert fake.mail.reconcile(prepared, "k1").status is ReconcileStatus.ABSENT


def test_send_absence_is_unproven_unless_the_provider_and_1_related_checks(
    clock: ManualClock, fake: FakeProvider
) -> None:
    # send_absence_is_unproven_unless_the_provider_says_otherwise
    for provable in (False, True):
        fake = FakeProvider(clock, send_absence_provable=provable)
        fake.mail.add_source(MAIL)
        settled = fake.mail.reconcile(_send(fake), "k1")
        assert settled.status is ReconcileStatus.ABSENT
        assert settled.proof is provable

    # a_fault_is_consumed_by_one_call_and_can_be_armed_for_several
    fake.arm(Fault.BUSY_BEFORE_APPLY, times=2)
    outcomes = [
        fake.mail.dispatch(_send(fake, f"<o{i}@example.org>"), f"k{i}", {}).outcome
        for i in range(3)
    ]
    assert outcomes == [
        DispatchOutcome.NOT_APPLIED,
        DispatchOutcome.NOT_APPLIED,
        DispatchOutcome.APPLIED,
    ]
    fake.arm(Fault.LOSE_BEFORE_APPLY)
    fake.disarm()
    assert not fake.world.armed(Fault.LOSE_BEFORE_APPLY)


def test_restart_changes_session_ids_but_not_locators(fake: FakeProvider) -> None:
    locator = fake.mail.add_message(MAIL, internet_message_id=MSG)
    before = fake.mail.session_id(MAIL, locator)
    fake.arm(Fault.RESTART)
    assert fake.mail.resolve(MAIL, locator).internet_message_id == MSG
    assert fake.mail.session_id(MAIL, locator) != before


def test_restart_between_prepare_and_dispatch_still_moves_the_right_message(
    fake: FakeProvider,
) -> None:
    locator = fake.mail.add_message(MAIL, internet_message_id=MSG)
    fake.mail.add_message(MAIL, internet_message_id="<other@example.org>")
    prepared = _move(fake, locator)
    fake.restart()
    assert fake.mail.dispatch(prepared, "k1", {}).outcome is DispatchOutcome.APPLIED
    assert fake.mail.copies(MAIL, MSG, "f-archive") == 1
    assert fake.mail.copies(MAIL, "<other@example.org>", "f-inbox") == 1


def test_duplicate_copy_leaves_the_source_behind_and_1_related_checks(fake: FakeProvider) -> None:
    # duplicate_copy_leaves_the_source_behind
    locator = fake.mail.add_message(MAIL, internet_message_id=MSG)
    prepared = _move(fake, locator)
    fake.arm(Fault.DUPLICATE_COPY)
    assert fake.mail.dispatch(prepared, "k1", {}).outcome is DispatchOutcome.UNKNOWN
    assert fake.mail.copies(MAIL, MSG, "f-inbox") == 1
    assert fake.mail.copies(MAIL, MSG, "f-archive") == 1
    assert fake.mail.reconcile(prepared, "k1").status is ReconcileStatus.UNKNOWN

    # two_copies_in_one_folder_make_the_locator_ambiguous
    locator = fake.mail.add_message(MAIL, internet_message_id=MSG)
    fake.mail.add_message(MAIL, internet_message_id=MSG)
    with pytest.raises(ChamberlainError) as raised:
        fake.mail.resolve(MAIL, locator)
    assert raised.value.code is ErrorCode.REMOTE_AMBIGUOUS


def test_slow_read_times_out_and_changes_nothing(fake: FakeProvider, clock: ManualClock) -> None:
    locator = fake.mail.add_message(MAIL, internet_message_id=MSG)
    started = clock.now()
    fake.arm(Fault.SLOW)
    with pytest.raises(ChamberlainError) as raised:
        fake.mail.read(MAIL, locator)
    assert raised.value.code is ErrorCode.SOURCE_UNAVAILABLE
    assert raised.value.retryable
    assert not raised.value.may_have_happened
    assert clock.now() - started > timedelta(seconds=30)
    assert fake.mail.is_unread(MAIL, locator)


def test_slow_dispatch_answers_unknown_then_applies_late(
    fake: FakeProvider, clock: ManualClock
) -> None:
    start = datetime(2027, 3, 1, 10, 0, tzinfo=UTC)
    event = EventFields(title="Review", start=start, end=start + timedelta(hours=1))
    prepared = fake.calendar.prepare(CalendarCreate(source=CAL, event=event, uid="u1"), {})
    fake.arm(Fault.SLOW)
    assert fake.calendar.dispatch(prepared, "k1").outcome is DispatchOutcome.UNKNOWN
    assert fake.calendar.count(CAL, "u1") == 0
    assert fake.calendar.reconcile(prepared, "k1").status is ReconcileStatus.ABSENT
    clock.advance(minutes=5)
    assert fake.calendar.reconcile(prepared, "k1").status is ReconcileStatus.PRESENT
    assert fake.calendar.count(CAL, "u1") == 1
    assert fake.dispatch_count("k1") == 1


def test_stale_shows_in_health_and_in_coverage(fake: FakeProvider) -> None:
    assert fake.mail.health(MAIL).state is HealthState.OK
    assert fake.mail.health(MAIL).fresh_as_of is not None
    fake.arm(Fault.STALE)
    health = fake.mail.health(MAIL)
    assert health.state is HealthState.STALE
    assert health.fresh_as_of is None
    page = fake.mail.search(MAIL, MailQuery())
    assert not page.complete
    assert page.gaps[0].source == MAIL
    assert fake.mail.search(MAIL, MailQuery()).complete


def test_sign_in_needed_can_be_set_per_source(fake: FakeProvider) -> None:
    fake.world.health_override[MAIL] = HealthState.SIGN_IN_NEEDED
    assert fake.mail.health(MAIL).state is HealthState.SIGN_IN_NEEDED
    assert fake.calendar.health(CAL).state is HealthState.OK


def test_the_dispatch_log_exposes_a_double_dispatch_and_1_related_checks(
    fake: FakeProvider,
) -> None:
    # the_dispatch_log_exposes_a_double_dispatch: The fake does not protect against a second dis
    prepared = _send(fake)
    fake.mail.dispatch(prepared, "k1", {})
    fake.mail.dispatch(prepared, "k1", {})
    assert fake.dispatch_count("k1") == 2
    assert fake.mail.copies(MAIL, "<out1@example.org>", "f-sent") == 2

    # an_unknown_source_is_not_configured
    with pytest.raises(ChamberlainError) as raised:
        fake.mail.search("mail:nowhere", MailQuery())
    assert raised.value.code is ErrorCode.SOURCE_NOT_CONFIGURED
