"""Uncertain writes never need the owner (u-settle; 04-writes W-13, W-37, 07-attention A-4).

The harness settles by reading for 24 hours. After that anything still unsettled reaches the agent
once as an item it can close, and the agent may do it again from the original. Writes imported
from the old harness are closed at import and never become work. Fake world only; no wording.
"""

from collections.abc import Iterator
from datetime import timedelta

import pytest
from tests.store.legacy import legacy_migrations

from chamberlain.connectors.fake import Fault
from chamberlain.domain.grant_rules import format_ts
from chamberlain.domain.operations import OperationState
from chamberlain.limits import UNCERTAIN_SEND_RECHECK_HOURS
from chamberlain.ports.clock import ManualClock
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.store.db import MEMORY, connect
from chamberlain.store.migrate import migrate
from chamberlain.writes import repo
from chamberlain.writes.maintenance import maintain
from chamberlain.writes.models import StoredReceipt
from chamberlain.writes.resend import propose_redo
from support.write_rig import CALENDAR, MAIL, OWNER, WriteRig, make_rig

S = OperationState


@pytest.fixture
def rig() -> Iterator[WriteRig]:
    made = make_rig()
    made.own(MAIL, OWNER)
    yield made
    made.close()


def asked_about(rig: WriteRig) -> list[str]:
    """What attention would hand the agent as `operation.uncertain` right now."""
    before = format_ts(rig.clock.now() - timedelta(hours=UNCERTAIN_SEND_RECHECK_HOURS))
    return [o.handle for o in repo.uncertain_operations(rig.db, before)]


def test_a_lost_calendar_write_goes_to_the_agent_after_a_day_and_may_be_done_again_once(
    rig: WriteRig,
) -> None:
    proposal = rig.engine().propose_intent(rig.event("Lunch"))
    op = rig.agent_approves(proposal).operation
    assert op is not None
    rig.fake.arm(Fault.LOSE_BEFORE_APPLY)
    with pytest.raises(ChamberlainError) as lost:
        rig.engine().do(op.handle)
    assert lost.value.code is ErrorCode.OPERATION_UNCERTAIN

    # The first day is the harness's business: nothing for the agent, no early redo.
    rig.clock.advance(hours=UNCERTAIN_SEND_RECHECK_HOURS - 1)
    maintain(rig.ctx, rig.policy)
    assert asked_about(rig) == []
    with pytest.raises(ChamberlainError):
        propose_redo(rig.engine(), op.handle)

    # After a day the harness looked once, found nothing, and the agent is told.
    rig.clock.advance(hours=2)
    maintain(rig.ctx, rig.policy)
    assert asked_about(rig) == [op.handle]
    looked = repo.get_operation(rig.db, op.handle)
    assert looked is not None
    assert looked.recheck_result == "missing"

    # The agent redoes it from the original: one new dispatch, the original is superseded.
    again = propose_redo(rig.engine(), op.handle)
    assert again.operation is not None
    assert again.ready
    done = rig.engine().do(again.operation.handle)
    assert done.state is S.CONFIRMED
    assert done.redo_of == op.handle
    assert rig.state(op.handle) == "superseded"
    assert rig.fake.calendar.count(CALENDAR, done.reconcile_token or "") == 1
    assert asked_about(rig) == []
    assert propose_redo(rig.engine(), op.handle).operation.handle == done.handle  # type: ignore[union-attr]


def test_a_send_the_harness_could_not_read_still_reaches_the_agent_and_is_never_resent(
    rig: WriteRig,
) -> None:
    op = rig.ready_send()
    rig.fake.arm(Fault.LOSE_BEFORE_APPLY)
    with pytest.raises(ChamberlainError):
        rig.engine().do(op.handle)
    rig.clock.advance(hours=UNCERTAIN_SEND_RECHECK_HOURS + 1)  # the harness's look never ran
    assert asked_about(rig) == [op.handle]
    with pytest.raises(ChamberlainError) as early:
        propose_redo(rig.engine(), op.handle)  # not looked at yet, so not resendable
    assert early.value.code is ErrorCode.REQUEST_INVALID
    assert rig.sent_copies() == 0
    assert rig.state(op.handle) == "uncertain"


def test_migration_0220_closes_imported_uncertain_operations_and_leaves_the_rest(
    clock: ManualClock,
) -> None:
    db = connect(MEMORY)
    upto = [m for m in legacy_migrations() if m.version <= 201]
    assert migrate(db, clock, backup_dir=None, migrations=upto)[-1] == 201
    when = "2026-01-01T00:00:00Z"
    sha = "a" * 64
    rows = [
        ("op_0000000000000001", "import:thunderbird:send:x", "uncertain", None),
        ("op_0000000000000002", "import:thunderbird:send:y", "confirmed", None),
        ("op_0000000000000003", "native-key", "uncertain", "{}"),
    ]
    for handle, key, state, intent in rows:
        db.execute(
            "INSERT INTO operations (handle, idempotency_key, kind, source_id, state, "
            "intent_sha256, intent_json, created_at, updated_at) "
            "VALUES (?, ?, 'mail.send', 'mail:t', ?, ?, ?, ?, ?)",
            (handle, key, state, sha, intent, when, when),
        )
    db.commit()
    assert migrate(db, clock, backup_dir=None, migrations=legacy_migrations())[0] == 220
    states = {r["handle"]: r["state"] for r in db.execute("SELECT handle, state FROM operations")}
    assert states == {
        "op_0000000000000001": "abandoned",
        "op_0000000000000002": "confirmed",
        "op_0000000000000003": "uncertain",  # one of this harness's own: still its business
    }
    receipt = db.execute(
        "SELECT receipt_json FROM operations WHERE handle = 'op_0000000000000001'"
    ).fetchone()[0]
    assert StoredReceipt.model_validate_json(receipt).summary.state is S.ABANDONED
    later = "2099-01-01T00:00:00Z"
    assert "op_0000000000000001" not in [o.handle for o in repo.uncertain_operations(db, later)]
    db.close()
