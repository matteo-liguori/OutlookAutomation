"""Typed shapes inside the attention track: attributes, drafts, detector input.

Contract: docs/contracts/07-attention.md (Events, Detectors, A-5).
"""

from dataclasses import dataclass, field
from datetime import datetime

from chamberlain.attention.tuning import PRIORITY
from chamberlain.domain.capabilities import HealthState
from chamberlain.domain.events import EventKind, EventState
from chamberlain.domain.grants import Requirement
from chamberlain.domain.intents import IntentKind
from chamberlain.limits import SUMMARY_MAX
from chamberlain.memory.repo import ActionRow, BudgetRow, CardRow, ComplexDecision
from chamberlain.review.repo import RoundRow
from chamberlain.runs.models import NoteRow
from chamberlain.runs.repo import DueRun
from chamberlain.schema.base import CardHandle, Model, SourceId
from chamberlain.writes.repo import GrantAlert, OperationAlert, SendRow


class EventAttrs(Model):
    """The small typed attributes rule filters match (the `attrs_json` model, S-2)."""

    intent_kind: IntentKind | None = None
    requirement: Requirement | None = None
    reason: str | None = None
    source: SourceId | None = None
    card: CardHandle | None = None
    health: HealthState | None = None
    since: str | None = None
    """Mail batches: the oldest received time, so the next call can list them. A calendar
    conflict: when the overlap starts."""
    until: str | None = None
    """A calendar conflict: when the overlap ends."""


@dataclass(frozen=True)
class EventDraft:
    """What a detector reports. The inbox turns it into a row (A-1)."""

    kind: EventKind
    subject: str | None
    summary: str
    priority: int
    attrs: EventAttrs = field(default_factory=EventAttrs)
    discriminator: str | None = None

    def __post_init__(self) -> None:
        if len(self.summary) > SUMMARY_MAX:
            raise ValueError("summary too long")

    @property
    def dedupe_key(self) -> str:
        key = f"{self.kind.value}:{self.subject or '-'}"
        return key if self.discriminator is None else f"{key}:{self.discriminator}"


def draft(
    kind: EventKind,
    subject: str | None,
    summary: str,
    attrs: EventAttrs | None = None,
    *,
    bump: int = 0,
    priority: int | None = None,
    discriminator: str | None = None,
) -> EventDraft:
    """A detector's report at the kind's priority (plus `bump`, or `priority` when given)."""
    return EventDraft(
        kind,
        subject,
        summary,
        PRIORITY[kind] + bump if priority is None else priority,
        attrs or EventAttrs(),
        discriminator,
    )


@dataclass(frozen=True)
class EventRow:
    handle: str
    kind: EventKind
    subject: str | None
    dedupe_key: str
    priority: int
    summary: str
    attrs: EventAttrs
    state: EventState
    detected_at: str
    delivered_at: str | None
    delivered_to: str | None
    closed_at: str | None
    quiet_until: str | None


@dataclass(frozen=True)
class SessionRow:
    handle: str
    agent: str
    purpose: str
    started_at: str
    last_seen_at: str
    ended_at: str | None


@dataclass(frozen=True)
class MailBatch:
    """New mail of one source awaiting triage: counts and times only, never text (E-8)."""

    source: str
    count: int
    first_seq: int
    """Sequence of the oldest untriaged row; fixed for the batch, so it names it."""
    oldest_received: str
    suggested: int
    """How many of the messages come with a suggested open card."""
    sent: bool = False
    """Mail the owner sent from another device that matches an open card (a low-priority line)."""


@dataclass(frozen=True)
class MailGap:
    """Mail of `source` received between `start` and `end` (UTC) has not been looked at."""

    source: str
    start: str
    end: str


@dataclass(frozen=True)
class Held:
    """An older span that was not read (more than a day back), and where the owner's answer
    stands (A-23)."""

    start: str
    end: str
    state: str
    estimate: int | None
    width: int | None


@dataclass(frozen=True)
class SyncGap:
    """Folders of `source` that would not sync, as roles (folder names are remote text, E-8)."""

    source: str
    roles: tuple[str, ...]
    since: str
    """The oldest time any of them was last known in sync, or the first try."""


@dataclass(frozen=True)
class SourceSnapshot:
    id: str
    state: HealthState


@dataclass(frozen=True)
class CalendarSlot:
    """One of the owner's events in the scanned window: where and when, never its text (E-8).

    `fingerprint` is a hash of the title and times, so a copy of one event held by two
    calendars can be told from two different events."""

    source: str
    locator: str
    uid: str
    start: datetime
    end: datetime
    fingerprint: str
    information_only: bool = False
    """From a calendar kept for information (`commitments: false`): other people's plans."""
    title: str = field(default="", repr=False)
    location: str = field(default="", repr=False)
    """Remote text, kept in memory only to tell one event on two calendars from two events
    (domain/same_event.py). Never written to an event, a summary or a log."""


@dataclass(frozen=True)
class DetectorInput:
    """Everything detectors may look at (A-5). Built once per tick."""

    now: datetime
    sources: tuple[SourceSnapshot, ...] = ()
    mail: tuple[MailBatch, ...] = ()
    mail_gaps: tuple[MailGap, ...] = ()
    mail_held: tuple[tuple[str, Held], ...] = ()
    """Each source with its held span."""
    sync_gaps: tuple[SyncGap, ...] = ()
    actions: tuple[ActionRow, ...] = ()
    cards: tuple[CardRow, ...] = ()
    open_cards: tuple[CardRow, ...] = ()
    complex_decisions: tuple[ComplexDecision, ...] = ()
    linked_ids: frozenset[str] = frozenset()
    """Handles and Message-IDs already linked to a card."""
    linked_cards: dict[str, str] = field(default_factory=lambda: {})
    """Linked handle or Message-ID to card handle."""
    grants: tuple[GrantAlert, ...] = ()
    review_grants: tuple[GrantAlert, ...] = ()
    operations: tuple[OperationAlert, ...] = ()
    sends: tuple[SendRow, ...] = ()
    budgets: tuple[BudgetRow, ...] = ()
    orphan_batch: str | None = None
    """`found_at` of the latest held quarantine batch, if any."""
    orphan_count: int = 0
    runs_due: tuple[DueRun, ...] = ()
    notes: tuple[NoteRow, ...] = ()
    calendar_slots: tuple[CalendarSlot, ...] = ()
    """The owner's timed events from now on that are neither cancelled nor declined (A-26)."""
    update_tag: str | None = None
    """The newer release tag the update checker recorded, if any."""
    reviews: tuple[RoundRow, ...] = ()
    """Open review rounds: how the agent has been working outside the harness (review track)."""
