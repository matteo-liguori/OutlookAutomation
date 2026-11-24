"""The write state machine against the fake provider (04-writes.md, 11-ported-test-intent.md)."""

from contextlib import AbstractContextManager

import pytest

from chamberlain.connectors.fake import Fault
from chamberlain.domain.canonical import sha256_bytes
from chamberlain.domain.handles import HandleKind
from chamberlain.domain.intents import (
    AttachmentRef,
    CalendarDelete,
    CalendarUpdate,
    MailMarkRead,
    MailMove,
    MailSend,
    MailTrash,
)
from chamberlain.domain.operations import OperationState
from chamberlain.schema.errors import ChamberlainError, ErrorCategory, ErrorCode
from chamberlain.testing.write_rig import CALENDAR, MAIL, SimulatedCrash, WriteRig, make_rig
from chamberlain.writes import repo
from chamberlain.writes.maintenance import maintain

S = OperationState


def fails(code: ErrorCode) -> AbstractContextManager[pytest.ExceptionInfo[ChamberlainError]]:
    """`with fails(code) as raised:` passes only for that exact error code."""
    return _Fails(code)


class _Fails(AbstractContextManager[pytest.ExceptionInfo[ChamberlainError]]):
    def __init__(self, code: ErrorCode) -> None:
        self._code = code
        self._inner = pytest.raises(ChamberlainError)
        self._info: pytest.ExceptionInfo[ChamberlainError] | None = None

    def __enter__(self) -> pytest.ExceptionInfo[ChamberlainError]:
        self._info = self._inner.__enter__()
        return self._info

    def __exit__(self, *exc: object) -> bool:
        suppressed = bool(self._inner.__exit__(*exc))  # type: ignore[arg-type]
        if suppressed:
            assert self._info is not None
            assert self._info.value.code is self._code, self._info.value.code
        return suppressed


def test_w8_w17_an_uncertain_send_is_never_reissued(rig: WriteRig) -> None:
    """Old: test_uncertain_send_is_never_reissued."""
    op = rig.ready_send()
    rig.fake.arm(Fault.LOSE_BEFORE_APPLY)
    eng = rig.engine()
    with fails(ErrorCode.OPERATION_UNCERTAIN):
        eng.do(op.handle)
    stored = rig.engine().get(op.handle)
    assert stored.state is S.UNCERTAIN
    for _ in range(3):
        with fails(ErrorCode.OPERATION_NEEDS_RECONCILE) as raised:
            rig.engine().do(op.handle)
        assert raised.value.category is ErrorCategory.UNCERTAIN
        assert raised.value.may_have_happened
    again = eng.propose_intent(rig.send())
    assert again.operation is not None
    assert again.operation.handle == op.handle
    assert rig.fake.dispatch_count(stored.idempotency_key) == 1
    assert rig.sent_copies() == 0


def test_w16_a_repeat_returns_the_stored_receipt(rig: WriteRig) -> None:
    """Old: test_exact_move_is_durable (first half)."""
    op = rig.ready_send()
    first = rig.engine().do(op.handle)
    second = rig.engine().do(op.handle)
    assert (first.state, first.receipt_json) == (second.state, second.receipt_json)
    assert rig.sent_copies() == 1


def test_w18_a_changed_intent_cannot_reuse_the_grant(rig: WriteRig) -> None:
    """Old: test_exact_move_is_durable (second half)."""
    approved = rig.ready_send()
    rig.engine().do(approved.handle)
    changed = rig.engine().propose_intent(rig.send(body="A different note."))
    assert not changed.ready
    assert changed.operation is not None
    assert changed.operation.handle != approved.handle
    with fails(ErrorCode.GRANT_REQUIRED):
        rig.engine().do(changed.operation.handle)
    assert rig.sent_copies() == 1


def test_w10_w11_busy_before_dispatch_frees_the_grant_and_a_retry_works(rig: WriteRig) -> None:
    """Old: test_client_busy_before_dispatch_can_be_retried_with_same_grant."""
    first = rig.ready_send()
    rig.fake.arm(Fault.BUSY_BEFORE_APPLY)
    with fails(ErrorCode.SOURCE_BUSY):
        rig.engine().do(first.handle)
    failed = rig.engine().get(first.handle)
    assert failed.state is S.FAILED_CLEAN
    assert failed.grant is not None
    grant = repo.get_grant(rig.db, failed.grant)
    assert grant is not None
    assert grant.state.value == "active"
    with fails(ErrorCode.SOURCE_BUSY):
        rig.engine().do(first.handle)
    retry = rig.engine().propose_intent(rig.send())
    assert retry.ready
    assert retry.operation is not None
    assert retry.operation.handle != first.handle
    done = rig.engine().do(retry.operation.handle)
    assert done.state is S.CONFIRMED
    assert done.idempotency_key != failed.idempotency_key
    assert rig.sent_copies() == 1
    spent = repo.get_grant(rig.db, failed.grant)
    assert spent is not None
    assert spent.state.value == "spent"


def test_w11_a_failed_prepare_releases_and_never_burns_the_grant(rig: WriteRig) -> None:
    handle = rig.seed_message()
    intent = MailMove(source=MAIL, message=handle, to_folder="f-archive")
    eng = rig.engine()
    proposal = eng.propose_intent(intent)
    approved = rig.agent_approves(proposal)
    assert approved.operation is not None
    rig.fake.world.mail[MAIL].clear()
    with fails(ErrorCode.REQUEST_UNKNOWN_HANDLE):
        eng.do(approved.operation.handle)
    op = eng.get(approved.operation.handle)
    assert op.state is S.FAILED_CLEAN
    assert rig.fake.dispatch_log == []


def test_w4_p14_trash_goes_to_the_recoverable_folder_and_the_handle_survives(
    rig: WriteRig,
) -> None:
    """Old: test_trash_uses_only_approved_configured_recoverable_destination (R4 repoint)."""
    handle = rig.seed_message("<keep@example.org>")
    before = rig.refs.locate(handle)[1]
    proposal = rig.engine().propose_intent(MailTrash(source=MAIL, message=handle))
    approved = rig.agent_approves(proposal)
    assert approved.operation is not None
    done = rig.engine().do(approved.operation.handle)
    assert done.state is S.CONFIRMED
    assert rig.fake.mail.copies(MAIL, "<keep@example.org>", "f-trash") == 1
    assert rig.fake.mail.copies(MAIL, "<keep@example.org>", "f-inbox") == 0
    assert rig.refs.locate(handle)[1] != before
    assert rig.refs.locate(handle)[1].startswith("f-trash/")


def test_p11_an_uncertain_move_reconciles_without_a_second_mutation(rig: WriteRig) -> None:
    """Old: test_uncertain_move_reconciles_without_a_second_mutation."""
    handle = rig.seed_message("<mv@example.org>")
    move = MailMove(source=MAIL, message=handle, to_folder="f-archive")
    proposal = rig.engine().propose_intent(move)
    approved = rig.agent_approves(proposal)
    assert approved.operation is not None
    rig.fake.arm(Fault.LOSE_RESPONSE_AFTER_APPLY)
    rig.fake.arm(Fault.STALE, 3)
    with fails(ErrorCode.OPERATION_UNCERTAIN):
        rig.engine().do(approved.operation.handle)
    key = rig.engine().get(approved.operation.handle).idempotency_key
    rig.fake.disarm(Fault.STALE)
    op, changed = rig.engine().reconcile(approved.operation.handle)
    assert changed
    assert op.state is S.CONFIRMED
    assert rig.fake.dispatch_count(key) == 1
    assert rig.fake.mail.copies(MAIL, "<mv@example.org>", "f-archive") == 1


def test_w12_absence_needs_proof_capability_and_time() -> None:
    rig = make_rig(send_absence_provable=True)
    try:
        op = rig.ready_send()
        rig.fake.arm(Fault.LOSE_BEFORE_APPLY)
        with fails(ErrorCode.OPERATION_UNCERTAIN):
            rig.engine().do(op.handle)
        early, _ = rig.engine().reconcile(op.handle)
        assert early.state is S.UNCERTAIN
        rig.clock.advance(minutes=11)
        late, changed = rig.engine().reconcile(op.handle)
        assert changed
        assert late.state is S.CONFIRMED_ABSENT
        grant = repo.get_grant(rig.db, late.grant or "")
        assert grant is not None
        assert grant.state.value == "active"
        assert rig.sent_copies() == 0
    finally:
        rig.close()


def test_w12_an_unprovable_absence_stays_uncertain_forever(rig: WriteRig) -> None:
    op = rig.ready_send()
    rig.fake.arm(Fault.LOSE_BEFORE_APPLY)
    with fails(ErrorCode.OPERATION_UNCERTAIN):
        rig.engine().do(op.handle)
    rig.clock.advance(hours=48)
    after, changed = rig.engine().reconcile(op.handle)
    assert after.state is S.UNCERTAIN
    assert not changed


def test_w13_the_user_resolves_an_uncertain_operation(rig: WriteRig) -> None:
    op = rig.ready_send()
    rig.fake.arm(Fault.LOSE_BEFORE_APPLY)
    with fails(ErrorCode.OPERATION_UNCERTAIN):
        rig.engine().do(op.handle)
    resolved = rig.engine().resolve(op.handle, happened=False, actor="human:viewer")
    assert resolved.state is S.CONFIRMED_ABSENT
    with fails(ErrorCode.OPERATION_ALREADY_SETTLED):
        rig.engine().resolve(op.handle, happened=True, actor="human:viewer")


def test_w14_a_client_verified_send_is_upgraded_by_reading_back(rig: WriteRig) -> None:
    op = rig.ready_send()
    done = rig.engine().do(op.handle)
    view = rig.engine().view(rig.engine().get(done.handle))
    assert view.verification.client_verified
    assert view.verification.server_verified


def test_w14_failure_to_upgrade_never_changes_the_state(rig: WriteRig) -> None:
    op = rig.ready_send()
    rig.fake.arm(Fault.STALE, 10)
    done = rig.engine().do(op.handle)
    assert done.state is S.CONFIRMED
    assert not rig.engine().get(op.handle).server_verified


def _send_with_attachment(rig: WriteRig, data: bytes) -> tuple[str, str]:
    artifact = rig.artifacts.put(data, owner="ses_testsession01")
    rig.know("friend@example.org")
    intent = MailSend(
        source=MAIL,
        to=("friend@example.org",),
        subject="With file",
        body="See attached.",
        attachments=(
            AttachmentRef(artifact=artifact, sha256=sha256_bytes(data), filename="a.bin"),
        ),
    )
    proposal = rig.engine().propose_intent(intent)
    approved = rig.agent_approves(proposal)
    assert approved.operation is not None
    return artifact, approved.operation.handle


def test_w3_changed_attachment_bytes_are_rejected_and_nothing_is_sent(rig: WriteRig) -> None:
    """Old: test_attachment_changed_bytes_are_rejected."""
    artifact, handle = _send_with_attachment(rig, b"original bytes")
    rig.artifacts.blobs[artifact] = b"tampered bytes"
    with fails(ErrorCode.ARTIFACT_HASH_MISMATCH):
        rig.engine().do(handle)
    assert rig.engine().get(handle).state is S.FAILED_CLEAN
    assert rig.sent_copies() == 0
    assert rig.fake.dispatch_log == []


def test_w3_bytes_changed_between_prepare_and_dispatch_are_caught_by_the_second_check(
    rig: WriteRig,
) -> None:
    artifact, handle = _send_with_attachment(rig, b"original bytes")
    rig.mail.after_prepare = lambda: rig.artifacts.blobs.update({artifact: b"tampered later"})
    with fails(ErrorCode.ARTIFACT_HASH_MISMATCH):
        rig.engine().do(handle)
    assert rig.engine().get(handle).state is S.FAILED_CLEAN
    assert rig.sent_copies() == 0
    assert rig.fake.dispatch_log == []


def test_w3_an_attachment_is_held_by_the_operation_until_it_ends(rig: WriteRig) -> None:
    """Old: test_pending_send_survives_cleanup (M-11)."""
    artifact, handle = _send_with_attachment(rig, b"hold me")
    assert handle in rig.artifacts.owners[artifact]
    rig.engine().do(handle)
    assert handle not in rig.artifacts.owners[artifact]
    assert rig.sent_copies() == 1


def test_w3_attachments_are_artifact_handles_never_paths() -> None:
    """Old: test_attachment_paths_outside_private_data_are_rejected."""
    with pytest.raises(ValueError, match="artifact"):
        AttachmentRef(artifact="C:/secrets/key.pem", sha256="0" * 64, filename="k")


def test_w24_mark_read_reports_per_message_results_and_is_not_retried(rig: WriteRig) -> None:
    """Old: test_mark_read_needs_the_permission_default_and_reports_partial_results."""
    first = rig.seed_message("<one@example.org>")
    second = rig.seed_message("<two@example.org>")
    proposal = rig.engine().propose_intent(MailMarkRead(source=MAIL, messages=(first, second)))
    approved = rig.agent_approves(proposal)
    assert approved.operation is not None

    def duplicate_the_second_message() -> None:
        rig.fake.mail.add_message(MAIL, internet_message_id="<two@example.org>")

    rig.mail.before_dispatch = duplicate_the_second_message
    done = rig.engine().do(approved.operation.handle)
    assert done.state is S.CONFIRMED
    view = rig.engine().view(done)
    assert (view.items_ok, view.items_failed) == (1, 1)
    assert not rig.fake.mail.is_unread(MAIL, rig.refs.locate(first)[1])
    assert len(rig.fake.dispatch_log) == 1


def test_w23_marking_read_is_a_write_that_needs_authority(rig: WriteRig) -> None:
    handle = rig.seed_message()
    proposal = rig.engine().propose_intent(MailMarkRead(source=MAIL, messages=(handle,)))
    assert not proposal.ready
    assert proposal.operation is not None
    with fails(ErrorCode.GRANT_REQUIRED):
        rig.engine().do(proposal.operation.handle)
    assert rig.fake.mail.is_unread(MAIL, rig.refs.locate(handle)[1])


def test_p16_a_calendar_create_is_exact_and_a_repeat_does_not_duplicate(rig: WriteRig) -> None:
    """Old: test_calendar_write_exact_id_and_old_version."""
    proposal = rig.engine().propose_intent(rig.event())
    approved = rig.agent_approves(proposal)
    assert approved.operation is not None
    done = rig.engine().do(approved.operation.handle)
    assert done.state is S.CONFIRMED
    uid = done.reconcile_token or ""
    assert uid
    assert rig.fake.calendar.count(CALENDAR, uid) == 1
    rig.engine().do(approved.operation.handle)
    assert rig.fake.calendar.count(CALENDAR, uid) == 1


def test_p11_a_calendar_timeout_settles_from_a_readback_without_reissuing(rig: WriteRig) -> None:
    """Old: test_calendar_timeout_settles_only_from_exact_readback_without_reissuing."""
    proposal = rig.engine().propose_intent(rig.event("Slow lunch"))
    approved = rig.agent_approves(proposal)
    assert approved.operation is not None

    def go_slow() -> None:
        rig.fake.arm(Fault.SLOW)

    rig.calendar.before_dispatch = go_slow
    # The late write lands inside the immediate-settle window, so `do` may already see it.
    try:
        done = rig.engine().do(approved.operation.handle)
    except ChamberlainError:
        done, _ = rig.engine().reconcile(approved.operation.handle)
    assert done.state is S.CONFIRMED
    assert len(rig.fake.dispatch_log) == 1
    assert rig.fake.calendar.count(CALENDAR, done.reconcile_token or "") == 1


def test_q2_a_calendar_change_with_attendees_needs_the_human(rig: WriteRig) -> None:
    proposal = rig.engine().propose_intent(rig.event(attendees=("guest@example.org",)))
    assert proposal.requirement.value == "human_confirmation"
    with fails(ErrorCode.GRANT_HUMAN_CONFIRMATION_REQUIRED):
        rig.agent_approves(proposal)


def test_g12_owner_calendar_writes_without_attendees_use_agent_evidence(rig: WriteRig) -> None:
    """Owner ruling 2026-11-24: create, update and delete on an event with no attendees."""
    created = rig.engine().propose_intent(rig.event())
    assert created.requirement.value == "agent_evidence"
    fields = rig.event().event
    locator = rig.fake.calendar.add_event(CALENDAR, fields)
    handle = rig.refs.remember(HandleKind.CALENDAR_EVENT, CALENDAR, locator)
    stored = rig.fake.calendar.read(CALENDAR, locator)
    update = CalendarUpdate(
        source=CALENDAR,
        event_ref=handle,
        expected_etag=stored.etag,
        event=fields.model_copy(update={"title": "Lunch moved"}),
    )
    delete = CalendarDelete(source=CALENDAR, event_ref=handle, expected_etag=stored.etag)
    assert rig.engine().propose_intent(update).requirement.value == "agent_evidence"
    assert rig.engine().propose_intent(delete).requirement.value == "agent_evidence"


def test_g12_owner_updating_or_deleting_an_event_with_attendees_needs_the_human(
    rig: WriteRig,
) -> None:
    fields = rig.event(attendees=("guest@example.org",)).event
    locator = rig.fake.calendar.add_event(CALENDAR, fields)
    handle = rig.refs.remember(HandleKind.CALENDAR_EVENT, CALENDAR, locator)
    stored = rig.fake.calendar.read(CALENDAR, locator)
    quiet = fields.model_copy(update={"title": "Lunch moved", "attendees": ()})
    update = CalendarUpdate(
        source=CALENDAR, event_ref=handle, expected_etag=stored.etag, event=quiet
    )
    delete = CalendarDelete(source=CALENDAR, event_ref=handle, expected_etag=stored.etag)
    for intent in (update, delete):
        proposal = rig.engine().propose_intent(intent)
        assert proposal.requirement.value == "human_confirmation"
        with fails(ErrorCode.GRANT_HUMAN_CONFIRMATION_REQUIRED):
            rig.agent_approves(proposal)


def test_r3_propose_runs_the_connector_scan_before_the_operation_is_stored(
    rig: WriteRig,
) -> None:
    rig.know("friend@example.org")

    def duplicate() -> None:
        raise ChamberlainError(ErrorCode.REMOTE_CHANGED, "A related message is already in Sent.")

    rig.mail.after_prepare = duplicate
    with fails(ErrorCode.REMOTE_CHANGED):
        rig.engine().propose_intent(rig.send())
    assert repo.count_operations(rig.db) == 0
    rig.mail.after_prepare = None
    assert rig.engine().propose_intent(rig.send()).operation is not None
    assert rig.mail.last_refs.get("_allow_duplicate") is None


def test_t11_cancel_only_before_prepare_and_never_after_dispatch(rig: WriteRig) -> None:
    op = rig.ready_send()
    cancelled = rig.engine().cancel(op.handle, actor="agent:x")
    assert cancelled.state is S.CANCELLED
    with fails(ErrorCode.OPERATION_ALREADY_SETTLED):
        rig.engine().do(op.handle)
    second = rig.ready_send(subject="Second")
    rig.fake.arm(Fault.LOSE_BEFORE_APPLY)
    with fails(ErrorCode.OPERATION_UNCERTAIN):
        rig.engine().do(second.handle)
    with fails(ErrorCode.OPERATION_NEEDS_RECONCILE):
        rig.engine().cancel(second.handle, actor="agent:x")


def test_w25_settled_operations_compact_to_a_one_line_receipt(rig: WriteRig) -> None:
    op = rig.ready_send()
    rig.engine().do(op.handle)
    rig.clock.advance(hours=24 * 31)
    report = maintain(rig.ctx, rig.policy)
    assert report.operations_compacted == 1
    compact = rig.engine().get(op.handle)
    assert compact.intent_json is None
    assert compact.prepared_json is None
    assert compact.receipt_json is not None
    assert len(compact.receipt_json) < 400
    assert rig.engine().do(op.handle).state is S.CONFIRMED


def test_w25_an_uncertain_operation_is_never_compacted(rig: WriteRig) -> None:
    op = rig.ready_send()
    rig.fake.arm(Fault.LOSE_BEFORE_APPLY)
    with fails(ErrorCode.OPERATION_UNCERTAIN):
        rig.engine().do(op.handle)
    rig.clock.advance(hours=24 * 900)
    maintain(rig.ctx, rig.policy)
    assert rig.engine().get(op.handle).intent_json is not None


def test_w9_a_crash_after_the_journal_becomes_uncertain_once_the_lease_expires(
    rig: WriteRig,
) -> None:
    op = rig.ready_send()
    rig.mail.crash_after_dispatch = True
    with pytest.raises(SimulatedCrash):
        rig.engine(worker="doomed").do(op.handle)
    assert rig.engine().get(op.handle).state is S.DISPATCHED
    assert rig.engine().recover() == 0
    rig.clock.advance(minutes=6)
    assert rig.engine().recover() == 1
    assert rig.engine().get(op.handle).state is S.UNCERTAIN
    rig.mail.crash_after_dispatch = False
    settled, changed = rig.engine().reconcile(op.handle)
    assert changed
    assert settled.state is S.CONFIRMED
    assert rig.sent_copies() == 1
    assert len(rig.fake.dispatch_log) == 1


def test_w9_authorised_operations_survive_a_crash_and_do_resumes(rig: WriteRig) -> None:
    op = rig.ready_send()
    rig.mail.crash_in_prepare = True
    with pytest.raises(SimulatedCrash):
        rig.engine().do(op.handle)
    assert rig.engine().get(op.handle).state is S.AUTHORISED
    rig.mail.crash_in_prepare = False
    done = rig.engine().do(op.handle)
    assert done.state is S.CONFIRMED
    assert len(rig.fake.dispatch_log) == 1


def test_w9_prepared_operations_survive_a_crash_and_do_resumes(rig: WriteRig) -> None:
    artifact, handle = _send_with_attachment(rig, b"bytes")
    reads = {"count": 0}
    original = rig.artifacts.read_bytes

    def flaky(name: str) -> bytes:
        reads["count"] += 1
        if reads["count"] == 2:
            raise SimulatedCrash
        return original(name)

    rig.artifacts.read_bytes = flaky  # type: ignore[method-assign]
    with pytest.raises(SimulatedCrash):
        rig.engine().do(handle)
    assert rig.engine().get(handle).state is S.PREPARED
    done = rig.engine().do(handle)
    assert done.state is S.CONFIRMED
    assert rig.fake.dispatch_log[0].applied
    assert len(rig.fake.dispatch_log) == 1
    assert artifact in rig.artifacts.blobs


def test_w9_w25_the_tick_seams_recover_compact_and_sweep_grants(rig: WriteRig) -> None:
    from chamberlain.writes.maintenance import COMPACTORS, recover_operations

    op = rig.ready_send()
    rig.mail.crash_after_dispatch = True
    with pytest.raises(SimulatedCrash):
        rig.engine().do(op.handle)
    rig.mail.crash_after_dispatch = False
    rig.clock.advance(minutes=6)
    recover_operations(rig.ctx)
    assert rig.engine().get(op.handle).state is S.UNCERTAIN
    assert [name for name, _ in COMPACTORS] == ["operations_compacted", "grants_changed"]
    changed = [fn(rig.ctx, rig.clock.now()) for _, fn in COMPACTORS]
    assert changed == [0, 0]
