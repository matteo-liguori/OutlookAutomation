"""Old safety tests with no row in the ported-intent table, ported by intent.

Sources (read-only): `tests/test_outlook_api.py` (`check_send`, the send journal) and
`tests/test_debug_pass.py` (refusals, immutable triggers, atomic token files), plus the
P-9, P-11 and P-16 cases of the writes brief.
"""

import contextlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from chamberlain.connectors.fake import Fault
from chamberlain.domain.calendar import EventFields
from chamberlain.domain.canonical import sha256_bytes
from chamberlain.domain.grants import GrantState
from chamberlain.domain.handles import HandleKind
from chamberlain.domain.intents import (
    AttachmentRef,
    CalendarUpdate,
    MailDraftSave,
    MailDraftUpdate,
    MailSend,
)
from chamberlain.domain.operations import OperationState, ReconcileResult, ReconcileStatus
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.writes import repo
from chamberlain.writes.confirm import create_token
from chamberlain.writes.maintenance import maintain
from support.write_rig import CALENDAR, MAIL, WriteRig

S = OperationState


def code_of(action) -> ErrorCode:  # type: ignore[no-untyped-def]
    with pytest.raises(ChamberlainError) as raised:
        action()
    return raised.value.code


def test_w21_check_send_a_stale_one_off_is_refused_and_a_missing_one_never_sends(
    rig: WriteRig,
) -> None:
    """Old: test_send_requires_exact_scope_and_unchanged_attachments (stale and empty cases)."""
    op = rig.ready_send()
    rig.clock.advance(hours=24 * 8)
    assert code_of(lambda: rig.engine().do(op.handle)) is ErrorCode.GRANT_NOT_ACTIVE
    assert rig.fake.dispatch_log == []
    assert rig.sent_copies() == 0
    bare = rig.engine().propose_intent(rig.send(subject="Never approved"))
    assert bare.operation is not None
    assert code_of(lambda: rig.engine().do(bare.operation.handle)) is ErrorCode.GRANT_REQUIRED  # type: ignore[union-attr]
    assert rig.fake.dispatch_log == []


def test_w21_an_authorised_operation_whose_grant_expires_before_dispatch_is_released(
    rig: WriteRig,
) -> None:
    rig.know("friend@example.org")
    proposal = rig.engine().propose_intent(rig.send())
    approved = rig.agent_approves(proposal)
    assert approved.operation is not None
    rig.mail.crash_in_prepare = True
    from support.write_rig import SimulatedCrash

    with pytest.raises(SimulatedCrash):
        rig.engine().do(approved.operation.handle)
    assert rig.engine().get(approved.operation.handle).state is S.AUTHORISED
    rig.mail.crash_in_prepare = False
    rig.clock.advance(hours=24 * 8)
    handle = approved.operation.handle
    assert code_of(lambda: rig.engine().do(handle)) is ErrorCode.GRANT_NOT_ACTIVE
    assert rig.engine().get(handle).state is S.FAILED_CLEAN
    assert rig.sent_copies() == 0
    assert rig.grant_for(approved.operation).state is GrantState.ACTIVE


def test_g7_using_a_standing_grant_never_renews_or_moves_its_review_date(rig: WriteRig) -> None:
    """Old: test_due_review_is_required_in_handoff_but_delivery_does_not_renew."""
    standing = rig.standing_grant()
    before = repo.get_grant(rig.db, standing.handle)
    for subject in ("A", "B"):
        proposal = rig.engine().propose_intent(rig.send(subject=subject))
        assert proposal.operation is not None
        rig.engine().do(proposal.operation.handle)
    assert repo.get_grant(rig.db, standing.handle) == before


def test_g3_g23_a_failed_review_changes_nothing_and_writes_no_audit_row(rig: WriteRig) -> None:
    """Old: test_failed_review_rolls_back_cross_thread_evidence_copy."""
    standing = rig.standing_grant()
    rig.engine().grants.revoke(standing.handle, actor="human:viewer")
    audit_before = repo.audit_count(rig.db)
    grant_before = repo.get_grant(rig.db, standing.handle)
    assert code_of(lambda: rig.engine().grants.keep(standing.handle, actor="human:viewer")) is (
        ErrorCode.GRANT_NOT_ACTIVE
    )
    assert repo.audit_count(rig.db) == audit_before
    assert repo.get_grant(rig.db, standing.handle) == grant_before


def test_g18_the_token_file_is_written_atomically_and_leaves_nothing_behind(
    tmp_path: Path,
) -> None:
    """Old: test_token_files_are_written_atomically_and_restricted."""
    token = create_token(tmp_path)
    run = tmp_path / "run"
    assert sorted(path.name for path in run.iterdir()) == ["viewer.token"]
    assert (run / "viewer.token").read_text(encoding="utf-8") == token
    newer = create_token(tmp_path)
    assert (run / "viewer.token").read_text(encoding="utf-8") == newer
    assert sorted(path.name for path in run.iterdir()) == ["viewer.token"]


def test_w10_a_refusal_before_anything_changed_releases_the_grant_not_uncertain(
    rig: WriteRig,
) -> None:
    """Old: test_refusals_release_the_approval_instead_of_marking_it_uncertain."""
    first = rig.ready_send()
    rig.fake.arm(Fault.BUSY_BEFORE_APPLY)
    with contextlib.suppress(ChamberlainError):
        rig.engine().do(first.handle)
    failed = rig.engine().get(first.handle)
    assert failed.state is S.FAILED_CLEAN
    assert not rig.engine().view(failed).may_have_happened


def test_p16_a_calendar_update_with_a_stale_etag_fails_clean_and_dispatches_nothing(
    rig: WriteRig,
) -> None:
    fields = EventFields(
        title="Review",
        start=datetime(2026, 2, 3, 10, tzinfo=UTC),
        end=datetime(2026, 2, 3, 11, tzinfo=UTC),
    )
    locator = rig.fake.calendar.add_event(CALENDAR, fields)
    handle = rig.refs.remember(HandleKind.CALENDAR_EVENT, CALENDAR, locator)
    update = CalendarUpdate(
        source=CALENDAR,
        event_ref=handle,
        expected_etag='"an-old-version"',
        event=fields.model_copy(update={"title": "Review (moved)"}),
    )
    proposal = rig.engine().propose_intent(update)
    assert proposal.requirement.value == "agent_evidence"
    proposal = rig.agent_approves(proposal, "Yes, move the review.")
    assert proposal.operation is not None
    assert code_of(lambda: rig.engine().do(proposal.operation.handle)) is ErrorCode.REMOTE_CHANGED  # type: ignore[union-attr]
    op = rig.engine().get(proposal.operation.handle)
    assert op.state is S.FAILED_CLEAN
    assert rig.fake.dispatch_log == []
    grant = rig.grant_for(op)
    assert grant.state is GrantState.ACTIVE


def test_p11_an_uncertain_send_reconciles_after_its_attachment_is_gone(rig: WriteRig) -> None:
    """Old: test_send_reconciliation_never_resubmits_or_needs_original_attachment_file."""
    data = b"bytes that will vanish"
    artifact = rig.artifacts.put(data, owner="ses_testsession01")
    rig.know("friend@example.org")
    intent = MailSend(
        source=MAIL,
        to=("friend@example.org",),
        subject="With a file",
        body="See it.",
        attachments=(
            AttachmentRef(artifact=artifact, sha256=sha256_bytes(data), filename="a.bin"),
        ),
    )
    approved = rig.agent_approves(rig.engine().propose_intent(intent))
    assert approved.operation is not None
    unknown = ReconcileResult(status=ReconcileStatus.UNKNOWN)
    rig.mail.reconcile_script = [unknown] * 3
    rig.fake.arm(Fault.LOSE_RESPONSE_AFTER_APPLY)
    assert code_of(lambda: rig.engine().do(approved.operation.handle)) is (  # type: ignore[union-attr]
        ErrorCode.OPERATION_UNCERTAIN
    )
    del rig.artifacts.blobs[artifact]
    settled, changed = rig.engine().reconcile(approved.operation.handle)
    assert changed
    assert settled.state is S.CONFIRMED
    assert len(rig.fake.dispatch_log) == 1
    assert rig.sent_copies() == 1


def test_p9_a_draft_update_with_the_wrong_old_hash_fails_clean(rig: WriteRig) -> None:
    """Old: test_draft_update_requires_old_raw_hash_and_exact_bound_folder."""
    saved = rig.agent_approves(
        rig.engine().propose_intent(
            MailDraftSave(source=MAIL, to=("a@example.org",), subject="Draft", body="v1")
        )
    )
    assert saved.operation is not None
    done = rig.engine().do(saved.operation.handle)
    draft = rig.engine().view(done).result_ref
    assert draft is not None
    stored = rig.fake.world.mail[MAIL][-1]
    assert stored.raw_sha256 is not None
    update = MailDraftUpdate(
        source=MAIL,
        draft=draft,
        expected_sha256="0" * 64,
        to=("a@example.org",),
        subject="Draft",
        body="v2",
    )
    proposal = rig.agent_approves(rig.engine().propose_intent(update))
    assert proposal.operation is not None
    assert code_of(lambda: rig.engine().do(proposal.operation.handle)) is ErrorCode.REMOTE_CHANGED  # type: ignore[union-attr]
    assert rig.engine().get(proposal.operation.handle).state is S.FAILED_CLEAN
    assert stored.body == "v1"
    right = MailDraftUpdate(
        source=MAIL,
        draft=draft,
        expected_sha256=stored.raw_sha256,
        to=("a@example.org",),
        subject="Draft",
        body="v2",
    )
    again = rig.agent_approves(rig.engine().propose_intent(right))
    assert again.operation is not None
    assert rig.engine().do(again.operation.handle).state is S.CONFIRMED
    assert maintain(rig.ctx, rig.policy).recovered == 0
