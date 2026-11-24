"""Calendar rigs for the real connectors (contract: docs/contracts/08-ports.md)."""

from datetime import UTC, datetime

from cal_fake_google import FakeGoogleApi

from chamberlain.connectors.fake.world import Fault
from chamberlain.domain.calendar import EventFields
from chamberlain.ports.calendar import CalendarPort
from chamberlain.ports.clock import ManualClock

_SERIAL = iter(range(1, 1_000_000))


def new_uid(prefix: str) -> str:
    return f"{prefix}-{next(_SERIAL)}@chamberlain.test"


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


class GoogleRig:
    source = "calendar:test"

    def __init__(self, port: CalendarPort, api: FakeGoogleApi, clock: ManualClock) -> None:
        self.port = port
        self.api = api
        self.clock = clock

    def inject(self, fault: Fault) -> bool:
        if fault is Fault.STALE:
            return False  # the API is the server: there is no stale local copy to report
        self.api.arm(fault)
        return True

    def restart(self) -> bool:
        return False

    def advance(self, seconds: float) -> None:
        self.api.advance(seconds)

    def new_uid(self) -> str:
        return new_uid("google")

    def seed(self, fields: EventFields) -> str:
        return self.api.seed(
            {
                "summary": fields.title,
                "start": {"dateTime": iso(fields.start)},
                "end": {"dateTime": iso(fields.end)},
            }
        )

    def count(self, uid: str) -> int:
        return len(self.api.live(uid))
