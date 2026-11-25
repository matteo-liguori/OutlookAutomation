"""Operation states, the legal transitions and what connectors report.

Contract: docs/contracts/04-writes.md (States, W-6, W-10) and
docs/contracts/08-ports.md (P-9 to P-12).
"""

import json
from enum import StrEnum
from typing import Any, Self, cast

from pydantic import Field, JsonValue, model_validator

from chamberlain.domain.intents import IntentKind
from chamberlain.schema.base import Model, Sha256, SourceId, Timestamp, WriteSourceId
from chamberlain.schema.errors import ChamberlainError, ErrorCode


class OperationState(StrEnum):
    PROPOSED = "proposed"
    AUTHORISED = "authorised"
    PREPARED = "prepared"
    DISPATCHED = "dispatched"
    CONFIRMED = "confirmed"
    FAILED_CLEAN = "failed_clean"
    UNCERTAIN = "uncertain"
    RECONCILING = "reconciling"
    CONFIRMED_ABSENT = "confirmed_absent"
    CANCELLED = "cancelled"
    ABANDONED = "abandoned"
    SUPERSEDED = "superseded"
    AGENT_REPORTED = "agent_reported"
    """An outside action the assistant says it did. The harness did not do it and cannot check
    it, so this is never `confirmed` (04-writes W-46)."""


_S = OperationState

TRANSITIONS: dict[OperationState, frozenset[OperationState]] = {
    _S.PROPOSED: frozenset({_S.AUTHORISED, _S.CANCELLED}),
    _S.AUTHORISED: frozenset(
        {_S.PREPARED, _S.FAILED_CLEAN, _S.CANCELLED, _S.AGENT_REPORTED, _S.ABANDONED}
    ),
    _S.PREPARED: frozenset({_S.DISPATCHED, _S.FAILED_CLEAN}),
    _S.DISPATCHED: frozenset({_S.CONFIRMED, _S.FAILED_CLEAN, _S.UNCERTAIN}),
    _S.UNCERTAIN: frozenset(
        {_S.RECONCILING, _S.CONFIRMED, _S.CONFIRMED_ABSENT, _S.ABANDONED, _S.SUPERSEDED}
    ),
    _S.RECONCILING: frozenset({_S.CONFIRMED, _S.CONFIRMED_ABSENT, _S.UNCERTAIN}),
    _S.CONFIRMED: frozenset(),
    _S.FAILED_CLEAN: frozenset(),
    _S.CONFIRMED_ABSENT: frozenset(),
    _S.CANCELLED: frozenset(),
    _S.ABANDONED: frozenset(),
    _S.SUPERSEDED: frozenset(),
    _S.AGENT_REPORTED: frozenset(),
}
"""Exactly the arrows of the state diagram (W-6).

`uncertain -> confirmed | confirmed_absent` directly is the user resolving it
in the viewer (W-13); the engine itself always goes through `reconciling`.
"""

TERMINAL_STATES = frozenset(state for state, targets in TRANSITIONS.items() if not targets)

RELEASING_STATES = frozenset({_S.FAILED_CLEAN, _S.CONFIRMED_ABSENT, _S.CANCELLED, _S.SUPERSEDED})
"""States that free the grant use (G-11). `proposed` never reserved one. `superseded` frees it
because the resend that replaces the send takes it over in the same transaction (W-38)."""

MAY_HAVE_HAPPENED_STATES = frozenset({_S.DISPATCHED, _S.UNCERTAIN, _S.RECONCILING})
"""`do` in these states fails with `operation.needs_reconcile` (W-17)."""


OUTSIDE_STATE_WORDS = {
    "proposed": "waiting for permission",
    "authorised": "allowed; the assistant has not reported yet",
    "agent_reported": "done, as reported by the assistant (Chamberlain did not check it)",
    "abandoned": "allowed, but the assistant never reported",
    "cancelled": "withdrawn before it was done",
    "failed_clean": "did not happen",
}
"""An outside action's state in the owner's words (04-writes W-47)."""


def outside_details(intent_json: str | None, receipt_json: str | None) -> tuple[str, str, str]:
    """`(where, what, reported)` of an outside action, for the report, timeline and grant page.

    Empty strings once the stored request has been compacted away."""
    try:
        intent = cast(dict[str, Any], json.loads(intent_json or "{}"))
        receipt = cast(dict[str, Any], json.loads(receipt_json or "{}"))
        return (
            str(intent.get("where", "")),
            str(intent.get("what", "")),
            str(receipt.get("reported") or ""),
        )
    except (ValueError, AttributeError):
        return "", "", ""


def recheck_text(result: str | None, looked_json: str | None) -> str | None:
    """What an uncertain send's one 24-hour look found, in one line (W-37)."""
    if result is None:
        return None
    try:
        parsed = json.loads(looked_json or "{}")
        looked = [str(item) for item in parsed.get("looked", [])]
    except (ValueError, AttributeError):
        looked = []
    where = "; ".join(looked).replace(chr(10), " ")[:300]
    return f"{result} (looked in {where})" if where else result


def check_transition(current: OperationState, target: OperationState) -> None:
    """Raise unless `current -> target` is an arrow of the diagram (W-6)."""
    if target not in TRANSITIONS[current]:
        raise ChamberlainError(
            ErrorCode.INTERNAL_UNEXPECTED,
            f"Illegal operation transition {current.value} -> {target.value}.",
        )


class Verification(Model):
    """How far a result has been checked (W-14)."""

    client_verified: bool = False
    server_verified: bool = False


class ItemResult(Model):
    """One element of a multi-item write such as `mail.mark_read` (W-24)."""

    locator: str
    ok: bool
    code: ErrorCode | None = None


class ProviderReceipt(Model):
    """What a connector says happened. Never agent-facing as is."""

    locator: str | None = None
    """Stable locator of the item written, where there is one."""
    etag: str | None = None
    remote_sha256: Sha256 | None = None
    verification: Verification = Verification()
    items: tuple[ItemResult, ...] = ()
    read_state_preserved: bool | None = None
    """False when a merge into an existing copy could not keep the flag (R7)."""
    warnings: tuple[str, ...] = ()
    """Harness-safe warning codes such as `merged_existing_copy` (R7)."""


class Prepared(Model):
    """Everything `dispatch` and `reconcile` need; JSON-serialisable (P-9)."""

    source: SourceId
    intent_kind: IntentKind
    reconcile_token: str | None = None
    data: dict[str, JsonValue] = Field(default_factory=dict)


class DispatchOutcome(StrEnum):
    APPLIED = "applied"
    NOT_APPLIED = "not_applied"
    UNKNOWN = "unknown"


class DispatchResult(Model):
    outcome: DispatchOutcome
    receipt: ProviderReceipt | None = None
    detail: str = ""
    """For the log only (E-15)."""

    @model_validator(mode="after")
    def _receipt_iff_applied(self) -> Self:
        if (self.outcome is DispatchOutcome.APPLIED) != (self.receipt is not None):
            raise ValueError("a receipt accompanies exactly the applied outcome")
        return self


class ReconcileStatus(StrEnum):
    PRESENT = "present"
    ABSENT = "absent"
    QUEUED = "queued"
    """An exact copy sits in Outbox or Drafts awaiting submission. Keeps the
    operation uncertain and never releases the grant (board R2)."""
    UNKNOWN = "unknown"


class ReconcileResult(Model):
    status: ReconcileStatus
    receipt: ProviderReceipt | None = None
    proof: bool = False
    """True only where absence is provable and coverage was complete (P-12)."""
    detail: str = ""

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.status is ReconcileStatus.QUEUED and self.proof:
            raise ValueError("a queued copy is never proof of absence")
        if (self.status is ReconcileStatus.PRESENT) != (self.receipt is not None):
            raise ValueError("a receipt accompanies exactly the present status")
        if self.proof and self.status is not ReconcileStatus.ABSENT:
            raise ValueError("proof qualifies only an absent result")
        return self


class Receipt(Model):
    """The stored, agent-safe record of a settled operation (W-16, W-25)."""

    intent_kind: IntentKind
    source: WriteSourceId
    state: OperationState
    intent_sha256: Sha256
    settled_at: Timestamp
    verification: Verification = Verification()
    items_ok: int = 0
    items_failed: int = 0
