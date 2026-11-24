"""Pure grant rules: requirement selection, scope matching, review dates.

Contract: docs/contracts/05-grants.md (G-1 to G-16, G-12 table, G-6) and
docs/contracts/04-writes.md (W-22: exact-field scope matching, never fuzzy).
Nothing here reads a clock, a store or a connector.
"""

import calendar
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from chamberlain.domain.canonical import content_hash
from chamberlain.domain.grants import Requirement, StandingScope
from chamberlain.domain.intents import (
    CalendarCreate,
    CalendarDelete,
    CalendarRespond,
    CalendarUpdate,
    IntentKind,
    MailMarkRead,
    MailMove,
    MailSend,
    MailTrash,
    WriteIntent,
)
from chamberlain.limits import REVIEW_GRACE_DAYS

_TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_STRICTNESS = {
    Requirement.NONE: 0,
    Requirement.AGENT_EVIDENCE: 1,
    Requirement.HUMAN_CONFIRMATION: 2,
}

HARNESS_TOKEN_FIELDS = ("message_id", "uid")
"""Tokens the harness assigns at reservation; excluded from the hash (board R1)."""


def parse_ts(text: str) -> datetime:
    """A stored timestamp (D-7) as an aware UTC datetime."""
    return datetime.strptime(text, _TS_FORMAT).replace(tzinfo=UTC)


def format_ts(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime(_TS_FORMAT)


# --- intent facts ---------------------------------------------------------------


def intent_hash(intent: WriteIntent) -> str:
    """`intent_sha256`: the content hash without harness-assigned tokens (R1)."""
    blank = {name: None for name in HARNESS_TOKEN_FIELDS if name in type(intent).model_fields}
    return content_hash(intent.model_copy(update=blank))


def recipients_of(intent: WriteIntent) -> frozenset[str]:
    """Every address the write would reach (to, cc, bcc; event attendees)."""
    match intent:
        case MailSend():
            return frozenset((*intent.to, *intent.cc, *intent.bcc))
        case CalendarCreate() | CalendarUpdate():
            return frozenset(intent.event.attendees)
        case CalendarRespond():
            return frozenset() if intent.organiser is None else frozenset({intent.organiser})
        case _:
            return frozenset()


def may_reach_attendees(intent: WriteIntent) -> bool:
    """Calendar updates and deletes act on an existing event whose attendees the intent omits.

    Used by scope matching: a standing grant never covers them. One-off requirement selection
    asks the stored event instead (`existing_attendees` of `requirement_for`).
    """
    return isinstance(intent, CalendarUpdate | CalendarDelete)


def stricter(a: Requirement, b: Requirement) -> Requirement:
    return a if _STRICTNESS[a] >= _STRICTNESS[b] else b


def requirement_for(
    intent: WriteIntent,
    known_recipients: frozenset[str],
    *,
    send_known: Requirement,
    other: Requirement,
    existing_attendees: bool = False,
    stored_attendees: frozenset[str] = frozenset(),
    owner_addresses: frozenset[str] = frozenset(),
) -> Requirement:
    """The G-12 table: what it takes to activate a one-off grant for this intent.

    `send_known` and `other` come from config and may only tighten a row. Calendar writes
    need the human only when the event reaches someone other than the owner (owner rulings
    2026-11-24): `existing_attendees` says the stored event an update or delete acts on has
    attendees and `stored_attendees` names them (empty while `existing_attendees` is true
    means they are not known, so the human decides). An answer to an invitation reaches the
    organiser only (G-12b).
    """
    if isinstance(intent, MailSend):
        if recipients_of(intent) <= known_recipients:
            return stricter(send_known, Requirement.AGENT_EVIDENCE)
        return Requirement.HUMAN_CONFIRMATION
    if isinstance(intent, CalendarRespond):
        if intent.organiser is not None and intent.organiser in owner_addresses:
            return stricter(other, Requirement.AGENT_EVIDENCE)
        return Requirement.HUMAN_CONFIRMATION
    if isinstance(intent, CalendarCreate | CalendarUpdate | CalendarDelete):
        if existing_attendees and not stored_attendees:
            return Requirement.HUMAN_CONFIRMATION
        reached = recipients_of(intent) | stored_attendees
        if reached and not reached <= owner_addresses:
            return Requirement.HUMAN_CONFIRMATION
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
    if scope.source != intent.source or IntentKind(intent.kind) not in scope.intent_kinds:
        return True
    if scope.card is not None and scope.card != card:
        return True
    if scope.calendar is not None and scope.calendar != intent.source:
        return True
    if isinstance(intent, CalendarRespond) and intent.organiser is None:
        return True
    addresses = recipients_of(intent)
    if addresses:
        if isinstance(intent, CalendarCreate | CalendarRespond):
            covered = scope.recipients
        else:
            covered = scope.recipients | {
                a for a in addresses if _domain(a) in scope.recipient_domains
            }
        if not addresses <= covered:
            return True
    if may_reach_attendees(intent):
        return True
    if isinstance(intent, MailMove) and (
        intent.to_folder not in scope.folders or (intent.mark_read and not scope.allow_mark_read)
    ):
        return True
    if isinstance(intent, MailTrash) and (
        intent.from_folder is None or intent.from_folder not in scope.folders
    ):
        return True
    return isinstance(intent, MailMarkRead) and not scope.allow_mark_read


def narrowness(scope: StandingScope) -> tuple[int, int]:
    """Smaller sorts first: the narrowest covering standing grant wins (W-22)."""
    return (len(scope.intent_kinds), len(scope.recipients) + 100 * len(scope.recipient_domains))


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
