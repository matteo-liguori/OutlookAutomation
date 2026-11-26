"""An uncertain send is never lost, and the agent decides whether to send it again (W-37 to W-41).

One scenario for each way the harness's single look after 24 hours can end, and the resend path.
They run on the fake world; nothing here pins message wording.
"""

from collections.abc import Iterator
from typing import Any

import pytest

from chamberlain.attention import repo as attention_repo
from chamberlain.attention.detectors.operations import send_missing
from chamberlain.attention.models import DetectorInput
from chamberlain.connectors.fake import Fault
from chamberlain.domain.events import EventKind
from chamberlain.domain.operations import OperationState
from chamberlain.limits import UNCERTAIN_ABANDON_DAYS, UNCERTAIN_SEND_RECHECK_HOURS
from chamberlain.ports.mail import MailPort
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.testing.write_rig import MAIL, OWNER, OWNER_B, WriteRig, make_rig
from chamberlain.writes import repo
from chamberlain.writes.maintenance import maintain

MAIL_B = "mail:two"
S = OperationState


@pytest.fixture
def rig() -> Iterator[WriteRig]:
    made = make_rig()
    made.own(MAIL, OWNER)
    yield made
    made.close()


def lost_send(rig: WriteRig, fault: Fault, **kwargs: Any) -> str:
    """A send whose outcome is unknown. With `LOSE_BEFORE_APPLY` nothing was sent."""
    op = rig.ready_send(**kwargs)
    rig.fake.arm(fault)
    with pytest.raises(ChamberlainError) as raised:
        rig.engine().do(op.handle)
    assert raised.value.code is ErrorCode.OPERATION_UNCERTAIN
    return op.handle


def a_day_later(rig: WriteRig) -> None:
    rig.clock.advance(hours=UNCERTAIN_SEND_RECHECK_HOURS, minutes=5)
    maintain(rig.ctx, rig.policy)


def get(rig: WriteRig, handle: str):  # type: ignore[no-untyped-def]
    row = repo.get_operation(rig.db, handle)
    assert row is not None
    return row


def reasons(rig: WriteRig) -> list[str]:
    """What the harness would raise for the agent: the `send.missing` events."""
    sends = tuple(attention_repo.rechecked_sends(rig.db))
    drafts = send_missing(DetectorInput(now=rig.clock.now(), sends=sends))
    return [d.attrs.reason or "" for d in drafts if d.kind is EventKind.SEND_MISSING]


def test_nothing_is_looked_at_or_resent_while_the_send_is_fresh(rig: WriteRig) -> None:
    handle = lost_send(rig, Fault.LOSE_BEFORE_APPLY)
    rig.clock.advance(hours=UNCERTAIN_SEND_RECHECK_HOURS - 1)
    maintain(rig.ctx, rig.policy)
    assert get(rig, handle).rechecked_at is None
    with pytest.raises(ChamberlainError) as early:
        rig.engine().propose_redo(handle)
    assert early.value.code is ErrorCode.REQUEST_INVALID
    assert get(rig, handle).state is S.UNCERTAIN
    assert rig.sent_copies() == 0


def test_found_in_sent_or_in_a_recipients_inbox_settles_it_as_sent(rig: WriteRig) -> None:
    in_sent = lost_send(rig, Fault.LOSE_BEFORE_APPLY, subject="One")
    token = get(rig, in_sent).reconcile_token or ""
    rig.fake.mail.add_message(
        MAIL, folder_id="f-sent", internet_message_id=token, subject="One", sender=OWNER
    )
    rig.fake.mail.add_source(MAIL_B)
    rig.own(MAIL_B, OWNER_B)
    cast_mail: MailPort = rig.fake.mail  # type: ignore[assignment]
    rig.ctx.sources.mail[MAIL_B] = cast_mail  # type: ignore[index]
    in_inbox = lost_send(rig, Fault.LOSE_BEFORE_APPLY, to=(OWNER_B,), subject="Two", body="Hi.")
    rig.fake.mail.add_message(
        MAIL_B, internet_message_id="<elsewhere@example.org>", subject="Two", sender=OWNER,
        to=(OWNER_B,), body="Hi.",
    )  # fmt: skip
    a_day_later(rig)
    assert get(rig, in_sent).state is S.CONFIRMED
    assert get(rig, in_inbox).state is S.CONFIRMED
    assert reasons(rig) == []


def test_still_in_the_outbox_is_reported_and_never_resent(rig: WriteRig) -> None:
    handle = lost_send(rig, Fault.QUEUE_INSTEAD_OF_SEND)
    a_day_later(rig)
    assert get(rig, handle).recheck_result == "queued"
    assert reasons(rig) == ["outbox"]
    with pytest.raises(ChamberlainError) as stuck:
        rig.engine().propose_redo(handle)
    assert stuck.value.code is ErrorCode.REQUEST_INVALID
    assert get(rig, handle).state is S.UNCERTAIN


def test_missing_everywhere_is_reported_once_and_the_agent_may_send_it_again(
    rig: WriteRig,
) -> None:
    handle = lost_send(rig, Fault.LOSE_BEFORE_APPLY)
    original = get(rig, handle)
    a_day_later(rig)
    a_day_later(rig)  # a second tick looks at nothing new
    checked = get(rig, handle)
    assert checked.recheck_result == "missing"
    assert checked.rechecked_at is not None
    assert reasons(rig) == ["missing"]

    eng = rig.engine()
    proposal = eng.propose_redo(handle)
    assert proposal.ready
    assert proposal.operation is not None
    assert get(rig, handle).state is S.UNCERTAIN  # nothing changes until `do`
    again = eng.propose_redo(handle)
    assert again.operation is not None
    assert again.operation.handle == proposal.operation.handle
    same_text = eng.propose_intent(rig.send())
    assert same_text.operation is not None
    assert same_text.operation.handle == proposal.operation.handle  # no accidental second one

    done = eng.do(proposal.operation.handle)
    assert done.state is S.CONFIRMED
    assert done.redo_of == handle
    assert done.grant == original.grant
    assert get(rig, handle).state is S.SUPERSEDED
    assert rig.sent_copies() == 1
    assert reasons(rig) == []
    last = eng.propose_redo(handle)  # asking again only shows the resend that exists
    assert last.operation is not None
    assert last.operation.handle == proposal.operation.handle
    assert not last.ready
    resends = rig.db.execute("SELECT COUNT(*) FROM operations WHERE redo_of = ?", (handle,))
    assert resends.fetchone()[0] == 1
    assert rig.sent_copies() == 1
    audited = {r["kind"] for r in rig.db.execute("SELECT kind FROM audit_log")}
    assert {"operation.rechecked", "operation.superseded"} <= audited

    # A resend is not itself resent: a second one needs the owner.
    lost_first = lost_send(rig, Fault.LOSE_BEFORE_APPLY, subject="Third")
    a_day_later(rig)
    resent = eng.propose_redo(lost_first).operation
    assert resent is not None
    rig.fake.arm(Fault.LOSE_BEFORE_APPLY)
    with pytest.raises(ChamberlainError):
        eng.do(resent.handle)
    a_day_later(rig)
    with pytest.raises(ChamberlainError) as twice:
        eng.propose_redo(resent.handle)
    assert twice.value.code is ErrorCode.REQUEST_INVALID


def test_w40_a_missing_send_remains_visible_after_its_permission_is_revoked(rig: WriteRig) -> None:
    handle = lost_send(rig, Fault.LOSE_BEFORE_APPLY)
    a_day_later(rig)
    grant = get(rig, handle).grant
    assert grant is not None
    rig.engine().grants.revoke(grant, actor="human:viewer")
    with pytest.raises(ChamberlainError) as refused:
        rig.engine().propose_redo(handle)
    assert refused.value.code is ErrorCode.GRANT_NOT_ACTIVE

    rig.clock.advance(hours=24 * (UNCERTAIN_ABANDON_DAYS + 1))
    maintain(rig.ctx, rig.policy)
    assert get(rig, handle).state is S.UNCERTAIN  # a send is never quietly closed
    assert reasons(rig) == ["missing"]

    settled, changed = rig.engine().reconcile(handle)
    assert settled.state is S.UNCERTAIN
    assert not changed
    assert reasons(rig) == ["missing"]
