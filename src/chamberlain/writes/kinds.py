"""Per-kind write inputs, provider calls and result handling. No state transitions."""

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

from chamberlain.app.context import Context
from chamberlain.domain.calendar import CalendarEvent
from chamberlain.domain.capabilities import Capability, CapabilityName, CapabilitySet
from chamberlain.domain.handles import HandleKind
from chamberlain.domain.intents import (
    CalendarCreate,
    CalendarDelete,
    CalendarRespond,
    CalendarUpdate,
    IntentKind,
    MailDraftUpdate,
    MailMarkRead,
    MailMove,
    MailTrash,
    OutsideAction,
    WriteIntent,
    attachments_of,
)
from chamberlain.domain.operations import (
    DispatchResult,
    Prepared,
    ProviderReceipt,
    ReconcileResult,
    ReconcileStatus,
)
from chamberlain.domain.permissions import recipients_of
from chamberlain.domain.recurrence import describe
from chamberlain.ports.calendar import CalendarPort
from chamberlain.ports.mail import MailPort
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.schema.safelog import log_failure
from chamberlain.writes import read_state
from chamberlain.writes.models import OperationRow
from chamberlain.writes.render import (
    EVENT_ATTENDEES_REF,
    EVENT_ORGANISER_REF,
    EVENT_REPEATS_REF,
    EVENT_WHEN_REF,
    describe_intent,
)

log = logging.getLogger("chamberlain.writes")
ALLOW_DUPLICATE_REF: Final = "_allow_duplicate"
INVITATIONS_UNSUPPORTED: Final = "invitations-not-supported"
RECURRENCE_UNSUPPORTED: Final = "recurrence-not-supported"


@dataclass(frozen=True)
class Located:
    """Stable locators and facts from the read-only location step (P-9)."""

    refs: dict[str, str] = field(default_factory=dict[str, str])
    event_attendees: frozenset[str] = frozenset()
    organiser: str | None = None
    when: str | None = None
    repeats: str | None = None

    @classmethod
    def from_refs(cls, refs: Mapping[str, str]) -> "Located":
        stable = dict(refs)
        guests = stable.pop(EVENT_ATTENDEES_REF, "")
        organiser = stable.pop(EVENT_ORGANISER_REF, None)
        when = stable.pop(EVENT_WHEN_REF, None)
        repeats = stable.pop(EVENT_REPEATS_REF, None)
        return cls(stable, frozenset(filter(None, guests.split(","))), organiser, when, repeats)

    def to_refs(self) -> dict[str, str]:
        """The render/storage adapter for the established operation row format."""
        facts = {
            EVENT_ATTENDEES_REF: ",".join(sorted(self.event_attendees)),
            EVENT_ORGANISER_REF: self.organiser,
            EVENT_WHEN_REF: self.when,
            EVENT_REPEATS_REF: self.repeats,
        }
        return {**self.refs, **{key: value for key, value in facts.items() if value}}


@dataclass(frozen=True)
class Kind:
    capability: CapabilityName | None
    handles: tuple[str, ...] = ()
    calendar: bool = False
    token: str | None = None
    result: HandleKind | None = None
    terminal_repeat: bool = False


_MAIL_DRAFT = CapabilityName.MAIL_DRAFT
_CALENDAR_WRITE = CapabilityName.CALENDAR_WRITE
KINDS: Final = {
    IntentKind.MAIL_SEND: Kind(CapabilityName.MAIL_SEND, ("reply_to",), token="message_id"),
    IntentKind.MAIL_DRAFT_SAVE: Kind(
        _MAIL_DRAFT, ("reply_to",), token="message_id", result=HandleKind.MESSAGE
    ),
    IntentKind.MAIL_DRAFT_UPDATE: Kind(_MAIL_DRAFT, ("reply_to", "draft"), token="message_id"),
    IntentKind.MAIL_DRAFT_DISCARD: Kind(_MAIL_DRAFT, ("draft",)),
    IntentKind.MAIL_MOVE: Kind(CapabilityName.MAIL_MOVE, ("messages",), terminal_repeat=True),
    IntentKind.MAIL_TRASH: Kind(CapabilityName.MAIL_TRASH, ("messages",), terminal_repeat=True),
    IntentKind.MAIL_MARK_READ: Kind(CapabilityName.MAIL_MARK_READ, ("messages",)),
    IntentKind.CALENDAR_CREATE: Kind(
        _CALENDAR_WRITE, calendar=True, token="uid", result=HandleKind.CALENDAR_EVENT
    ),
    IntentKind.CALENDAR_UPDATE: Kind(_CALENDAR_WRITE, ("event_ref",), calendar=True),
    IntentKind.CALENDAR_DELETE: Kind(_CALENDAR_WRITE, ("event_ref",), calendar=True),
    IntentKind.CALENDAR_RESPOND: Kind(_CALENDAR_WRITE, ("event_ref",), calendar=True),
    IntentKind.OUTSIDE_ACTION: Kind(None, terminal_repeat=True),
}


def port_for(ctx: Context, source: str, kind: IntentKind) -> MailPort | CalendarPort:
    """The connector a write of `kind` goes through; an outside action has none."""
    if kind is IntentKind.OUTSIDE_ACTION:
        raise _no_connector()
    sources = ctx.sources
    return sources.calendar_port(source) if KINDS[kind].calendar else sources.mail_port(source)


def capabilities(ctx: Context, source: str, kind: IntentKind) -> CapabilitySet:
    return port_for(ctx, source, kind).capabilities(source)


def require_write(caps: CapabilitySet, kind: IntentKind) -> Capability:
    name = KINDS[kind].capability
    found = caps.get(name) if name is not None else None
    if found is None:
        raise ChamberlainError(
            ErrorCode.CAPABILITY_UNSUPPORTED, "This source cannot do that write."
        )
    return found


def locate(ctx: Context, intent: WriteIntent) -> Located:
    """Resolve the intent's stable handles and read metadata needed by calendar edits."""
    refs: dict[str, str] = {}
    for name in KINDS[intent.kind].handles:
        value: str | tuple[str, ...] | None = getattr(intent, name)
        handles = (value,) if isinstance(value, str) else value or ()
        for handle in handles:
            source, locator = ctx.services.refs.locate(handle)
            if source != intent.source:
                raise ChamberlainError(
                    ErrorCode.REQUEST_INVALID, "A handle belongs to a different source."
                )
            refs[handle] = locator
    if not isinstance(intent, CalendarUpdate | CalendarDelete | CalendarRespond):
        return Located(refs)
    event = ctx.sources.calendar_port(intent.source).read(intent.source, refs[intent.event_ref])
    if isinstance(intent, CalendarRespond):
        if event.organiser is None or event.my_response is None:
            raise ChamberlainError(
                ErrorCode.REQUEST_INVALID,
                "That event is not an invitation to this calendar's owner.",
                fix="Only an event someone else invited the owner to can be answered.",
                field="event_ref",
            )
        return Located(refs, organiser=event.organiser)
    when, repeats = _check_scope(intent, event)
    if event.my_response is not None:
        raise ChamberlainError(
            ErrorCode.CAPABILITY_UNSUPPORTED,
            "This is someone else's invitation; only its organiser can change it.",
            fix="Answer it with calendar.respond instead.",
            detail="invitee-copy",
        )
    return Located(
        refs,
        event_attendees=frozenset(event.fields.attendees),
        when=when,
        repeats=repeats,
    )


def complete(
    ctx: Context,
    intent: WriteIntent,
    located: Located,
    card: str | None = None,
) -> WriteIntent:
    spec = KINDS[intent.kind]
    completed: dict[str, object] = {}
    if spec.token is not None:
        completed[spec.token] = None
    if isinstance(intent, MailMove | MailTrash):
        port = ctx.sources.mail_port(intent.source)
        folders = {port.folder_of(intent.source, located.refs[h]) for h in intent.messages}
        if len(folders) != 1:
            raise ChamberlainError(
                ErrorCode.REQUEST_INVALID,
                "A batch moves messages from one folder at a time.",
                field="messages",
            )
        completed["from_folder"] = folders.pop()
    if isinstance(intent, MailMove) and not intent.mark_read:
        completed["mark_read"] = _filing_marks_read(ctx, intent, card)
    if isinstance(intent, CalendarRespond):
        completed["organiser"] = located.organiser
    if isinstance(intent, CalendarCreate | CalendarUpdate):
        rule = intent.event.recurrence
        if rule is not None and rule.zone is None:
            completed["event"] = intent.event.model_copy(
                update={"recurrence": rule.model_copy(update={"zone": ctx.write_policy.zone})}
            )
    return intent.model_copy(update=completed)


def _filing_marks_read(ctx: Context, move: MailMove, card: str | None) -> bool:
    """The filing preference acts only through an active read-state grant (G-24)."""
    if not read_state.options(ctx.db).filing_marks_read:
        return False
    source = move.source
    if (
        read_state.active_grant(ctx, MailMarkRead(source=source, messages=move.messages), card=card)
        is not None
    ):
        return True
    read_state.propose_missing(
        ctx.db, ctx.clock, ctx.services.audit, [source], policy=ctx.write_policy
    )
    return False


def prepare_intent(
    ctx: Context, intent: WriteIntent, card: str | None = None
) -> tuple[WriteIntent, Located]:
    caps = capabilities(ctx, intent.source, intent.kind)
    cap = require_write(caps, intent.kind)
    located = locate(ctx, intent)
    _check_calendar_features(caps, intent, located)
    completed = complete(ctx, intent, located, card)
    _check_limits(ctx, completed, cap)
    return completed, located


def describe_write(intent: WriteIntent, located: Located) -> tuple[str, bool]:
    return describe_intent(intent, refs=located.to_refs())


def with_token(intent: WriteIntent, key: str) -> tuple[WriteIntent, str | None]:
    """Complete the harness-assigned token, derived from the idempotency key (R1, Q4)."""
    spec = KINDS[intent.kind]
    if spec.token is None:
        return intent, None
    token = key[:32] if spec.calendar else f"<{key[:32]}@chamberlain.invalid>"
    return intent.model_copy(update={spec.token: token}), token


def result_ref(ctx: Context, op: OperationRow, receipt: ProviderReceipt) -> str | None:
    result = KINDS[op.kind].result
    if result is None or receipt.locator is None:
        return None
    return ctx.services.refs.remember(result, op.source_id, receipt.locator)


def after_confirmed(
    ctx: Context, op: OperationRow, intent: WriteIntent, located: Located, receipt: ProviderReceipt
) -> None:
    """Keep handles alive after filing or updating drafts (R4)."""
    moves = [(item.locator, item.moved_to) for item in receipt.items if item.moved_to is not None]
    if isinstance(intent, MailDraftUpdate) and receipt.locator is not None:
        before = located.refs.get(intent.draft)
        if before is not None:
            moves.append((before, receipt.locator))
    for before, after in moves:
        if after == before:
            continue
        try:
            ctx.services.refs.repoint(before, after, source=op.source_id)
        except Exception:
            log_failure(log, "could not repoint a handle of %s", op.handle)


def port_prepare(ctx: Context, intent: WriteIntent, refs: Mapping[str, str]) -> Prepared:
    if isinstance(intent, OutsideAction):
        raise _no_connector()
    if isinstance(intent, CalendarCreate | CalendarUpdate | CalendarDelete | CalendarRespond):
        return ctx.sources.calendar_port(intent.source).prepare(intent, refs)
    return ctx.sources.mail_port(intent.source).prepare(intent, refs)


def port_dispatch(
    ctx: Context, intent: WriteIntent, prepared: Prepared, key: str, payloads: Mapping[str, bytes]
) -> DispatchResult:
    if isinstance(intent, OutsideAction):
        raise _no_connector()
    if KINDS[intent.kind].calendar:
        return ctx.sources.calendar_port(intent.source).dispatch(prepared, key)
    return ctx.sources.mail_port(intent.source).dispatch(prepared, key, payloads)


def port_reconcile(
    ctx: Context, intent: WriteIntent, prepared: Prepared, key: str
) -> ReconcileResult:
    try:
        return port_for(ctx, intent.source, intent.kind).reconcile(prepared, key)
    except Exception:
        log_failure(log, "reconcile failed")
        return ReconcileResult(status=ReconcileStatus.UNKNOWN)


def _no_connector() -> ChamberlainError:
    return ChamberlainError(ErrorCode.INTERNAL_UNEXPECTED, "An outside action has no connector.")


def _check_scope(
    intent: CalendarUpdate | CalendarDelete, event: CalendarEvent
) -> tuple[str | None, str | None]:
    """The scope must say which part of a repeating event is meant, and fit the handle (P-25).

    The fix names the handle to use, so the agent needs no standing knowledge of series.
    """
    scope = intent.scope
    if event.series_locator is None:
        if scope is not None:
            raise ChamberlainError(
                ErrorCode.REQUEST_INVALID,
                "That event does not repeat, so it takes no scope.",
                fix="Leave scope out.",
                field="scope",
            )
        if isinstance(intent, CalendarUpdate) and intent.event.recurrence is not None:
            raise ChamberlainError(
                ErrorCode.REQUEST_INVALID,
                "An event that exists cannot be made to repeat.",
                fix="Create a repeating event with calendar.create instead.",
                field="event",
            )
        return None, None
    wanted = "series" if event.is_series else "occurrence"
    if scope != wanted:
        fix = (
            "This is the whole series. Give scope series, or open one date and use that "
            "date's handle with scope occurrence."
            if event.is_series
            else "This is one date of a repeating event. Give scope occurrence, or use "
            "view.series of this event as the handle with scope series."
        )
        raise ChamberlainError(
            ErrorCode.REQUEST_INVALID,
            "Say which part of the repeating event this reaches.",
            fix=fix,
            field="scope",
        )
    if (
        isinstance(intent, CalendarUpdate)
        and scope == "occurrence"
        and intent.event.recurrence is not None
    ):
        raise ChamberlainError(
            ErrorCode.REQUEST_INVALID,
            "One date of a repeating event takes no repeat rule.",
            fix="Leave recurrence out, or change the series with scope series.",
            field="event",
        )
    if scope == "occurrence":
        return event.fields.start.isoformat(), None
    return None, describe(event.fields.recurrence) if event.fields.recurrence is not None else None


def _check_calendar_features(caps: CapabilitySet, intent: WriteIntent, located: Located) -> None:
    if not isinstance(intent, CalendarCreate | CalendarUpdate | CalendarDelete | CalendarRespond):
        return
    event = intent.event if isinstance(intent, CalendarCreate | CalendarUpdate) else None
    invites = (
        isinstance(intent, CalendarRespond)
        or located.event_attendees
        or (event and event.attendees)
    )
    if invites and caps.get(CapabilityName.CALENDAR_INVITE) is None:
        raise ChamberlainError(
            ErrorCode.CAPABILITY_UNSUPPORTED,
            "This calendar cannot send or answer invitations.",
            fix="Use a calendar that can, or write the event without other people.",
            detail=INVITATIONS_UNSUPPORTED,
        )
    scope = intent.scope if isinstance(intent, CalendarUpdate | CalendarDelete) else None
    if isinstance(intent, CalendarCreate) and intent.event.recurrence is not None:
        scope = "series"
    if scope is None:
        return
    feature = (
        CapabilityName.CALENDAR_OCCURRENCE
        if scope == "occurrence"
        else CapabilityName.CALENDAR_RECURRENCE
    )
    if caps.get(feature) is None:
        raise ChamberlainError(
            ErrorCode.CAPABILITY_UNSUPPORTED,
            "This calendar cannot write repeating events that way.",
            fix="Use a calendar that can, or write a single event.",
            detail=RECURRENCE_UNSUPPORTED,
        )


def _check_limits(ctx: Context, intent: WriteIntent, capability: Capability) -> None:
    """Connector limits are checked before `prepare` (P-2)."""
    limits = capability.limits
    most = limits.get("max_recipients")
    if most is not None and len(recipients_of(intent)) > most:
        raise ChamberlainError(ErrorCode.REQUEST_TOO_LARGE, "Too many recipients.")
    biggest = limits.get("max_attachment_bytes")
    for attachment in attachments_of(intent):
        info = ctx.services.artifacts.info(attachment.artifact)
        if not info.live:
            raise ChamberlainError(ErrorCode.ARTIFACT_EXPIRED, "An attachment has expired.")
        if info.sha256 != attachment.sha256:
            raise ChamberlainError(
                ErrorCode.ARTIFACT_HASH_MISMATCH, "An attachment does not match its hash."
            )
        if biggest is not None and info.size > biggest:
            raise ChamberlainError(ErrorCode.REQUEST_TOO_LARGE, "An attachment is too big.")
