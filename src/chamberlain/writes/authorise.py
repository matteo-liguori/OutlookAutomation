"""`authorise`: the pure decision (W-20 to W-22, G-16).

No clock, no store, no connector. The engine loads the facts, calls this
inside the transaction that reserves the grant use, and acts on the answer.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from chamberlain.domain.grant_rules import (
    narrowness,
    requirement_for,
    scope_mismatch,
    suspend_at,
)
from chamberlain.domain.grants import GrantKind, GrantState, Requirement
from chamberlain.domain.intents import IntentKind, MailMarkRead, MailSend, WriteIntent
from chamberlain.schema.errors import ErrorCode
from chamberlain.writes.models import GrantRow, WritePolicy


@dataclass(frozen=True)
class OperationFacts:
    """The operation being authorised."""

    handle: str
    intent_sha256: str
    card: str | None = None
    reserved_grant: str | None = None
    """Set when `do` is resuming an operation that already reserved a grant (W-21)."""


@dataclass(frozen=True)
class Usage:
    """What the store says is already spent, reserved or known.

    All counts exclude the operation being authorised, so a resumed operation
    is not counted against itself (G-11).
    """

    live_uses: Mapping[str, int] = field(default_factory=dict[str, int])
    """Reserved or spent uses per grant, any age (G-11)."""
    recent_uses: Mapping[str, int] = field(default_factory=dict[str, int])
    """Uses per grant that count against `max_per_day`: dispatched in the last 24 hours,
    or reserved and not yet dispatched."""
    source_sends_last_hour: int = 0
    known_recipients: frozenset[str] = frozenset()
    mail_folders: frozenset[str] = frozenset()
    event_attendees: frozenset[str] = frozenset()
    """The guests already on the stored event an update acts on (read at propose)."""
    owner_addresses: frozenset[str] = frozenset()
    """The owner's own addresses: every source's address and `authorised_senders.owner`."""


@dataclass(frozen=True)
class Allow:
    grant: str
    allow_duplicate: bool = False


@dataclass(frozen=True)
class Deny:
    code: ErrorCode
    requirement: Requirement


Decision = Allow | Deny


def requirement_needed(intent: WriteIntent, usage: Usage, policy: WritePolicy) -> Requirement:
    return requirement_for(
        intent,
        usage.known_recipients,
        send_known=policy.send_known,
        other=policy.other,
        stored_attendees=usage.event_attendees,
        owner_addresses=usage.owner_addresses,
    )


def _only_to_owner(intent: WriteIntent, owner: frozenset[str]) -> bool:
    """Every recipient is one of the owner's own addresses: the hourly cap guards against
    mistaken mail to other people, so it does not apply (a live test sends only like this)."""
    if not isinstance(intent, MailSend):
        return False
    people = [*intent.to, *intent.cc, *intent.bcc]
    return bool(people) and all(str(p).lower() in owner for p in people)


def authorise(
    intent: WriteIntent,
    operation: OperationFacts,
    grants: Sequence[GrantRow],
    usage: Usage,
    policy: WritePolicy,
    now: datetime,
) -> Decision:
    """`allow(grant)` or `deny(code, requirement)`. Same inputs, same answer (W-20)."""
    needed = requirement_needed(intent, usage, policy)
    if (
        intent.kind is IntentKind.MAIL_SEND
        and usage.source_sends_last_hour >= policy.sends_per_hour
        and not _only_to_owner(intent, usage.owner_addresses)
    ):
        return Deny(ErrorCode.GRANT_LIMIT_REACHED, needed)
    if operation.reserved_grant is not None:
        pool = [g for g in grants if g.handle == operation.reserved_grant]
        if not pool:
            return Deny(ErrorCode.GRANT_NOT_ACTIVE, needed)
    else:
        pool = _candidates(grants, operation, intent)
    denials: list[ErrorCode] = []
    for grant in pool:
        verdict = _verdict(grant, intent, operation, usage, now)
        if verdict is None:
            scope = grant.scope
            return Allow(grant.handle, allow_duplicate=scope is not None and scope.allow_duplicate)
        denials.append(verdict)
    code = _best(denials)
    if code is None:
        new_recipient = needed is Requirement.HUMAN_CONFIRMATION and (
            intent.kind is IntentKind.MAIL_SEND
        )
        code = ErrorCode.RECIPIENT_UNKNOWN if new_recipient else ErrorCode.GRANT_REQUIRED
    return Deny(code, needed)


def _candidates(
    grants: Sequence[GrantRow], operation: OperationFacts, intent: WriteIntent
) -> list[GrantRow]:
    """One-offs for exactly this content first, then standing grants narrowest first (W-22)."""
    live = [g for g in grants if g.covers(intent.source) and g.state is GrantState.ACTIVE]
    one_offs = [
        g
        for g in live
        if g.kind is GrantKind.ONE_OFF and g.content_sha256 == operation.intent_sha256
    ]
    standing = [g for g in live if g.kind is GrantKind.STANDING and g.scope is not None]
    standing.sort(key=lambda g: narrowness(g.scope) if g.scope is not None else (0, 0, 0))
    return [*sorted(one_offs, key=lambda g: g.handle), *standing]


def _verdict(
    grant: GrantRow,
    intent: WriteIntent,
    operation: OperationFacts,
    usage: Usage,
    now: datetime,
) -> ErrorCode | None:
    """None if this grant authorises the intent now, else the reason it does not."""
    if grant.state is not GrantState.ACTIVE or not grant.covers(intent.source):
        return ErrorCode.GRANT_NOT_ACTIVE
    if grant.expires_at is None or grant.expires_at <= now:
        return ErrorCode.GRANT_NOT_ACTIVE
    if grant.card is not None and grant.card != operation.card:
        return ErrorCode.GRANT_SCOPE_MISMATCH
    if grant.kind is GrantKind.ONE_OFF:
        if grant.content_sha256 != operation.intent_sha256:
            return ErrorCode.GRANT_CONTENT_MISMATCH
        if usage.live_uses.get(grant.handle, 0) >= 1:
            return ErrorCode.GRANT_LIMIT_REACHED
        return None
    scope = grant.scope
    if scope is None:
        return ErrorCode.GRANT_NOT_ACTIVE
    if grant.review_at is not None and now >= suspend_at(grant.review_at):
        return ErrorCode.GRANT_NOT_ACTIVE
    if (
        isinstance(intent, MailMarkRead)
        and scope.folders
        and (not usage.mail_folders or not usage.mail_folders <= scope.folders)
    ):
        return ErrorCode.GRANT_SCOPE_MISMATCH
    if scope_mismatch(scope, intent, operation.card):
        return ErrorCode.GRANT_SCOPE_MISMATCH
    used = usage.recent_uses.get(grant.handle, 0)
    if scope.max_per_day is not None and used >= scope.max_per_day:
        return ErrorCode.GRANT_LIMIT_REACHED
    return None


def _best(codes: list[ErrorCode]) -> ErrorCode | None:
    for wanted in (
        ErrorCode.GRANT_LIMIT_REACHED,
        ErrorCode.GRANT_SCOPE_MISMATCH,
        ErrorCode.GRANT_CONTENT_MISMATCH,
        ErrorCode.GRANT_NOT_ACTIVE,
    ):
        if wanted in codes:
            return wanted
    return None
