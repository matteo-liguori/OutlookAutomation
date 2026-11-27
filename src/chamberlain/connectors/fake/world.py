"""The fake provider's in-memory state and its fault switches.

Contract: docs/contracts/08-ports.md (The fake provider).
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, JsonValue

from chamberlain.connectors.calendar_support.paging import offset_cursor, slice_page
from chamberlain.domain.calendar import EventFields, EventStatus, Response
from chamberlain.domain.capabilities import HealthState, SourceHealth
from chamberlain.domain.intents import IntentKind
from chamberlain.domain.mail import Address, AttachmentMeta, Folder, FolderRole
from chamberlain.domain.operations import (
    DispatchOutcome,
    DispatchResult,
    Prepared,
    ProviderReceipt,
    ReconcileResult,
    ReconcileStatus,
)
from chamberlain.ports.clock import ManualClock
from chamberlain.ports.page import Gap, Page
from chamberlain.schema.errors import ChamberlainError, ErrorCode


def text(data: Mapping[str, JsonValue], key: str) -> str:
    value = data[key]
    if not isinstance(value, str):
        raise TypeError(f"prepared field {key} is not text")
    return value


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
    QUEUE_INSTEAD_OF_SEND = "queue_instead_of_send"
    """A send is held in the client's Outbox: `dispatch` returns `unknown`, nothing is in Sent,
    and `reconcile` says `queued` until the test empties the Outbox."""


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
    in_reply_to: str = ""
    references: tuple[str, ...] = ()
    unsynced: bool = False
    """Arrived at the server but not in the client's local index yet: searches do not see it
    until its folder is synced (`FakeMail.sync_folders`)."""


@dataclass
class StoredEvent:
    id: str
    uid: str
    sequence: int
    status: EventStatus
    fields: EventFields
    updated: datetime
    organiser: str | None = None
    """Who invited this calendar's owner, on an invitee's copy; None on one's own events."""
    my_response: Response | None = None
    """The owner's answer on an invitee's copy."""
    participants: dict[str, Response] = field(default_factory=lambda: {})
    """Each invitee's answer, as the organiser's copy (and the invitee's copy) holds them."""
    invite_of: tuple[str, str] | None = None
    """On an invitee's copy: (organiser's source, event id on that calendar)."""
    overrides: dict[str, EventFields | None] = field(default_factory=lambda: {})
    """A series only: occurrence id to the fields that occurrence was changed to, or None when
    that occurrence was cancelled."""

    @property
    def etag(self) -> str:
        return f'"{self.id}-{self.sequence}"'


@dataclass
class StoredContact:
    id: str
    name: str
    emails: tuple[str, ...]


@dataclass(frozen=True)
class Submission:
    """One message handed to the mail server (the SMTP submission of a send)."""

    source: str
    token: str
    recipients: tuple[str, ...]


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
        self.mail: dict[str, list[StoredMail]] = {}
        self.folders: dict[str, list[Folder]] = {}
        self.events: dict[str, dict[str, StoredEvent]] = {}
        self.addresses: dict[str, str] = {}
        """Calendar source to its owner's address (invitations are delivered by address)."""
        self.invitations: dict[str, bool] = {}
        """Calendar source to whether it declares `calendar.invite`."""
        self.recurrence: dict[str, bool] = {}
        """Calendar source to whether it writes repeating events (an Owl calendar does not)."""
        self.undelivered: dict[str, list[tuple[str, str]]] = {}
        """`(uid, address)` of invitees outside every fake calendar, per organiser's source."""
        self.responses_sent: dict[str, list[tuple[str, str, Response]]] = {}
        """`(uid, organiser, response)` of answers to an organiser outside every fake calendar."""
        self.contacts: dict[str, list[StoredContact]] = {}
        self.health_override: dict[str, HealthState] = {}
        self.stuck_folders: dict[str, set[str]] = {}
        """Per mail source, folders whose sync never finishes (a stuck client)."""
        self.sync_log: list[tuple[str, tuple[str, ...]]] = []
        """Every `sync_folders` call: the source and the folders asked, in order."""
        self.delivers_to_self: set[str] = set()
        """Mail sources where a send addressed to the mailbox's own address also lands in the
        Inbox (a working round trip). Off by default: a send leaves only a Sent copy."""
        self.dispatch_log: list[DispatchRecord] = []
        self.mail_addresses: dict[str, str] = {}
        """Mail source to its own address; a send to it from another fake source arrives in
        its Inbox. Unset: `owner@example.org`, with no delivery between sources."""
        self.server_keeps_sent: set[str] = set()
        """Mail sources whose server saves its own Sent copy of each submission (Exchange)."""
        self.sent_copy: dict[str, str] = {}
        """Mail source to `client` (the client files the Sent copy, the default) or `server`."""
        self.submissions: list[Submission] = []
        """Every message handed to a server, in order: one per send that went out."""
        self.open_composers = 0
        """Composer windows the harness opened and left open (only a display-only draft does)."""
        self.owner_composers: list[str] = []
        """Composers the owner has open themselves (test-seeded). The harness never touches them."""
        self.outbox: dict[str, set[str]] = {}
        """Per mail source, the Message-ID tokens of sends held in the client's Outbox."""
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
        for messages in self.mail.values():
            for message in messages:
                message.session_id = self.next_session_id()

    def apply_late(self, apply: Callable[[], ProviderReceipt]) -> None:
        """Schedule a mutation that lands after its caller has given up (a failed precondition
        means it simply does not happen)."""

        def run() -> None:
            try:
                apply()
            except ChamberlainError:
                return

        self._late.append((self.clock.now() + timedelta(seconds=self.deadline_seconds + 60), run))

    def settle_late_writes(self) -> None:
        now = self.clock.now()
        due = [item for item in self._late if item[0] <= now]
        self._late = [item for item in self._late if item[0] > now]
        for _, apply in due:
            apply()

    def run_dispatch(
        self,
        prepared: Prepared,
        key: str,
        apply: Callable[[], ProviderReceipt],
        finish: Callable[[ProviderReceipt], DispatchResult] | None = None,
    ) -> DispatchResult:
        """The fault ladder of the one mutation (P-10). Never retries and never raises."""
        kind = prepared.intent_kind
        self.begin_call()

        def lost(outcome: DispatchOutcome, detail: str) -> DispatchResult:
            self.dispatch_log.append(DispatchRecord(key, kind, applied=False))
            return DispatchResult(outcome=outcome, detail=detail)

        if self.take(Fault.BUSY_BEFORE_APPLY):
            return lost(DispatchOutcome.NOT_APPLIED, "fake: busy")
        if self.take(Fault.LOSE_BEFORE_APPLY):
            return lost(DispatchOutcome.UNKNOWN, "fake: lost before apply")
        if kind is IntentKind.MAIL_SEND and self.take(Fault.QUEUE_INSTEAD_OF_SEND):
            self.outbox.setdefault(prepared.source, set()).add(prepared.reconcile_token or "")
            return lost(DispatchOutcome.UNKNOWN, "fake: held in Outbox")
        if self.take(Fault.SLOW):
            self.apply_late(apply)
            self.clock.advance(seconds=self.deadline_seconds + 1)
            return lost(DispatchOutcome.UNKNOWN, "fake: deadline passed")
        try:
            receipt = apply()
        except ChamberlainError as error:
            return lost(DispatchOutcome.NOT_APPLIED, f"fake: precondition {error.code}")
        self.dispatch_log.append(DispatchRecord(key, kind, applied=True))
        if self.take(Fault.LOSE_RESPONSE_AFTER_APPLY):
            return DispatchResult(outcome=DispatchOutcome.UNKNOWN, detail="fake: response lost")
        if finish is not None:
            return finish(receipt)
        return DispatchResult(outcome=DispatchOutcome.APPLIED, receipt=receipt)

    def reconcile_gate(self, stale: str) -> ReconcileResult | None:
        """Run at the start of `reconcile`: an answer when a fault stops the read-back."""
        self.begin_call()
        if self.take(Fault.SLOW):
            self.clock.advance(seconds=self.deadline_seconds + 1)
            return ReconcileResult(status=ReconcileStatus.UNKNOWN, detail="fake: deadline passed")
        if self.take(Fault.STALE):
            return ReconcileResult(status=ReconcileStatus.UNKNOWN, detail=f"fake: stale {stale}")
        return None

    def page[T: BaseModel](
        self,
        page: type[Page[T]],
        source: str,
        found: Sequence[T],
        cursor: str | None,
        limit: int,
        stale: str,
    ) -> Page[T]:
        """One page of `found`; the cursor is an offset, and a stale source leaves a gap."""
        offset = offset_cursor(cursor, "f1")
        gaps: tuple[Gap, ...] = ()
        if self.take(Fault.STALE):
            gaps = (Gap.model_validate({"source": source, "reason": f"The {stale} is stale."}),)
        return slice_page(page, found, gaps, offset, limit, "f1")

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
