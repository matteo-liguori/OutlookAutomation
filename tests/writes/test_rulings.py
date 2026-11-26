"""Write safety and reconciliation rulings R1 to R7 and R11."""

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from chamberlain.connectors.fake import Fault
from chamberlain.domain.canonical import sha256_text
from chamberlain.domain.grant_rules import intent_hash
from chamberlain.domain.grants import StandingScope
from chamberlain.domain.intents import IntentKind, MailDraftSave, MailSend
from chamberlain.domain.operations import (
    DispatchOutcome,
    DispatchResult,
    OperationState,
    ProviderReceipt,
    ReconcileResult,
    ReconcileStatus,
    Verification,
)
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.writes import repo
from chamberlain.writes.audit import verify
from chamberlain.writes.confirm import confirm_grant
from chamberlain.writes.core import (
    ALLOW_DUPLICATE_REF,
    CALENDAR_SETTLE_ATTEMPTS,
    MAIL_SETTLE_ATTEMPTS,
    MAIL_SETTLE_WINDOW_SECONDS,
)
from support.write_rig import MAIL, WriteRig, make_rig

S = OperationState
NO_FIXTURES = settings(
    max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)


@contextmanager
def rig_scope(*, send_absence_provable: bool = False) -> Iterator[WriteRig]:
    """A fresh rig per example, for property tests (function fixtures are shared across them)."""
    rig = make_rig(send_absence_provable=send_absence_provable)
    try:
        yield rig
    finally:
        rig.close()


@pytest.fixture
def provable_rig() -> Iterator[WriteRig]:
    with rig_scope(send_absence_provable=True) as rig:
        yield rig


def uncertain_send(rig: WriteRig, **kwargs: str) -> str:
    """A send whose dispatch outcome is unknown and nothing was applied."""
    op = rig.ready_send(**kwargs)
    rig.fake.arm(Fault.LOSE_BEFORE_APPLY)
    with pytest.raises(ChamberlainError) as raised:
        rig.engine().do(op.handle)
    assert raised.value.code is ErrorCode.OPERATION_UNCERTAIN
    return op.handle


def present() -> ReconcileResult:
    receipt = ProviderReceipt(
        locator="f-sent/x", verification=Verification(client_verified=True, server_verified=True)
    )
    return ReconcileResult(status=ReconcileStatus.PRESENT, receipt=receipt)


# --- R1: the hash excludes harness tokens; the token comes from the grant ------------


@given(token=st.text(min_size=0, max_size=30), uid=st.text(min_size=0, max_size=30))
def test_r1_the_intent_hash_ignores_agent_supplied_and_harness_tokens(token: str, uid: str) -> None:
    plain = MailSend(source=MAIL, to=("a@example.org",), subject="s", body="b")
    tokened = plain.model_copy(update={"message_id": token or None})
    assert intent_hash(plain) == intent_hash(tokened)
    other = MailSend(source=MAIL, to=("a@example.org",), subject="s", body="changed")
    assert intent_hash(plain) != intent_hash(other)
    assert uid is not None


def test_r1_the_token_is_derived_from_the_grant_and_the_intent_hash(rig: WriteRig) -> None:
    op = rig.ready_send()
    assert op.grant is None
    done = rig.engine().do(op.handle)
    assert done.grant is not None
    seed = sha256_text(f"{done.grant}:{done.intent_sha256}")
    assert done.idempotency_key == seed
    assert done.reconcile_token == f"<{seed[:32]}@chamberlain.invalid>"
    stored = done.intent()
    assert stored.message_id == done.reconcile_token  # type: ignore[union-attr]
    assert intent_hash(stored) == done.intent_sha256
    assert rig.fake.world.mail[MAIL][-1].internet_message_id == done.reconcile_token


def test_r1_an_agent_supplied_message_id_is_ignored(rig: WriteRig) -> None:
    rig.know("friend@example.org")
    intent = rig.send().model_copy(update={"message_id": "<chosen@evil.example>"})
    proposal = rig.engine().propose_intent(intent)
    assert proposal.operation is not None
    stored = proposal.operation.intent()
    assert stored.message_id is None  # type: ignore[union-attr]
    plain = rig.engine().propose_intent(rig.send())
    assert plain.operation is not None
    assert plain.operation.handle == proposal.operation.handle


# --- R2: queued keeps the operation uncertain and holds the grant --------------------


def test_r2_a_queued_copy_never_releases_the_grant_even_with_provable_absence(
    provable_rig: WriteRig,
) -> None:
    handle = uncertain_send(provable_rig)
    queued = ReconcileResult(status=ReconcileStatus.QUEUED)
    provable_rig.mail.reconcile_script = [queued] * 40
    provable_rig.clock.advance(hours=3)
    for _ in range(5):
        after, changed = provable_rig.engine().reconcile(handle)
        assert after.state is S.UNCERTAIN
        assert not changed
    held = provable_rig.engine().get(handle)
    assert held.grant is not None
    assert repo.live_uses(provable_rig.db, [held.grant], "op_other") == {held.grant: 1}
    again = provable_rig.engine().propose_intent(provable_rig.send())
    assert again.operation is not None
    assert again.operation.handle == handle
    assert not again.ready
    assert provable_rig.sent_copies() == 0


def test_r2_a_queued_copy_later_seen_present_confirms(rig: WriteRig) -> None:
    handle = uncertain_send(rig)
    rig.mail.reconcile_script = [ReconcileResult(status=ReconcileStatus.QUEUED), present()]
    first, _ = rig.engine().reconcile(handle)
    assert first.state is S.UNCERTAIN
    second, changed = rig.engine().reconcile(handle)
    assert changed
    assert second.state is S.CONFIRMED


@given(
    steps=st.lists(
        st.sampled_from(["queued", "unknown", "absent_unproven", "absent_proven"]),
        min_size=1,
        max_size=8,
    ),
    wait=st.integers(min_value=0, max_value=3 * 24 * 60),
)
@NO_FIXTURES
def test_r2_without_a_present_answer_the_grant_is_never_released(
    steps: list[str], wait: int
) -> None:
    """Property: queued and unproven answers keep the operation uncertain and held."""
    with rig_scope() as rig:
        handle = uncertain_send(rig)
        answers = {
            "queued": ReconcileResult(status=ReconcileStatus.QUEUED),
            "unknown": ReconcileResult(status=ReconcileStatus.UNKNOWN),
            "absent_unproven": ReconcileResult(status=ReconcileStatus.ABSENT, proof=False),
            "absent_proven": ReconcileResult(status=ReconcileStatus.ABSENT, proof=True),
        }
        rig.clock.advance(minutes=wait)
        for step in steps:
            rig.mail.reconcile_script = [answers[step]] * 4
            after, _ = rig.engine().reconcile(handle)
            # The fake does not declare send absence provable, so even a proven
            # absent from a connector must not free the grant (P-12).
            assert after.state is S.UNCERTAIN
        held = rig.engine().get(handle)
        assert held.grant is not None
        assert repo.live_uses(rig.db, [held.grant], "op_other")[held.grant] == 1


# --- R3: the duplicate scan reaches prepare through a reserved ref --------------------


def test_r3_allow_duplicate_reaches_prepare_only_for_a_grant_that_sets_it(rig: WriteRig) -> None:
    op = rig.ready_send()
    rig.engine().do(op.handle)
    assert ALLOW_DUPLICATE_REF not in rig.mail.last_refs
    scope = StandingScope(
        source=MAIL,
        intent_kinds=frozenset({IntentKind.MAIL_SEND}),
        title="Friend",
        recipients=frozenset({"friend@example.org"}),
        max_per_day=5,
        allow_duplicate=True,
    )
    standing = rig.engine().propose_standing(scope)
    assert standing.grant is not None
    confirm_grant(
        rig.engine().ctx,
        standing.grant.handle,
        {"content_sha256": standing.content_sha256},
        rig.policy,
    )
    proposal = rig.engine().propose_intent(rig.send(subject="Another"))
    assert proposal.ready
    assert proposal.operation is not None
    rig.engine().do(proposal.operation.handle)
    assert rig.mail.last_refs[ALLOW_DUPLICATE_REF] == "1"


# --- R4: handles survive filing; covered with the trash test in test_engine ------------


def test_r4_a_draft_save_returns_a_handle_for_what_it_produced(rig: WriteRig) -> None:
    draft = MailDraftSave(source=MAIL, to=("a@example.org",), subject="Draft", body="b")
    proposal = rig.engine().propose_intent(draft)
    approved = rig.agent_approves(proposal)
    assert approved.operation is not None
    done = rig.engine().do(approved.operation.handle)
    view = rig.engine().view(done)
    assert view.result_ref is not None
    assert view.result_ref.startswith("msg_")
    assert rig.refs.locate(view.result_ref)[0] == MAIL


# --- R5: leases ---------------------------------------------------------------------


def test_r5_recovery_never_touches_an_operation_whose_lease_is_alive(rig: WriteRig) -> None:
    op = rig.ready_send()
    seen: list[int] = []

    def while_dispatching() -> None:
        seen.append(rig.engine(worker="other").recover())

    rig.mail.during_dispatch = while_dispatching
    done = rig.engine().do(op.handle)
    assert seen == [0]
    assert done.state is S.CONFIRMED


def test_r5_a_late_applied_result_after_takeover_confirms_through_the_reconcile_path(
    rig: WriteRig,
) -> None:
    op = rig.ready_send()

    def take_over() -> None:
        rig.clock.advance(minutes=6)
        assert rig.engine(worker="rescuer").recover() == 1

    rig.mail.during_dispatch = take_over
    done = rig.engine(worker="original").do(op.handle)
    assert done.state is S.CONFIRMED
    kinds = [
        row["kind"]
        for row in rig.db.execute(
            "SELECT kind FROM audit_log WHERE subject = ? ORDER BY seq", (op.handle,)
        )
    ]
    tail = [k for k in kinds if k.startswith("operation.")][-4:]
    assert tail == [
        "operation.dispatched",
        "operation.uncertain",
        "operation.reconciling",
        "operation.confirmed",
    ]
    assert len(rig.fake.dispatch_log) == 1
    assert rig.sent_copies() == 1
    assert verify(rig.db).ok


def test_r5_a_taken_over_process_preserves_the_newer_reconcile_receipt(rig: WriteRig) -> None:
    op = rig.ready_send()
    receipts: list[str | None] = []

    def take_over_and_reconcile() -> None:
        rig.clock.advance(minutes=6)
        newer, changed = rig.engine(worker="rescuer").reconcile(op.handle)
        assert changed
        assert newer.state is S.CONFIRMED
        receipts.append(newer.receipt_json)

    rig.mail.during_dispatch = take_over_and_reconcile
    done = rig.engine(worker="original").do(op.handle)
    assert done.state is S.CONFIRMED
    assert done.receipt_json is not None
    assert receipts == [done.receipt_json]
    assert rig.sent_copies() == 1


@given(elapsed=st.integers(min_value=0, max_value=900))
@NO_FIXTURES
def test_r5_property_only_an_expired_lease_is_recovered(elapsed: int) -> None:
    with rig_scope() as rig:
        op = rig.ready_send()
        rig.mail.crash_after_dispatch = True
        from support.write_rig import SimulatedCrash

        with pytest.raises(SimulatedCrash):
            rig.engine(worker="doomed").do(op.handle)
        rig.clock.advance(seconds=elapsed)
        moved = rig.engine().recover()
        state = rig.engine().get(op.handle).state
        if elapsed < 300:
            assert (moved, state) == (0, S.DISPATCHED)
        else:
            assert (moved, state) == (1, S.UNCERTAIN)


# --- R6: bounded immediate settle ------------------------------------------------------


def test_r6_mail_settles_within_three_attempts_when_the_copy_appears(rig: WriteRig) -> None:
    op = rig.ready_send()
    unknown = ReconcileResult(status=ReconcileStatus.UNKNOWN)
    rig.mail.reconcile_script = [unknown, unknown, present()]
    rig.fake.arm(Fault.LOSE_RESPONSE_AFTER_APPLY)
    done = rig.engine().do(op.handle)
    assert done.state is S.CONFIRMED
    assert rig.mail.reconcile_calls >= 3
    assert rig.pauses[:2] == [10, 10]
    assert len(rig.fake.dispatch_log) == 1


def test_r6_mail_gives_up_after_three_attempts_and_stays_uncertain(rig: WriteRig) -> None:
    op = rig.ready_send()
    unknown = ReconcileResult(status=ReconcileStatus.UNKNOWN)
    rig.mail.reconcile_script = [unknown] * 20
    rig.fake.arm(Fault.LOSE_BEFORE_APPLY)
    with pytest.raises(ChamberlainError) as raised:
        rig.engine().do(op.handle)
    assert raised.value.code is ErrorCode.OPERATION_UNCERTAIN
    assert rig.mail.reconcile_calls == MAIL_SETTLE_ATTEMPTS
    assert sum(rig.pauses) <= MAIL_SETTLE_WINDOW_SECONDS


def test_r6_calendar_keeps_trying_for_up_to_a_minute(rig: WriteRig) -> None:
    proposal = rig.engine().propose_intent(rig.event("Retry lunch"))
    approved = rig.agent_approves(proposal)
    assert approved.operation is not None
    unknown = ReconcileResult(status=ReconcileStatus.UNKNOWN)
    rig.calendar.reconcile_script = [unknown] * 30
    rig.calendar.before_dispatch = lambda: rig.fake.arm(Fault.LOSE_BEFORE_APPLY)
    with pytest.raises(ChamberlainError) as raised:
        rig.engine().do(approved.operation.handle)
    assert raised.value.code is ErrorCode.OPERATION_UNCERTAIN
    assert rig.calendar.reconcile_calls == CALENDAR_SETTLE_ATTEMPTS
    assert sum(rig.pauses) <= 60


@given(answers=st.lists(st.sampled_from(["unknown", "queued", "absent"]), min_size=0, max_size=12))
@NO_FIXTURES
def test_r6_property_the_settle_is_bounded_and_never_frees_the_grant(answers: list[str]) -> None:
    with rig_scope(send_absence_provable=True) as rig:
        op = rig.ready_send()
        table = {
            "unknown": ReconcileResult(status=ReconcileStatus.UNKNOWN),
            "queued": ReconcileResult(status=ReconcileStatus.QUEUED),
            "absent": ReconcileResult(status=ReconcileStatus.ABSENT, proof=True),
        }
        rig.mail.reconcile_script = [table[a] for a in answers]
        rig.fake.arm(Fault.LOSE_BEFORE_APPLY)
        with pytest.raises(ChamberlainError):
            rig.engine().do(op.handle)
        stored = rig.engine().get(op.handle)
        assert stored.state is S.UNCERTAIN
        assert rig.mail.reconcile_calls <= MAIL_SETTLE_ATTEMPTS
        assert sum(rig.pauses) <= MAIL_SETTLE_WINDOW_SECONDS
        assert stored.grant is not None
        assert repo.live_uses(rig.db, [stored.grant], "x")[stored.grant] == 1
        assert stored.settled_at is None


# --- R7: a merged trash is applied with a warning --------------------------------------


def test_r7_an_applied_result_with_a_warning_is_confirmed_and_surfaces_the_warning(
    rig: WriteRig,
) -> None:
    op = rig.ready_send()

    def warn(result: DispatchResult) -> DispatchResult:
        assert result.receipt is not None
        warned = result.receipt.model_copy(
            update={"warnings": ("merged_existing_copy",), "read_state_preserved": False}
        )
        return DispatchResult(outcome=DispatchOutcome.APPLIED, receipt=warned)

    rig.mail.result_transform = warn
    done = rig.engine().do(op.handle)
    assert done.state is S.CONFIRMED
    assert rig.engine().view(done).warnings == ("merged_existing_copy",)


def test_r5_late_worker_cannot_replace_a_confirmed_absent_reconcile(provable_rig: WriteRig) -> None:
    op = provable_rig.ready_send()

    def take_over_and_reconcile() -> None:
        provable_rig.clock.advance(hours=2)
        assert provable_rig.engine(worker="rescuer").recover() == 1
        provable_rig.mail.reconcile_script = [
            ReconcileResult(status=ReconcileStatus.ABSENT, proof=True)
        ]
        newer, changed = provable_rig.engine(worker="rescuer").reconcile(op.handle)
        assert changed
        assert newer.state is S.CONFIRMED_ABSENT

    provable_rig.mail.during_dispatch = take_over_and_reconcile
    with pytest.raises(ChamberlainError) as raised:
        provable_rig.engine(worker="original").do(op.handle)
    assert raised.value.code is ErrorCode.OPERATION_ALREADY_SETTLED
    assert provable_rig.engine().get(op.handle).state is S.CONFIRMED_ABSENT
