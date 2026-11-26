"""The shared port contract suites against the Thunderbird connector (rules P-1 to P-18)."""

from collections.abc import Mapping
from dataclasses import replace

import pytest
from thunderbird_rig import ACCOUNT, CalendarRigAdapter, ContactsRigAdapter, MailRigAdapter, Stack

from chamberlain.app.context import SourceRouter
from chamberlain.connectors.thunderbird.identity import encode_locator
from chamberlain.domain.handles import HandleKind
from chamberlain.domain.intents import MailTrash
from chamberlain.domain.mail import FolderRole
from chamberlain.domain.operations import DispatchOutcome, DispatchResult, OperationState, Prepared
from chamberlain.ports.calendar import CalendarPort
from chamberlain.ports.contacts import ContactsPort
from chamberlain.ports.mail import MailPort
from support.calendar_contract import CalendarPortContract
from support.contacts_contract import ContactsPortContract
from support.mail_contract import MailPortContract
from support.rigs import CalendarRig, ContactsRig, MailRig
from support.write_rig import MAIL, make_rig


class TestThunderbirdMail(MailPortContract):
    @pytest.fixture
    def rig(self, stack: Stack) -> MailRig:
        return MailRigAdapter(stack)


class TestOwlCalendar(CalendarPortContract):
    @pytest.fixture
    def rig(self, stack: Stack) -> CalendarRig:
        return CalendarRigAdapter(stack)


class TestThunderbirdContacts(ContactsPortContract):
    @pytest.fixture
    def rig(self, stack: Stack) -> ContactsRig:
        return ContactsRigAdapter(stack)


def test_p1_the_connectors_satisfy_the_port_protocols(stack: Stack) -> None:
    assert isinstance(stack.mail, MailPort)
    assert isinstance(stack.contacts, ContactsPort)
    assert isinstance(stack.calendar, CalendarPort)


def test_w29_batch_reconcile_repoints_each_moved_handle(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = make_rig()
    try:
        rig.ctx = replace(rig.ctx, sources=SourceRouter(mail={MAIL: stack.mail}))
        ids = [stack.new_message_id() for _ in range(2)]
        handles = tuple(
            rig.refs.remember(HandleKind.MESSAGE, MAIL, stack.seed(internet_message_id=mid))
            for mid in ids
        )
        missing = encode_locator(ACCOUNT, stack.folder_id(FolderRole.INBOX), stack.new_message_id())
        skipped = rig.refs.remember(HandleKind.MESSAGE, MAIL, missing)
        intent = MailTrash(source=MAIL, messages=(*handles, skipped))
        proposal = rig.agent_approves(rig.engine().propose_intent(intent), "Trash these.")
        assert proposal.operation is not None
        dispatch = stack.mail.dispatch
        calls = []

        def lose_reply(
            prepared: Prepared, key: str, payloads: Mapping[str, bytes]
        ) -> DispatchResult:
            calls.append(key)
            result = dispatch(prepared, key, payloads)
            assert result.outcome is DispatchOutcome.APPLIED, result.detail
            return DispatchResult(outcome=DispatchOutcome.UNKNOWN)

        monkeypatch.setattr(stack.mail, "dispatch", lose_reply)
        done = rig.engine().do(proposal.operation.handle)
        assert done.state is OperationState.CONFIRMED
        assert len(calls) == 1
        assert rig.refs.locate(skipped) == (MAIL, missing)
        for handle, mid in zip(handles, ids, strict=True):
            assert rig.refs.locate(handle) == (
                MAIL,
                encode_locator(ACCOUNT, stack.folder_id(FolderRole.TRASH), mid),
            )
    finally:
        rig.close()
