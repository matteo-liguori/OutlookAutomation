"""Calendar rigs for the real connectors (contract: docs/contracts/08-ports.md)."""

from collections.abc import Mapping
from datetime import UTC, datetime

from cal_fake_google import FakeGoogleApi

from chamberlain.connectors.caldav import dav
from chamberlain.connectors.calendar_support import ics
from chamberlain.connectors.calendar_support.http import HttpReply, HttpTransport, TransportFailure
from chamberlain.connectors.fake.world import Fault
from chamberlain.domain.calendar import EventFields, EventStatus
from chamberlain.ports.calendar import CalendarPort
from chamberlain.ports.clock import ManualClock

_SERIAL = iter(range(1, 1_000_000))
CALDAV_ADDRESS = "alice@example.test"
"""The calendar user address the CalDAV test account sends invitations from."""


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

    def seed_invitation(self, organiser: str, fields: EventFields) -> str | None:
        return self.api.seed(
            {
                "summary": fields.title,
                "start": {"dateTime": iso(fields.start)},
                "end": {"dateTime": iso(fields.end)},
                "organizer": {"email": organiser},
                "attendees": [
                    {"email": organiser, "organizer": True, "responseStatus": "accepted"},
                    {"email": self.api.owner, "self": True, "responseStatus": "needsAction"},
                ],
            }
        )

    def count(self, uid: str) -> int:
        return len(self.api.live(uid))


class FaultyTransport:
    """Wraps the real transport and injects write faults for the next mutating request."""

    def __init__(self, inner: HttpTransport) -> None:
        self.inner = inner
        self.armed: list[Fault] = []
        self.late: list[tuple[str, str, Mapping[str, str], bytes | None]] = []
        self.sent: list[tuple[str, str]] = []

    def send(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HttpReply:
        if method not in ("PUT", "DELETE") or not self.armed:
            self.sent.append((method, url))
            return self.inner.send(method, url, headers=headers, body=body, timeout=timeout)
        fault = self.armed.pop(0)
        if fault is Fault.BUSY_BEFORE_APPLY:
            raise TransportFailure("connection refused", sent=False)
        if fault is Fault.LOSE_BEFORE_APPLY:
            raise TransportFailure("connection reset", sent=True)
        if fault is Fault.SLOW:
            self.late.append((method, url, headers, body))
            raise TransportFailure("timed out", sent=True)
        self.sent.append((method, url))
        reply = self.inner.send(method, url, headers=headers, body=body, timeout=timeout)
        if fault is Fault.LOSE_RESPONSE_AFTER_APPLY:
            raise TransportFailure("connection reset", sent=True)
        return reply

    def settle_late(self) -> None:
        late, self.late = self.late, []
        for method, url, headers, body in late:
            self.inner.send(method, url, headers=headers, body=body, timeout=30)


class CalDavRig:
    source = "calendar:test"

    def __init__(
        self,
        port: CalendarPort,
        transport: FaultyTransport,
        clock: ManualClock,
        collection: str,
        auth: str,
    ) -> None:
        self.port = port
        self.transport = transport
        self.clock = clock
        self.collection = collection
        self._auth = {"Authorization": auth}
        self._seeded = 0

    def inject(self, fault: Fault) -> bool:
        if fault in (Fault.STALE, Fault.RESTART):
            return False
        self.transport.armed.append(fault)
        return True

    def restart(self) -> bool:
        return False

    def advance(self, seconds: float) -> None:
        self.clock.advance(seconds=seconds)
        self.transport.settle_late()

    def new_uid(self) -> str:
        return new_uid("caldav")

    def seed(self, fields: EventFields) -> str:
        self._seeded += 1
        uid = new_uid("seed")
        name = f"seed-{self._seeded}.ics"
        text = ics.build_new(uid, fields, self.clock.now())
        reply = self.transport.inner.send(
            "PUT",
            self.collection + name,
            headers={**self._auth, "Content-Type": "text/calendar", "If-None-Match": "*"},
            body=text.encode(),
            timeout=10,
        )
        assert reply.status in (201, 204)
        return name

    def seed_invitation(self, organiser: str, fields: EventFields) -> str | None:
        self._seeded += 1
        uid = new_uid("invite")
        name = f"invite-{self._seeded}.ics"
        text = ics.build_new(uid, fields, self.clock.now())
        text = text.replace(
            "END:VEVENT",
            f"ORGANIZER:mailto:{organiser}\r\n"
            f"ATTENDEE;PARTSTAT=ACCEPTED:mailto:{organiser}\r\n"
            f"ATTENDEE;PARTSTAT=NEEDS-ACTION;RSVP=TRUE:mailto:{CALDAV_ADDRESS}\r\n"
            "END:VEVENT",
        )
        reply = self.transport.inner.send(
            "PUT",
            self.collection + name,
            headers={**self._auth, "Content-Type": "text/calendar", "If-None-Match": "*"},
            body=text.encode(),
            timeout=10,
        )
        assert reply.status in (201, 204)
        return name

    def count(self, uid: str) -> int:
        reply = self.transport.inner.send(
            "REPORT",
            self.collection,
            headers={**self._auth, "Depth": "1", "Content-Type": "application/xml"},
            body=dav.query_uid(uid),
            timeout=10,
        )
        responses = dav.parse_multistatus(reply.body)
        return sum(
            1
            for r in responses
            if r.calendar_data
            and any(
                p.uid == uid and p.status is not EventStatus.CANCELLED
                for p in ics.parse_events(r.calendar_data)
            )
        )
