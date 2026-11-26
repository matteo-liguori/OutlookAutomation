"""The audit log: append-only hash chain, tamper evidence, pruning (05-grants.md G-20 to G-23)."""

import itertools
import sqlite3
from datetime import timedelta

import pytest

from chamberlain.limits import AUDIT_DAYS
from chamberlain.store.db import transaction
from chamberlain.store.schema import get_state
from chamberlain.writes import repo
from chamberlain.writes.audit import CHECKPOINT_KEY, HEAD_KEY, AuditChain, prune, verify
from support.write_rig import WriteRig


def audit_of(rig: WriteRig) -> AuditChain:
    return AuditChain(rig.db, rig.clock)


def fill(rig: WriteRig, count: int, *, kind: str = "tool.call") -> None:
    audit = audit_of(rig)
    for index in range(count):
        audit.append(actor="agent:ses_x", kind=kind, subject=None, summary=f"row {index}")


def lift_guards(rig: WriteRig) -> None:
    """What an attacker with the file would do first."""
    rig.db.execute("DROP TRIGGER audit_log_no_update")
    rig.db.execute("DROP TRIGGER audit_log_guarded_delete")


def test_g20_every_row_chains_to_the_previous_one_and_the_chain_verifies(rig: WriteRig) -> None:
    fill(rig, 5)
    report = verify(rig.db)
    assert report.ok
    assert report.rows == 5
    rows = repo.audit_rows(rig.db)
    assert rows[0].prev_hash == "0" * 64
    for before, after in itertools.pairwise(rows):
        assert after.prev_hash == before.hash
        assert after.seq == before.seq + 1


def test_g20_the_database_itself_refuses_to_edit_or_delete_audit_rows(rig: WriteRig) -> None:
    fill(rig, 3)
    with pytest.raises(sqlite3.DatabaseError):
        rig.db.execute("UPDATE audit_log SET summary = 'changed' WHERE seq = 2")
    with pytest.raises(sqlite3.DatabaseError):
        rig.db.execute("DELETE FROM audit_log WHERE seq = 2")
    assert verify(rig.db).ok


@pytest.mark.parametrize("column", ["actor", "kind", "subject", "summary", "at", "payload_sha256"])
def test_g21_editing_any_field_of_any_row_is_detected_at_that_row(
    rig: WriteRig, column: str
) -> None:
    fill(rig, 6)
    lift_guards(rig)
    rig.db.execute(f"UPDATE audit_log SET {column} = 'tampered' WHERE seq = 3")
    report = verify(rig.db)
    assert not report.ok
    assert report.first_bad_seq == 3


def test_g21_editing_a_hash_breaks_the_next_link(rig: WriteRig) -> None:
    fill(rig, 6)
    lift_guards(rig)
    rig.db.execute("UPDATE audit_log SET hash = ? WHERE seq = 3", ("a" * 64,))
    assert verify(rig.db).first_bad_seq == 3


def test_g21_deleting_a_row_in_the_middle_is_detected(rig: WriteRig) -> None:
    fill(rig, 6)
    lift_guards(rig)
    rig.db.execute("DELETE FROM audit_log WHERE seq = 3")
    report = verify(rig.db)
    assert not report.ok
    assert report.first_bad_seq == 3


def test_g21_deleting_rows_from_the_end_is_detected(rig: WriteRig) -> None:
    fill(rig, 6)
    lift_guards(rig)
    rig.db.execute("DELETE FROM audit_log WHERE seq >= 5")
    report = verify(rig.db)
    assert not report.ok
    assert report.problem == "rows are missing at the end"


def test_g21_deleting_the_first_rows_is_detected(rig: WriteRig) -> None:
    fill(rig, 6)
    lift_guards(rig)
    rig.db.execute("DELETE FROM audit_log WHERE seq <= 2")
    assert not verify(rig.db).ok


def test_g22_pruning_whole_months_leaves_a_checkpoint_and_a_verifying_chain(
    rig: WriteRig,
) -> None:
    fill(rig, 4)
    rig.clock.advance(hours=24 * 40)
    fill(rig, 4)
    rig.clock.advance(hours=24 * (AUDIT_DAYS + 5))
    fill(rig, 3)
    before = repo.audit_count(rig.db)
    removed = prune(rig.db, rig.clock, audit_of(rig))
    assert removed == 4
    assert repo.audit_count(rig.db) == before - removed + 1
    assert verify(rig.db).ok
    checkpoint = get_state(rig.db, CHECKPOINT_KEY)
    assert checkpoint is not None
    assert checkpoint.split(":")[0].isdigit()
    first = repo.audit_rows(rig.db)[0]
    assert first.seq == int(checkpoint.split(":")[0]) + 1
    assert first.prev_hash == checkpoint.split(":")[1]
    assert repo.audit_rows(rig.db)[-1].kind == "sweep"
    cutoff = (rig.clock.now() - timedelta(days=AUDIT_DAYS)).replace(day=1)
    assert all(
        row.at >= cutoff.strftime("%Y-%m-%dT00:00:00Z") for row in repo.audit_rows(rig.db)[:-1]
    )


def test_g22_pruning_is_idempotent_and_appending_continues_the_chain(rig: WriteRig) -> None:
    fill(rig, 3)
    rig.clock.advance(hours=24 * (AUDIT_DAYS + 70))
    fill(rig, 3)
    assert prune(rig.db, rig.clock, audit_of(rig)) == 3
    assert prune(rig.db, rig.clock, audit_of(rig)) == 0
    fill(rig, 2)
    assert verify(rig.db).ok


def test_g22_a_forged_checkpoint_is_not_enough_to_hide_an_edit(rig: WriteRig) -> None:
    fill(rig, 6)
    lift_guards(rig)
    rig.db.execute(
        "INSERT OR REPLACE INTO system_state (key, value, updated_at) VALUES (?, ?, 'x')",
        (CHECKPOINT_KEY, "2:" + "c" * 64),
    )
    assert get_state(rig.db, HEAD_KEY) is not None
    assert not verify(rig.db).ok


def test_g21_the_audit_stores_hashes_and_handles_never_bodies_or_addresses(rig: WriteRig) -> None:
    op = rig.ready_send(
        to=("secret.person@example.org",), subject="Private subject", body="TOP-SECRET-BODY"
    )
    rig.engine().do(op.handle)
    dump = " ".join(
        " ".join(str(value) for value in tuple(row))
        for row in rig.db.execute("SELECT * FROM audit_log")
    )
    for forbidden in ("secret.person", "Private subject", "TOP-SECRET-BODY"):
        assert forbidden not in dump
    assert op.handle in dump
    assert verify(rig.db).ok


def test_g23_a_transaction_that_rolls_back_leaves_no_audit_row(rig: WriteRig) -> None:
    fill(rig, 2)

    def fail_after_appending() -> None:
        with transaction(rig.db):
            audit_of(rig).append(actor="agent:x", kind="grant.activated", subject=None, summary="x")
            raise RuntimeError

    with pytest.raises(RuntimeError):
        fail_after_appending()
    assert repo.audit_count(rig.db) == 2
    assert verify(rig.db).ok


def test_g23_every_grant_and_operation_transition_writes_one_row(rig: WriteRig) -> None:
    op = rig.ready_send()
    rig.engine().do(op.handle)
    kinds = [
        row["kind"]
        for row in rig.db.execute(
            "SELECT kind FROM audit_log WHERE kind LIKE 'grant.%' OR kind LIKE 'operation.%'"
        )
    ]
    for expected in (
        "operation.proposed",
        "grant.proposed",
        "grant.activated",
        "operation.authorised",
        "operation.prepared",
        "operation.dispatched",
        "operation.confirmed",
        "grant.spent",
    ):
        assert expected in kinds, expected
    assert verify(rig.db).ok


def test_g23_every_tool_call_writes_one_tool_call_row(rig: WriteRig) -> None:
    from chamberlain.app.core import App
    from chamberlain.writes.handlers import HANDLERS
    from support.write_rig import SESSION

    app = App(rig.ctx, HANDLERS)
    before = repo.audit_count(rig.db)
    reply = app.call("do", {"session": SESSION, "operation": "op_0123456789abcdef"})
    assert not reply.ok
    rows = [row for row in repo.audit_rows(rig.db)[before:] if row.kind == "tool.call"]
    assert len(rows) == 1
    assert rows[0].summary.startswith("do: request.unknown_handle (")
    assert rows[0].actor == f"agent:{SESSION}"


def test_g21_doctor_reports_a_broken_chain_and_the_first_bad_row(rig: WriteRig) -> None:
    from chamberlain.writes.handlers import audit_checks

    fill(rig, 5)
    assert audit_checks(rig.ctx)[0].status == "ok"
    lift_guards(rig)
    rig.db.execute("UPDATE audit_log SET summary = 'x' WHERE seq = 4")
    checks = audit_checks(rig.ctx)
    assert checks[0].status == "fail"
    assert "seq 4" in checks[0].summary
    assert "not a sandbox" in checks[1].summary
