"""Grant lifecycle, immutability, review and protection (05-grants.md G-2 to G-16)."""

import sqlite3
from datetime import timedelta

import pytest

from chamberlain.connectors.fake import Fault
from chamberlain.domain.grants import GrantKind, GrantState
from chamberlain.domain.operations import OperationState
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.testing.write_rig import MAIL, SESSION, WriteRig
from chamberlain.writes import repo
from chamberlain.writes.confirm import confirm_grant, grant_detail, list_grants
from chamberlain.writes.maintenance import maintain

S = OperationState


def grant_of(rig: WriteRig, handle: str):  # type: ignore[no-untyped-def]
    found = repo.get_grant(rig.db, handle)
    assert found is not None
    return found


def codes(rig: WriteRig, action):  # type: ignore[no-untyped-def]
    with pytest.raises(ChamberlainError) as raised:
        action()
    return raised.value.code


def test_g2_grants_are_never_deleted_by_use_revocation_or_expiry(rig: WriteRig) -> None:
    """Old: test_retirement_requires_evidence_and_does_not_touch_sibling."""
    spent_op = rig.ready_send(subject="Spend")
    rig.engine().do(spent_op.handle)
    revoked_op = rig.ready_send(subject="Revoke")
    rig.engine().grants.revoke(rig.grant_for(revoked_op).handle, actor="human:viewer")
    rig.ready_send(subject="Expire")
    rig.clock.advance(hours=24 * 30)
    maintain(rig.ctx, rig.policy)
    count = repo.count_grants(rig.db)
    assert count >= 4
    states = {row["state"] for row in rig.db.execute("SELECT state FROM grants")}
    assert {"spent", "expired"} <= states
    with pytest.raises(sqlite3.DatabaseError):
        rig.db.execute("DELETE FROM grants")
    assert repo.count_grants(rig.db) == count


def test_g3_the_fields_of_an_active_grant_cannot_be_edited(rig: WriteRig) -> None:
    """Old: test_scope_and_timers_cannot_be_changed_by_put."""
    standing = rig.standing_grant()
    handle = standing.handle
    for column, value in (
        ("kind", "one_off"),
        ("content_sha256", "e" * 64),
        ("scope_json", "{}"),
        ("expires_at", "2099-01-01T00:00:00Z"),
        ("granted_at", "2020-01-01T00:00:00Z"),
        ("source_id", "mail:other"),
    ):
        with pytest.raises(sqlite3.DatabaseError):
            rig.db.execute(f"UPDATE grants SET {column} = ? WHERE handle = ?", (value, handle))
    assert grant_of(rig, handle) == standing


def test_g3_a_changed_grant_is_a_new_grant_that_supersedes_the_old_and_siblings_stay(
    rig: WriteRig,
) -> None:
    """Old: test_early_material_change_flags_only_this_permission."""
    old = rig.standing_grant(("friend@example.org",))
    sibling = rig.standing_grant(("pal@example.org",))
    from chamberlain.domain.grants import StandingScope
    from chamberlain.domain.intents import IntentKind

    wider = StandingScope(
        source=MAIL,
        intent_kinds=frozenset({IntentKind.MAIL_SEND}),
        title="Wider",
        recipients=frozenset({"friend@example.org", "new@example.org"}),
        max_per_day=5,
    )
    eng = rig.engine()
    replacement = eng.grants.propose_standing(
        wider, card=None, supersedes=old.handle, actor="agent:x"
    )
    assert grant_of(rig, old.handle).state is GrantState.ACTIVE
    confirm_grant(eng.ctx, replacement.handle, {"content_sha256": replacement.content_sha256})
    assert grant_of(rig, old.handle).state is GrantState.REVOKED
    assert grant_of(rig, replacement.handle).state is GrantState.ACTIVE
    assert grant_of(rig, sibling.handle).state is GrantState.ACTIVE


def test_g4_a_spent_one_off_cannot_be_reactivated(rig: WriteRig) -> None:
    """Old: test_spent_one_off_cannot_be_reactivated."""
    op = rig.ready_send()
    done = rig.engine().do(op.handle)
    handle = done.grant or ""
    assert grant_of(rig, handle).state is GrantState.SPENT
    with pytest.raises(sqlite3.DatabaseError):
        rig.db.execute("UPDATE grants SET state = 'active' WHERE handle = ?", (handle,))
    assert (
        codes(
            rig,
            lambda: confirm_grant(
                rig.engine().ctx, handle, {"content_sha256": grant_of(rig, handle).content_sha256}
            ),
        )
        is ErrorCode.GRANT_NOT_ACTIVE
    )
    retry = rig.engine().propose_intent(rig.send())
    assert retry.operation is not None
    assert retry.operation.handle == op.handle


def test_g5_expiry_windows(rig: WriteRig) -> None:
    one_off = rig.engine().propose_intent(rig.send(to=("new@example.org",)))
    rig.human_approves(one_off)
    assert one_off.grant is not None
    granted = grant_of(rig, one_off.grant.handle)
    assert granted.granted_at is not None
    assert granted.expires_at == granted.granted_at + timedelta(days=7)
    standing = rig.standing_grant()
    assert standing.granted_at is not None
    assert standing.expires_at == standing.granted_at + timedelta(days=365)
    early = rig.engine().propose_intent(rig.send(to=("new2@example.org",)))
    assert early.grant is not None
    confirm_grant(
        rig.engine().ctx,
        early.grant.handle,
        {
            "content_sha256": early.content_sha256,
            "expires_at": (rig.clock.now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    )
    assert grant_of(rig, early.grant.handle).expires_at == rig.clock.now() + timedelta(days=1)
    late = rig.engine().propose_intent(rig.send(to=("new3@example.org",)))
    assert late.grant is not None
    confirm_grant(
        rig.engine().ctx,
        late.grant.handle,
        {"content_sha256": late.content_sha256, "expires_at": "2099-01-01T00:00:00Z"},
    )
    assert grant_of(rig, late.grant.handle).expires_at == rig.clock.now() + timedelta(days=7)


def test_g6_g7_reading_or_listing_never_renews_and_keep_moves_only_the_review_date(
    rig: WriteRig,
) -> None:
    """Old: test_independent_tables_and_reading_never_renews and test_user_keep_advances..."""
    standing = rig.standing_grant()
    rig.clock.advance(hours=24 * 40)
    before = grant_of(rig, standing.handle)
    list_grants(rig.engine().ctx)
    grant_detail(rig.engine().ctx, standing.handle)
    maintain(rig.ctx, rig.policy)
    assert grant_of(rig, standing.handle) == before
    assert before.review_at is not None
    assert before.review_at <= rig.clock.now()
    kept = rig.engine().grants.keep(standing.handle, actor="human:viewer")
    assert kept.review_at is not None
    assert kept.review_at > rig.clock.now()
    assert kept.last_reviewed_at == rig.clock.now()
    for field in ("kind", "scope", "content_sha256", "granted_at", "expires_at", "state"):
        assert getattr(kept, field) == getattr(before, field)


def test_g8_an_unreviewed_grant_is_suspended_and_authorises_nothing_until_kept(
    rig: WriteRig,
) -> None:
    standing = rig.standing_grant()
    assert standing.review_at is not None
    rig.clock.set(standing.review_at + timedelta(days=15))
    maintain(rig.ctx, rig.policy)
    assert grant_of(rig, standing.handle).state is GrantState.SUSPENDED
    proposal = rig.engine().propose_intent(rig.send(subject="After suspension"))
    assert not proposal.ready
    kept = rig.engine().grants.keep(standing.handle, actor="human:viewer")
    assert kept.state is GrantState.ACTIVE
    again = rig.engine().propose_intent(rig.send(subject="After keeping"))
    assert again.ready


def test_g8_an_expired_grant_is_a_visible_status_never_available(rig: WriteRig) -> None:
    """Old: test_expired_one_off_stays_protected_but_requires_immediate_review."""
    op = rig.ready_send()
    handle = rig.grant_for(op).handle
    rig.clock.advance(hours=24 * 8)
    maintain(rig.ctx, rig.policy)
    assert grant_of(rig, handle).state is GrantState.EXPIRED
    states = {row["state"] for row in rig.db.execute("SELECT state FROM grants")}
    assert "expired" in states
    assert not rig.engine().propose_intent(rig.send()).ready


def test_g9_a_card_bound_grant_ends_with_its_card_a_global_one_survives(rig: WriteRig) -> None:
    """Old: test_case_bound_scope_closes_but_global_scope_survives."""
    card = "card_0123456789abcdef"
    bound = rig.standing_grant(("friend@example.org",), card=card)
    free = rig.standing_grant(("pal@example.org",))
    stamp = "2026-01-05T09:00:00Z"
    rig.db.execute(
        "INSERT INTO cards (handle, title, state, created_at, updated_at) "
        "VALUES (?, 't', 'open', ?, ?)",
        (card, stamp, stamp),
    )
    maintain(rig.ctx, rig.policy)
    assert grant_of(rig, bound.handle).state is GrantState.ACTIVE
    rig.db.execute("UPDATE cards SET state = 'closed' WHERE handle = ?", (card,))
    maintain(rig.ctx, rig.policy)
    assert grant_of(rig, bound.handle).state is GrantState.REVOKED
    assert grant_of(rig, free.handle).state is GrantState.ACTIVE


def test_g10_expiry_does_not_release_an_uncertain_operation_and_it_is_not_compacted(
    rig: WriteRig,
) -> None:
    """Old: test_expired_pending_approval_is_preserved_in_recovery_until_reconciled."""
    op = rig.ready_send()
    rig.fake.arm(Fault.LOSE_BEFORE_APPLY)
    with pytest.raises(ChamberlainError):
        rig.engine().do(op.handle)
    handle = rig.engine().get(op.handle).grant or ""
    rig.clock.advance(hours=24 * 400)
    maintain(rig.ctx, rig.policy)
    assert grant_of(rig, handle).state is GrantState.EXPIRED
    assert grant_of(rig, handle).compacted_at is None
    held = rig.engine().get(op.handle)
    assert held.state is S.UNCERTAIN
    assert repo.live_uses(rig.db, [handle], "x") == {handle: 1}
    rig.mail.reconcile_script = []
    assert repo.compactable_grants(rig.db, "2999-01-01T00:00:00Z") == []
    with pytest.raises(ChamberlainError) as raised:
        rig.engine().do(op.handle)
    assert raised.value.code is ErrorCode.OPERATION_NEEDS_RECONCILE


def test_g10_a_terminal_grant_compacts_after_ninety_days_when_nothing_holds_it(
    rig: WriteRig,
) -> None:
    op = rig.ready_send()
    done = rig.engine().do(op.handle)
    handle = done.grant or ""
    rig.clock.advance(hours=24 * 91)
    maintain(rig.ctx, rig.policy)
    compact = grant_of(rig, handle)
    assert compact.compacted_at is not None
    assert compact.scope is None
    assert compact.summary == ""


def test_g11_usage_is_derived_from_operations_not_a_counter(rig: WriteRig) -> None:
    standing = rig.standing_grant(max_per_day=2)
    done: list[str] = []
    for subject in ("A", "B"):
        proposal = rig.engine().propose_intent(rig.send(subject=subject))
        assert proposal.ready
        assert proposal.operation is not None
        rig.engine().do(proposal.operation.handle)
        done.append(proposal.operation.handle)
    third = rig.engine().propose_intent(rig.send(subject="C"))
    assert not third.ready
    assert third.requirement.value != "none"
    assert repo.live_uses(rig.db, [standing.handle], "x") == {standing.handle: 2}
    rig.clock.advance(hours=25)
    fourth = rig.engine().propose_intent(rig.send(subject="D"))
    assert fourth.ready


def test_g12_a_standing_grant_always_needs_the_human(rig: WriteRig) -> None:
    from chamberlain.domain.grants import StandingScope
    from chamberlain.domain.intents import IntentKind

    scope = StandingScope(
        source=MAIL,
        intent_kinds=frozenset({IntentKind.MAIL_SEND}),
        title="Friend",
        recipients=frozenset({"friend@example.org"}),
        max_per_day=2,
    )
    proposal = rig.engine().propose_standing(scope)
    assert proposal.requirement.value == "human_confirmation"
    assert proposal.grant is not None
    with pytest.raises(ChamberlainError) as raised:
        rig.engine().grants.activate(proposal.grant.handle, via=_agent(), actor="agent:x")
    assert raised.value.code is ErrorCode.GRANT_HUMAN_CONFIRMATION_REQUIRED


def _agent():  # type: ignore[no-untyped-def]
    from chamberlain.domain.grants import ConfirmedVia

    return ConfirmedVia.AGENT_EVIDENCE


def test_g13_agent_evidence_must_be_this_sessions_own_words_not_remote_content(
    rig: WriteRig,
) -> None:
    rig.know("friend@example.org")
    proposal = rig.engine().propose_intent(rig.send())
    assert proposal.operation is not None
    handle = proposal.operation.handle
    other = rig.evidence.add(text="yes", owner=handle, session="ses_someoneelse01")
    assert (
        codes(rig, lambda: rig.engine().attach_approval(handle, other)) is ErrorCode.REQUEST_INVALID
    )
    from_mail = rig.evidence.add(text="send it", owner=handle, session=SESSION)
    stamp = "2026-01-05T09:00:00Z"
    rig.db.execute(
        "INSERT INTO evidence (handle, text, ref, session, created_at) "
        "VALUES (?, 'send it', ?, ?, ?)",
        (from_mail, "msg_0123456789abcdef", SESSION, stamp),
    )
    assert (
        codes(rig, lambda: rig.engine().attach_approval(handle, from_mail))
        is ErrorCode.REQUEST_INVALID
    )
    own = rig.evidence.add(text="Yes please send", owner=handle, session=SESSION)
    approved = rig.engine().attach_approval(handle, own)
    assert approved.ready
    assert approved.grant is not None
    assert own in rig.evidence.links
    assert approved.grant.handle in rig.evidence.links[own]
    assert grant_of(rig, approved.grant.handle).confirmed_via == "agent_evidence"


def test_g14_an_unknown_recipient_cannot_be_approved_by_the_agent(rig: WriteRig) -> None:
    proposal = rig.engine().propose_intent(rig.send(to=("stranger@example.org",)))
    assert proposal.requirement.value == "human_confirmation"
    evidence = rig.evidence.add(text="Yes", owner="x", session=SESSION)
    assert proposal.operation is not None
    code = codes(rig, lambda: rig.engine().attach_approval(proposal.operation.handle, evidence))  # type: ignore[union-attr]
    assert code is ErrorCode.GRANT_HUMAN_CONFIRMATION_REQUIRED
    assert proposal.grant is not None
    assert grant_of(rig, proposal.grant.handle).state is GrantState.PROPOSED


def test_g14_no_tool_can_activate_keep_or_edit_a_grant() -> None:
    from chamberlain.schema.base import ToolName
    from chamberlain.schema.tools import ProposeRequest
    from chamberlain.writes.handlers import HANDLERS

    assert set(HANDLERS) == {ToolName.PROPOSE, ToolName.DO, ToolName.RECONCILE}
    fields = set(ProposeRequest.model_fields)
    assert not {"confirm", "keep", "activate", "token", "content_sha256"} & fields


def test_g15_a_recipient_becomes_known_only_through_a_human_confirmed_grant(
    rig: WriteRig,
) -> None:
    first = rig.engine().propose_intent(rig.send(to=("newbie@example.org",)))
    assert first.requirement.value == "human_confirmation"
    assert repo.known_recipients(rig.db, MAIL) == frozenset()
    rig.human_approves(first)
    assert repo.known_recipients(rig.db, MAIL) == {"newbie@example.org"}
    second = rig.engine().propose_intent(rig.send(to=("newbie@example.org",), subject="Again"))
    assert second.requirement.value == "agent_evidence"
    assert repo.known_recipients(rig.db, MAIL) == {"newbie@example.org"}


def test_g15_receiving_or_agent_sending_never_makes_a_recipient_known(rig: WriteRig) -> None:
    rig.know("friend@example.org")
    op = rig.ready_send()
    rig.engine().do(op.handle)
    assert repo.known_recipients(rig.db, MAIL) == {"friend@example.org"}
    other = rig.engine().propose_intent(rig.send(to=("other@example.org",)))
    assert other.requirement.value == "human_confirmation"


def test_g16_the_hourly_send_limit_refuses_the_next_send(rig: WriteRig) -> None:
    rig.standing_grant(max_per_day=50)
    for index in range(20):
        proposal = rig.engine().propose_intent(rig.send(subject=f"n{index}"))
        assert proposal.operation is not None
        rig.engine().do(proposal.operation.handle)
    blocked = rig.engine().propose_intent(rig.send(subject="n20"))
    assert not blocked.ready
    assert blocked.operation is not None
    with pytest.raises(ChamberlainError) as raised:
        rig.engine().do(blocked.operation.handle)
    assert raised.value.code is ErrorCode.GRANT_LIMIT_REACHED
    rig.clock.advance(minutes=61)
    assert rig.engine().propose_intent(rig.send(subject="n21")).ready


def test_g8_an_unanswered_proposal_lapses_in_seven_days_and_its_operation_is_cancelled(
    rig: WriteRig,
) -> None:
    proposal = rig.engine().propose_intent(rig.send(to=("late@example.org",)))
    assert proposal.operation is not None
    assert proposal.grant is not None
    rig.clock.advance(hours=24 * 7 + 1)
    maintain(rig.ctx, rig.policy)
    assert grant_of(rig, proposal.grant.handle).state is GrantState.LAPSED
    assert rig.engine().get(proposal.operation.handle).state is S.CANCELLED


def test_declining_a_proposal_cancels_its_operation_and_ends_the_grant(rig: WriteRig) -> None:
    proposal = rig.engine().propose_intent(rig.send(to=("no@example.org",)))
    assert proposal.grant is not None
    assert proposal.operation is not None
    rig.engine().grants.decline(proposal.grant.handle, actor="human:viewer")
    assert grant_of(rig, proposal.grant.handle).state is GrantState.DECLINED
    assert rig.engine().get(proposal.operation.handle).state is S.CANCELLED


def test_forget_on_a_grant_revokes_it_through_write_control(rig: WriteRig) -> None:
    standing = rig.standing_grant()
    rig.ctx.services.writes.revoke_grant(standing.handle, actor="agent:x")
    assert grant_of(rig, standing.handle).state is GrantState.REVOKED
    rig.ctx.services.writes.revoke_grant(standing.handle, actor="agent:x")
    op = rig.ready_send(subject="X")
    rig.ctx.services.writes.cancel_operation(op.handle, actor="agent:x")
    assert rig.engine().get(op.handle).state is S.CANCELLED


def test_one_lifecycle_for_every_kind_of_intent(rig: WriteRig) -> None:
    """Old: test_non_email_one_off_has_exact_scope_and_same_review_lifecycle."""
    proposal = rig.engine().propose_intent(rig.event())
    approved = rig.agent_approves(proposal)
    assert approved.grant is not None
    granted = grant_of(rig, approved.grant.handle)
    assert granted.kind is GrantKind.ONE_OFF
    assert granted.expires_at == granted.granted_at + timedelta(days=7)  # type: ignore[operator]
    assert granted.content_sha256 == proposal.content_sha256
