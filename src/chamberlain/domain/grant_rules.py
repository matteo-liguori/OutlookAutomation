"""Pure grant rules: requirement selection, scope matching, review dates.

Contract: docs/contracts/05-grants.md (G-1 to G-16, G-6); the permission rows live in
domain/permissions.py and
docs/contracts/04-writes.md (W-22: exact-field scope matching, never fuzzy).
Nothing here reads a clock, a store or a connector.
"""

import calendar
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from chamberlain.domain.canonical import content_hash
from chamberlain.domain.grants import Requirement, StandingScope
from chamberlain.domain.intents import (
    CalendarRespond,
    IntentKind,
    MailMarkRead,
    MailMove,
    MailTrash,
    OutsideAction,
    WriteIntent,
)
from chamberlain.domain.permissions import Facts, Needs, cover_for, needs_for, recipients_of
from chamberlain.limits import REVIEW_GRACE_DAYS

_STRICTNESS = {
    Requirement.NONE: 0,
    Requirement.AGENT_EVIDENCE: 1,
    Requirement.HUMAN_CONFIRMATION: 2,
}

HARNESS_TOKEN_FIELDS = ("message_id", "uid")
"""Tokens the harness assigns at reservation; excluded from the hash (board R1)."""


# --- intent facts ---------------------------------------------------------------


def intent_hash(intent: WriteIntent) -> str:
    """`intent_sha256`: the content hash without harness-assigned tokens (R1)."""
    blank = {name: None for name in HARNESS_TOKEN_FIELDS if name in type(intent).model_fields}
    return content_hash(intent.model_copy(update=blank))


def stricter(a: Requirement, b: Requirement) -> Requirement:
    return a if _STRICTNESS[a] >= _STRICTNESS[b] else b


def requirement_for(
    intent: WriteIntent,
    known_recipients: frozenset[str],
    *,
    send_known: Requirement,
    other: Requirement,
    stored_attendees: frozenset[str] = frozenset(),
    owner_addresses: frozenset[str] = frozenset(),
) -> Requirement:
    """What it takes to activate a one-off grant for this intent, read from the permission table.

    `send_known` and `other` come from config and may only tighten a row. `stored_attendees`
    are the guests already on the event an update acts on, so they never count as new.
    """
    needs = needs_for(Facts(intent, known_recipients, stored_attendees, owner_addresses))
    match needs:
        case Needs.CLICK:
            return Requirement.HUMAN_CONFIRMATION
        case Needs.WORD_SEND_KNOWN:
            return stricter(send_known, Requirement.AGENT_EVIDENCE)
        case Needs.WORD_OTHER:
            return stricter(other, Requirement.AGENT_EVIDENCE)


def check_config_requirement(value: Requirement) -> Requirement:
    """Config may tighten a row, never loosen it to `none` (G-12)."""
    from_config = {Requirement.AGENT_EVIDENCE, Requirement.HUMAN_CONFIRMATION}
    if value not in from_config:
        raise ValueError("a configured requirement is agent_evidence or human_confirmation")
    return value


# --- scope matching (W-22) ------------------------------------------------------


def _domain(address: str) -> str:
    return address.rsplit("@", 1)[-1].lower()


def scope_mismatch(scope: StandingScope, intent: WriteIntent, card: str | None) -> bool:
    """True when the scope does not cover the intent. Exact fields only (W-22)."""
    if not scope.covers(intent.source) or IntentKind(intent.kind) not in scope.intent_kinds:
        return True
    if scope.card is not None and scope.card != card:
        return True
    if scope.calendar is not None and scope.calendar != intent.source:
        return True
    if isinstance(intent, OutsideAction):
        return (
            card is None
            or scope.where is None
            or scope.where.strip().lower() != intent.where.strip().lower()
            or scope.categories is None
            or intent.category not in scope.categories
        )
    if isinstance(intent, CalendarRespond) and intent.organiser is None:
        return True
    cover = cover_for(IntentKind(intent.kind))
    if cover.never:
        return True
    addresses = recipients_of(intent)
    if addresses:
        covered = scope.recipients
        if cover.domains:
            covered = covered | {a for a in addresses if _domain(a) in scope.recipient_domains}
        if not addresses <= covered:
            return True
    if cover.folders:
        if isinstance(intent, MailMove):
            return intent.to_folder not in scope.folders or (
                intent.mark_read and not scope.allow_mark_read
            )
        if isinstance(intent, MailTrash):
            return intent.from_folder is None or intent.from_folder not in scope.folders
        return True  # A table entry requiring a folder must supply a known folder rule.
    return isinstance(intent, MailMarkRead) and not scope.allow_mark_read


def narrowness(scope: StandingScope) -> tuple[int, int, int]:
    """Smaller sorts first: the narrowest covering standing grant wins (W-22); of two that are
    otherwise alike, the one covering fewer sources (G-26)."""
    return (
        len(scope.intent_kinds),
        len(scope.recipients) + 100 * len(scope.recipient_domains),
        len(scope.all_sources()),
    )


# --- review dates (G-6 to G-8) ---------------------------------------------------


def anniversary(granted_at: datetime, months: int, zone: ZoneInfo) -> datetime:
    """The `months`-th monthly anniversary: same local day and wall time, clamped."""
    local = granted_at.astimezone(zone)
    index = local.year * 12 + (local.month - 1) + months
    year, month0 = divmod(index, 12)
    month = month0 + 1
    day = min(local.day, calendar.monthrange(year, month)[1])
    wall = datetime(year, month, day, local.hour, local.minute, local.second, tzinfo=zone)
    return wall.astimezone(UTC)


def next_review(granted_at: datetime | None, after: datetime, zone: ZoneInfo) -> datetime:
    """The first anniversary strictly after `after`. No grant date means due at once."""
    if granted_at is None:
        return after
    months = 1
    while anniversary(granted_at, months, zone) <= after:
        months += 1
    return anniversary(granted_at, months, zone)


def suspend_at(review_at: datetime) -> datetime:
    """When an unreviewed standing grant stops authorising (G-8)."""
    return review_at + timedelta(days=REVIEW_GRACE_DAYS)
