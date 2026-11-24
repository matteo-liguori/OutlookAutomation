"""First end-to-end pass: propose, approve, do (W-7, W-10, W-16)."""

from chamberlain.domain.operations import OperationState
from chamberlain.testing.write_rig import WriteRig


def test_a_known_recipient_send_is_proposed_approved_and_sent_once(rig: WriteRig) -> None:
    op = rig.ready_send()
    done = rig.engine().do(op.handle)
    assert done.state is OperationState.CONFIRMED
    assert rig.sent_copies() == 1
    again = rig.engine().do(op.handle)
    assert again.state is OperationState.CONFIRMED
    assert rig.sent_copies() == 1
