"""Plain data the writes track passes around: policy, rows, stored receipts.

Contract: docs/contracts/04-writes.md, 05-grants.md. Rows are read from the
store by `repo.py`; the pure rules in `authorise.py` see only these values.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from pydantic import Field, TypeAdapter

from chamberlain.domain.grant_rules import check_config_requirement
from chamberlain.domain.grants import GrantKind, GrantState, Requirement, StandingScope
from chamberlain.domain.intents import IntentKind, WriteIntent
from chamberlain.domain.operations import ItemResult, OperationState, Receipt
from chamberlain.limits import SENDS_PER_HOUR
from chamberlain.schema.base import Handle, Model
from chamberlain.schema.errors import ChamberlainError, ErrorCode

_INTENT: TypeAdapter[WriteIntent] = TypeAdapter(WriteIntent)


@dataclass(frozen=True)
class WritePolicy:
    """The configurable part of authority (G-12, G-16, G-6)."""

    send_known: Requirement = Requirement.AGENT_EVIDENCE
    other: Requirement = Requirement.AGENT_EVIDENCE
    sends_per_hour: int = SENDS_PER_HOUR
    zone: str = "UTC"

    def __post_init__(self) -> None:
        try:
            check_config_requirement(self.send_known)
            check_config_requirement(self.other)
            ZoneInfo(self.zone)
        except (ValueError, KeyError) as error:
            raise ChamberlainError(
                ErrorCode.CONFIG_INVALID, "The write permission settings are not valid."
            ) from error
        if not 1 <= self.sends_per_hour <= SENDS_PER_HOUR:
            raise ChamberlainError(
                ErrorCode.CONFIG_INVALID, "The send limit may only be tightened."
            )

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.zone)


@dataclass(frozen=True)
class GrantRow:
    handle: str
    kind: GrantKind
    state: GrantState
    source_id: str
    title: str
    summary: str
    requirement: Requirement
    content_sha256: str
    scope: StandingScope | None
    card: str | None
    supersedes: str | None
    proposed_by: str | None
    proposed_at: datetime
    confirmed_via: str | None
    granted_at: datetime | None
    expires_at: datetime | None
    review_at: datetime | None
    last_reviewed_at: datetime | None
    ended_at: datetime | None
    compacted_at: datetime | None
    action: str | None = None
    """A held send: the card action, waiting for the owner, that asks them to approve it."""

    def covers(self, source: str) -> bool:
        """True when this grant is for `source`: its own, or one its scope lists (G-26)."""
        listed = None if self.scope is None else self.scope.sources
        return self.source_id == source or (listed is not None and source in listed)


@dataclass(frozen=True)
class OperationRow:
    handle: str
    idempotency_key: str
    kind: IntentKind
    source_id: str
    state: OperationState
    grant: str | None
    card: str | None
    session: str | None
    intent_sha256: str
    intent_json: str | None
    reconcile_token: str | None
    refs_json: str | None
    prepared_json: str | None
    receipt_json: str | None
    client_verified: bool
    server_verified: bool
    error_code: str | None
    lease_owner: str | None
    lease_expires_at: datetime | None
    created_at: datetime
    dispatched_at: datetime | None
    settled_at: datetime | None
    compacted_at: datetime | None
    updated_at: datetime | None = None
    summary_json: str | None = None
    """Display and matching text of an operation imported from an old journal. Never an intent."""
    attempted_at: datetime | None = None
    """When the old journal says the write was tried. Not the import time."""
    rechecked_at: datetime | None = None
    """When the harness made its one 24-hour look for an uncertain send (W-37)."""
    recheck_result: str | None = None
    """`found`, `queued` (waiting in Outbox or Drafts) or `missing`."""
    recheck_json: str | None = None
    """Where that look went: `{"looked": [...]}`. No message text."""
    redo_of: str | None = None
    """On a resend: the uncertain send it replaces (W-38)."""

    def intent(self) -> WriteIntent:
        if self.intent_json is None:
            raise ChamberlainError(
                ErrorCode.OPERATION_ALREADY_SETTLED, "That operation has been compacted."
            )
        return _INTENT.validate_json(self.intent_json)

    def refs(self) -> dict[str, str]:
        stored: dict[str, str] = json.loads(self.refs_json or "{}")
        return stored

    def receipt(self) -> "StoredReceipt | None":
        return StoredReceipt.model_validate_json(self.receipt_json) if self.receipt_json else None


class StoredReceipt(Model):
    """What `operations.receipt_json` holds (S-2). Compaction keeps `summary` only (W-25)."""

    summary: Receipt
    result_ref: Handle | None = None
    items: tuple[ItemResult, ...] = ()
    warnings: tuple[str, ...] = ()
    reported: str | None = Field(default=None, max_length=500)
    """What the assistant said it did, for an outside action (W-46). Compaction drops it."""


@dataclass(frozen=True)
class Proposal:
    """What `propose` learned (T-13): the operation or grant, what it needs, and a preview."""

    operation: OperationRow | None
    grant: GrantRow | None
    state: OperationState | GrantState
    requirement: Requirement
    ready: bool
    content_sha256: str
    preview: str
    truncated: bool
    origin: str
