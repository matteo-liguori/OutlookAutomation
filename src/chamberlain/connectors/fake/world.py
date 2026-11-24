"""The fake provider's in-memory state and its fault switches.

Contract: docs/contracts/08-ports.md (The fake provider).
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from chamberlain.domain.calendar import EventFields, EventStatus
from chamberlain.domain.capabilities import HealthState, SourceHealth
from chamberlain.domain.intents import IntentKind
from chamberlain.domain.mail import Address, AttachmentMeta, Folder, FolderRole
from chamberlain.ports.clock import ManualClock
from chamberlain.schema.errors import ChamberlainError, ErrorCode


class Fault(StrEnum):
    LOSE_RESPONSE_AFTER_APPLY = "lose_response_after_apply"
    """The mutation applies, then `dispatch` returns `unknown`."""
    LOSE_BEFORE_APPLY = "lose_before_apply"
    """Nothing applies; `dispatch` returns `unknown`."""
    BUSY_BEFORE_APPLY = "busy_before_apply"
    """Nothing applies; `dispatch` returns `not_applied`."""
    RESTART = "restart"
    """Session IDs are renumbered before the call; stable locators still resolve."""
    DUPLICATE_COPY = "duplicate_copy"
    """The next move leaves the source copy behind as well."""
    SLOW = "slow"
    """The call outlasts its deadline. A dispatch applies late, after the answer."""
    STALE = "stale"
    """Reads report incomplete coverage; health is stale."""


@dataclass
class StoredMail:
    uid: int
    session_id: int
    folder_id: str
    internet_message_id: str
    subject: str
    sender: Address
    to: tuple[Address, ...]
    cc: tuple[Address, ...]
    date: datetime
    unread: bool
    body: str
    attachments: list[tuple[AttachmentMeta, bytes]] = field(default_factory=lambda: [])
    raw_sha256: str | None = None
    authentication_results: tuple[str, ...] = ()


@dataclass
class StoredEvent:
    id: str
    uid: str
    sequence: int
    status: EventStatus
    fields: EventFields
    updated: datetime

    @property
    def etag(self) -> str:
        return f'"{self.id}-{self.sequence}"'


@dataclass
class StoredContact:
    id: str
    name: str
    emails: tuple[str, ...]


@dataclass(frozen=True)
class DispatchRecord:
    """One call to `dispatch`. Tests assert no key appears twice (W-8)."""

    key: str
    intent_kind: IntentKind
    applied: bool


class FakeWorld:
    """Shared state behind FakeMail, FakeCalendar and FakeContacts."""

    def __init__(self, clock: ManualClock, *, deadline_seconds: int = 30) -> None:
        self.clock = clock
        self.deadline_seconds = deadline_seconds
        self.generation = 1
        self.mail: dict[str, list[StoredMail]] = {}
        self.folders: dict[str, list[Folder]] = {}
        self.events: dict[str, dict[str, StoredEvent]] = {}
        self.contacts: dict[str, list[StoredContact]] = {}
        self.health_override: dict[str, HealthState] = {}
        self.dispatch_log: list[DispatchRecord] = []
        self._armed: dict[Fault, int] = {}
        self._late: list[tuple[datetime, Callable[[], None]]] = []
        self._uid = 0
        self._session = 100
        self._event = 0

    # --- fault switches -------------------------------------------------------

    def arm(self, fault: Fault, times: int = 1) -> None:
        """Make the next `times` relevant calls misbehave."""
        self._armed[fault] = self._armed.get(fault, 0) + times

    def disarm(self, fault: Fault | None = None) -> None:
        if fault is None:
            self._armed.clear()
        else:
            self._armed.pop(fault, None)

    def armed(self, fault: Fault) -> bool:
        return self._armed.get(fault, 0) > 0

    def take(self, fault: Fault) -> bool:
        """Consume one arming of `fault`, if any."""
        if not self.armed(fault):
            return False
        self._armed[fault] -= 1
        return True

    # --- call bookkeeping -----------------------------------------------------

    def begin_call(self) -> None:
        """Run at the start of every port call."""
        if self.take(Fault.RESTART):
            self.restart()
        self.settle_late_writes()

    def begin_read(self, source: str) -> None:
        self.begin_call()
        if self.take(Fault.SLOW):
            self.clock.advance(seconds=self.deadline_seconds + 1)
            raise ChamberlainError(
                ErrorCode.SOURCE_UNAVAILABLE,
                "The source did not answer in time.",
                detail=f"fake: slow read on {source}",
            )

    def restart(self) -> None:
        """Renumber every session ID, as a mail client does on restart (P-6)."""
        self.generation += 1
        for messages in self.mail.values():
            for message in messages:
                message.session_id = self.next_session_id()

    def apply_late(self, apply: Callable[[], None]) -> None:
        """Schedule a mutation that lands after its caller has given up."""
        due = self.clock.now() + timedelta(seconds=self.deadline_seconds + 60)
        self._late.append((due, apply))

    def settle_late_writes(self) -> None:
        now = self.clock.now()
        due = [item for item in self._late if item[0] <= now]
        self._late = [item for item in self._late if item[0] > now]
        for _, apply in due:
            apply()

    # --- counters and health --------------------------------------------------

    def next_uid(self) -> int:
        self._uid += 1
        return self._uid

    def next_session_id(self) -> int:
        self._session += 1
        return self._session

    def next_event_id(self) -> str:
        self._event += 1
        return f"evt{self._event:05d}"

    def health(self, source: str) -> SourceHealth:
        now = self.clock.now()
        state = self.health_override.get(source)
        if state is None:
            state = HealthState.STALE if self.armed(Fault.STALE) else HealthState.OK
        fresh = now if state is HealthState.OK else None
        return SourceHealth.model_validate(
            {"source": source, "state": state, "checked_at": now, "fresh_as_of": fresh}
        )

    def require_source(self, known: Mapping[str, object], source: str) -> None:
        if source not in known:
            raise ChamberlainError(ErrorCode.SOURCE_NOT_CONFIGURED, "That source is not set up.")

    def folder(self, source: str, folder_id: str) -> Folder:
        for folder in self.folders[source]:
            if folder.id == folder_id:
                return folder
        raise ChamberlainError(ErrorCode.REQUEST_UNKNOWN_HANDLE, "There is no such folder.")

    def folder_by_role(self, source: str, role: FolderRole) -> Folder:
        for folder in self.folders[source]:
            if folder.role is role:
                return folder
        raise ChamberlainError(ErrorCode.SOURCE_NOT_CONFIGURED, f"No {role.value} folder is bound.")
