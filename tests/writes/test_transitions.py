"""Every state pair not in TRANSITIONS is refused (04-writes.md W-6, W-7)."""

import itertools

import pytest

from chamberlain.domain.grants import GRANT_TRANSITIONS, GrantState
from chamberlain.domain.intents import IntentKind
from chamberlain.domain.operations import (
    TERMINAL_STATES,
    TRANSITIONS,
    OperationState,
    check_transition,
)
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.testing.write_rig import MAIL, WriteRig
from chamberlain.writes import repo
from chamberlain.writes.transitions import build_receipt, move_operation

S = OperationState
ARROWS = {
    (S.PROPOSED, S.AUTHORISED),
    (S.PROPOSED, S.CANCELLED),
    (S.AUTHORISED, S.PREPARED),
    (S.AUTHORISED, S.FAILED_CLEAN),
    (S.AUTHORISED, S.CANCELLED),
    (S.PREPARED, S.DISPATCHED),
    (S.PREPARED, S.FAILED_CLEAN),
    (S.DISPATCHED, S.CONFIRMED),
    (S.DISPATCHED, S.FAILED_CLEAN),
    (S.DISPATCHED, S.UNCERTAIN),
    (S.UNCERTAIN, S.RECONCILING),
    (S.UNCERTAIN, S.CONFIRMED),
    (S.UNCERTAIN, S.CONFIRMED_ABSENT),
    (S.UNCERTAIN, S.ABANDONED),
    (S.UNCERTAIN, S.SUPERSEDED),
    (S.RECONCILING, S.CONFIRMED),
    (S.RECONCILING, S.CONFIRMED_ABSENT),
    (S.RECONCILING, S.UNCERTAIN),
}


def test_w6_the_legal_transitions_are_exactly_the_arrows_of_the_diagram() -> None:
    legal = {(a, b) for a, targets in TRANSITIONS.items() for b in targets}
    assert legal == ARROWS
    assert {
        S.CONFIRMED,
        S.FAILED_CLEAN,
        S.CONFIRMED_ABSENT,
        S.CANCELLED,
        S.ABANDONED,
        S.SUPERSEDED,
    } == TERMINAL_STATES


def _make(rig: WriteRig, state: OperationState) -> str:
    handle = f"op_{state.value.replace('_', '')[:12]:0<12}abcd"
    repo.insert_operation(
        rig.db,
        handle=handle,
        idempotency_key=f"key-{state.value}",
        kind=IntentKind.MAIL_SEND,
        source_id=MAIL,
        card=None,
        session=None,
        intent_sha256="d" * 64,
        intent_json="{}",
        refs_json="{}",
        at="2026-01-05T09:00:00Z",
    )
    rig.db.execute("UPDATE operations SET state = ? WHERE handle = ?", (state.value, handle))
    return handle


@pytest.mark.parametrize(
    ("before", "after"), list(itertools.product(list(S), list(S))), ids=lambda s: s.value
)
def test_w6_w7_every_pair_is_taken_or_refused_before_touching_the_store(
    rig: WriteRig, before: OperationState, after: OperationState
) -> None:
    handle = _make(rig, before)
    row = repo.get_operation(rig.db, handle)
    assert row is not None
    audit_before = repo.audit_count(rig.db)
    receipt = (
        build_receipt(row, after, "2026-01-05T09:00:00Z") if after in TERMINAL_STATES else None
    )
    columns = {} if receipt is None else {"receipt_json": receipt}
    if (before, after) in ARROWS:
        moved = move_operation(
            rig.db, rig.clock, rig.ctx.services.audit, row, after, actor="t", summary="s", **columns
        )
        assert moved is not None
        assert moved.state is after
        assert repo.audit_count(rig.db) == audit_before + 1
        return
    with pytest.raises(ChamberlainError) as raised:
        move_operation(
            rig.db, rig.clock, rig.ctx.services.audit, row, after, actor="t", summary="s", **columns
        )
    assert raised.value.code is ErrorCode.INTERNAL_UNEXPECTED
    assert repo.get_operation(rig.db, handle) == row
    assert repo.audit_count(rig.db) == audit_before
    with pytest.raises(ChamberlainError):
        check_transition(before, after)


def test_w7_a_terminal_transition_without_a_receipt_is_refused(rig: WriteRig) -> None:
    handle = _make(rig, S.DISPATCHED)
    row = repo.get_operation(rig.db, handle)
    assert row is not None
    with pytest.raises(ChamberlainError):
        move_operation(
            rig.db, rig.clock, rig.ctx.services.audit, row, S.CONFIRMED, actor="t", summary="s"
        )
    assert repo.get_operation(rig.db, handle) == row


def test_w7_a_lost_race_changes_nothing_and_writes_no_audit_row(rig: WriteRig) -> None:
    handle = _make(rig, S.DISPATCHED)
    row = repo.get_operation(rig.db, handle)
    assert row is not None
    rig.db.execute("UPDATE operations SET state = 'uncertain' WHERE handle = ?", (handle,))
    before = repo.audit_count(rig.db)
    moved = move_operation(
        rig.db,
        rig.clock,
        rig.ctx.services.audit,
        row,
        S.FAILED_CLEAN,
        actor="t",
        summary="s",
        receipt_json=build_receipt(row, S.FAILED_CLEAN, "2026-01-05T09:00:00Z"),
    )
    assert moved is None
    assert repo.audit_count(rig.db) == before
    assert repo.get_operation(rig.db, handle).state is S.UNCERTAIN  # type: ignore[union-attr]


def test_w6_a_terminal_operation_state_is_final_in_the_database_too(rig: WriteRig) -> None:
    import sqlite3

    handle = _make(rig, S.CONFIRMED)
    with pytest.raises(sqlite3.DatabaseError):
        rig.db.execute("UPDATE operations SET state = 'proposed' WHERE handle = ?", (handle,))
    with pytest.raises(sqlite3.DatabaseError):
        rig.db.execute(
            "UPDATE operations SET intent_sha256 = ? WHERE handle = ?", ("e" * 64, handle)
        )
    with pytest.raises(sqlite3.DatabaseError):
        rig.db.execute("DELETE FROM operations WHERE handle = ?", (handle,))


def test_g2_g4_the_grant_transitions_are_the_lifecycle_diagram() -> None:
    G = GrantState
    assert GRANT_TRANSITIONS[G.PROPOSED] == {G.ACTIVE, G.DECLINED, G.LAPSED}
    assert GRANT_TRANSITIONS[G.ACTIVE] == {G.SPENT, G.REVOKED, G.EXPIRED, G.SUSPENDED}
    assert GRANT_TRANSITIONS[G.SUSPENDED] == {G.ACTIVE, G.REVOKED, G.EXPIRED}
    for final in (G.SPENT, G.REVOKED, G.EXPIRED, G.DECLINED, G.LAPSED):
        assert GRANT_TRANSITIONS[final] == frozenset()
