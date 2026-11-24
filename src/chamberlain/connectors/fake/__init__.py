"""The fake provider: in-memory mail, calendar and contacts with fault switches.

The reference implementation of the ports and the thing every track tests
against. Contract: docs/contracts/08-ports.md (The fake provider).

    fake = FakeProvider()
    fake.mail.add_source("mail:work")
    fake.arm(Fault.LOSE_RESPONSE_AFTER_APPLY)
"""

from chamberlain.connectors.fake.calendar import FakeCalendar
from chamberlain.connectors.fake.contacts import FakeContacts
from chamberlain.connectors.fake.mail import FakeMail
from chamberlain.connectors.fake.world import DispatchRecord, FakeWorld, Fault
from chamberlain.ports.clock import ManualClock

__all__ = [
    "DispatchRecord",
    "FakeCalendar",
    "FakeContacts",
    "FakeMail",
    "FakeProvider",
    "FakeWorld",
    "Fault",
]


class FakeProvider:
    """One fake world with its three ports."""

    def __init__(
        self,
        clock: ManualClock | None = None,
        *,
        deadline_seconds: int = 30,
        send_absence_provable: bool = False,
    ) -> None:
        self.clock = clock or ManualClock()
        self.world = FakeWorld(self.clock, deadline_seconds=deadline_seconds)
        self.mail = FakeMail(self.world, send_absence_provable=send_absence_provable)
        self.calendar = FakeCalendar(self.world)
        self.contacts = FakeContacts(self.world)

    def arm(self, fault: Fault, times: int = 1) -> None:
        self.world.arm(fault, times)

    def disarm(self, fault: Fault | None = None) -> None:
        self.world.disarm(fault)

    def restart(self) -> None:
        self.world.restart()

    @property
    def dispatch_log(self) -> list[DispatchRecord]:
        return self.world.dispatch_log

    def dispatch_count(self, key: str) -> int:
        """How many times `dispatch` was called with this key (must be at most 1)."""
        return len([record for record in self.world.dispatch_log if record.key == key])
