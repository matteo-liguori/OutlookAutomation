"""The shared port contract suites against the Thunderbird connector (rules P-1 to P-18)."""

import pytest
from thunderbird_rig import ContactsRigAdapter, MailRigAdapter, Stack

from chamberlain.ports.contacts import ContactsPort
from chamberlain.ports.mail import MailPort
from chamberlain.testing.contacts_contract import ContactsPortContract
from chamberlain.testing.mail_contract import MailPortContract
from chamberlain.testing.rigs import ContactsRig, MailRig


class TestThunderbirdMail(MailPortContract):
    @pytest.fixture
    def rig(self, stack: Stack) -> MailRig:
        return MailRigAdapter(stack)


class TestThunderbirdContacts(ContactsPortContract):
    @pytest.fixture
    def rig(self, stack: Stack) -> ContactsRig:
        return ContactsRigAdapter(stack)


def test_p1_the_connectors_satisfy_the_port_protocols(stack: Stack) -> None:
    assert isinstance(stack.mail, MailPort)
    assert isinstance(stack.contacts, ContactsPort)
