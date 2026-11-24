"""Typed shapes inside the attention track: attributes, drafts, detector input.

Contract: docs/contracts/07-attention.md (Events, Detectors, A-5).
"""

from dataclasses import dataclass, field
from datetime import datetime

from chamberlain.domain.capabilities import HealthState
from chamberlain.domain.events import EventKind, EventState
from chamberlain.domain.grants import Requirement
from chamberlain.domain.intents import IntentKind
from chamberlain.limits import SUMMARY_MAX
from chamberlain.schema.base import CardHandle, Model, SourceId


class EventAttrs(Model):
    """The small typed attributes rule filters match (the `attrs_json` model, S-2)."""

    intent_kind: IntentKind | None = None
    requirement: Requirement | None = None
    reason: str | None = None
    source: SourceId | None = None
    card: CardHandle | None = None
    health: HealthState | None = None
    since: str | None = None
    """Mail batches: the oldest received time, so the next call can list them."""


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
class ActionRow:
    handle: str
    card: str
    due_at: str | None
    snooze_until: str | None


@dataclass(frozen=True)
class CardRow:
    handle: str
    title: str
    review_at: str | None
    over_cap: bool


@dataclass(frozen=True)
class GrantRow:
    handle: str
    state: str
    requirement: str
    card: str | None


@dataclass(frozen=True)
class OperationRow:
    handle: str
    kind: str
    state: str


@dataclass(frozen=True)
class BudgetRow:
    """`scope` is a card handle or None for the global total."""

    scope: str | None
    bytes_used: int
    limit: int


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


@dataclass(frozen=True)
class MailGap:
    """Mail of `source` received between `start` and `end` (UTC) has not been looked at."""

    source: str
    start: str
    end: str


@dataclass(frozen=True)
class SourceSnapshot:
    id: str
    state: HealthState


@dataclass(frozen=True)
class DueRun:
    """A scheduled pass that is due or in progress (the `runs` table, runs track)."""

    handle: str
    schedule: str
    kind: str
    day: str


@dataclass(frozen=True)
class OpenNote:
    """A note the person left in the viewer (the `user_notes` table, runs track)."""

    id: int
    text: str


@dataclass(frozen=True)
class DetectorInput:
    """Everything detectors may look at (A-5). Built once per tick."""

    now: datetime
    sources: tuple[SourceSnapshot, ...] = ()
    mail: tuple[MailBatch, ...] = ()
    mail_gaps: tuple[MailGap, ...] = ()
    actions: tuple[ActionRow, ...] = ()
    cards: tuple[CardRow, ...] = ()
    open_cards: tuple[CardRow, ...] = ()
    linked_ids: frozenset[str] = frozenset()
    """Handles and Message-IDs already linked to a card."""
    linked_cards: dict[str, str] = field(default_factory=lambda: {})
    """Linked handle or Message-ID to card handle."""
    grants: tuple[GrantRow, ...] = ()
    review_grants: tuple[GrantRow, ...] = ()
    operations: tuple[OperationRow, ...] = ()
    budgets: tuple[BudgetRow, ...] = ()
    orphan_batch: str | None = None
    """`found_at` of the latest held quarantine batch, if any."""
    orphan_count: int = 0
    runs_due: tuple[DueRun, ...] = ()
    notes: tuple[OpenNote, ...] = ()
    update_tag: str | None = None
    """The newer release tag the update checker recorded, if any."""
