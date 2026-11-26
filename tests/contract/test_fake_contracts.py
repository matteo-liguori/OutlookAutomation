"""The contract suites, run against the fake provider on every gate.

This is the pattern a real connector copies: subclass the suite, provide a
`rig`. See docs/contracts/08-ports.md (Contract suites).
"""

import pytest

from chamberlain.ports.calendar import CalendarPort
from chamberlain.ports.contacts import ContactsPort
from chamberlain.ports.mail import MailPort
from support.calendar_contract import CalendarPortContract
from support.contacts_contract import ContactsPortContract
from support.fake_rigs import FakeCalendarRig, FakeContactsRig, FakeMailRig
from support.mail_contract import MailPortContract
from support.rigs import CalendarRig, ContactsRig, MailRig


class TestFakeMail(MailPortContract):
    @pytest.fixture
    def rig(self) -> MailRig:
        return FakeMailRig()


class TestFakeCalendar(CalendarPortContract):
    @pytest.fixture
    def rig(self) -> CalendarRig:
        return FakeCalendarRig()


class TestFakeContacts(ContactsPortContract):
    @pytest.fixture
    def rig(self) -> ContactsRig:
        return FakeContactsRig()


def test_the_fakes_satisfy_the_port_protocols() -> None:
    assert isinstance(FakeMailRig().port, MailPort)
    assert isinstance(FakeCalendarRig().port, CalendarPort)
    assert isinstance(FakeContactsRig().port, ContactsPort)
