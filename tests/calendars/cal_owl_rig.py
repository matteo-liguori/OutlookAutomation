"""The rig for the Owl calendar connector over the fake bridge."""

from cal_bridge import FakeCalendarBridge
from cal_rigs import new_uid

from chamberlain.connectors.calendar_support import ics
from chamberlain.connectors.fake.world import Fault
from chamberlain.domain.calendar import EventFields, EventStatus
from chamberlain.ports.calendar import CalendarPort
from chamberlain.ports.clock import ManualClock


class OwlRig:
    source = "calendar:test"

    def __init__(self, port: CalendarPort, bridge: FakeCalendarBridge, clock: ManualClock) -> None:
        self.port = port
        self.bridge = bridge
        self.clock = clock

    def inject(self, fault: Fault) -> bool:
        if fault in (Fault.RESTART, Fault.LOSE_BEFORE_APPLY):
            # Absence is never provable through a client cache, so the proof test does not apply.
            return False
        self.bridge.arm(fault)
        return True

    def restart(self) -> bool:
        return False

    def advance(self, seconds: float) -> None:
        self.bridge.advance(seconds)

    def new_uid(self) -> str:
        return new_uid("owl")

    def seed(self, fields: EventFields) -> str:
        uid = new_uid("owl-seed")
        self.bridge.items[uid] = ics.build_new(uid, fields, self.clock.now())
        return uid

    def seed_invitation(self, organiser: str, fields: EventFields) -> str | None:
        return None  # Owl cannot send or answer invitations, so there is nothing to seed

    def count(self, uid: str) -> int:
        text = self.bridge.items.get(uid)
        if text is None:
            return 0
        return int(ics.master_of(text).status is not EventStatus.CANCELLED)
