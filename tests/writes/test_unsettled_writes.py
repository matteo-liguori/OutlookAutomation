"""Uncertain writes never need the owner (u-settle; 04-writes W-13, W-37, 07-attention A-4).

The harness settles by reading for 24 hours. After that anything still unsettled reaches the agent
once as an item it can close, and the agent may do it again from the original. Writes imported
from the old harness are closed at import and never become work. Fake world only; no wording.
"""

from collections.abc import Iterator
from datetime import timedelta

import pytest

from chamberlain.connectors.fake import Fault
from chamberlain.domain.operations import OperationState
from chamberlain.domain.timefmt import ts
from chamberlain.limits import UNCERTAIN_SEND_RECHECK_HOURS
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.writes import repo
from chamberlain.writes.maintenance import maintain
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
    before = ts(rig.clock.now() - timedelta(hours=UNCERTAIN_SEND_RECHECK_HOURS))
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
