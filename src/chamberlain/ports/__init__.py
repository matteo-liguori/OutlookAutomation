"""Interfaces between the core and the outside world.

Contract: docs/contracts/08-ports.md. Shared package (docs/OWNERSHIP.md).
"""

from chamberlain.ports.calendar import CalendarPort
from chamberlain.ports.clock import Clock, ManualClock, SystemClock
from chamberlain.ports.contacts import ContactsPort
from chamberlain.ports.mail import MailPort
from chamberlain.ports.page import Gap, Page

__all__ = [
    "CalendarPort",
    "Clock",
    "ContactsPort",
    "Gap",
    "MailPort",
    "ManualClock",
    "Page",
    "SystemClock",
]
