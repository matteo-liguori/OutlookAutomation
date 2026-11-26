"""Per-kind write inputs, provider calls and result handling. No state transitions."""

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

from chamberlain.app.context import Context
from chamberlain.domain.calendar import CalendarEvent
from chamberlain.domain.capabilities import Capability, CapabilityName
from chamberlain.domain.handles import HandleKind
from chamberlain.domain.intents import (
    AttachmentRef,
    CalendarCreate,
    CalendarDelete,
    CalendarRespond,
    CalendarUpdate,
    IntentKind,
    MailDraftSave,
    MailDraftUpdate,
    MailMove,
    MailSend,
    MailTrash,
    OutsideAction,
    WriteIntent,
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
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.schema.safelog import log_failure
from chamberlain.writes import read_state
from chamberlain.writes.models import OperationRow, WritePolicy
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
        return cls(
            refs=stable_after_metadata(stable),
            event_attendees=frozenset(a for a in guests.split(",") if a),
            organiser=refs.get(EVENT_ORGANISER_REF),
            when=refs.get(EVENT_WHEN_REF),
            repeats=refs.get(EVENT_REPEATS_REF),
        )

    def to_refs(self) -> dict[str, str]:
        """The render/storage adapter for the established operation row format."""
        facts = {
            EVENT_ATTENDEES_REF: ",".join(sorted(self.event_attendees)),
            EVENT_ORGANISER_REF: self.organiser,
            EVENT_WHEN_REF: self.when,
            EVENT_REPEATS_REF: self.repeats,
        }
        return {**self.refs, **{key: value for key, value in facts.items() if value}}


def stable_after_metadata(refs: dict[str, str]) -> dict[str, str]:
    for key in (EVENT_ORGANISER_REF, EVENT_WHEN_REF, EVENT_REPEATS_REF):
        refs.pop(key, None)
    return refs


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


def attachments_of(intent: WriteIntent) -> tuple[AttachmentRef, ...]:
    return (
        intent.attachments if isinstance(intent, MailSend | MailDraftSave | MailDraftUpdate) else ()
    )


def capability(ctx: Context, intent: WriteIntent) -> Capability:
    spec = KINDS[intent.kind]
    if spec.capability is None:
        raise _no_connector()
    port = (
        ctx.sources.calendar_port(intent.source)
        if spec.calendar
        else ctx.sources.mail_port(intent.source)
    )
    found = port.capabilities(intent.source).get(spec.capability)
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
    scope: dict[str, str] = {}
    _check_scope(intent, event, scope)
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
        when=scope.get(EVENT_WHEN_REF),
        repeats=scope.get(EVENT_REPEATS_REF),
    )


def complete(
    ctx: Context, intent: WriteIntent, located: Located, policy: WritePolicy
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
        completed["mark_read"] = _filing_marks_read(ctx, intent.source, policy)
    if isinstance(intent, CalendarRespond):
        completed["organiser"] = located.organiser
    if isinstance(intent, CalendarCreate | CalendarUpdate):
        rule = intent.event.recurrence
        if rule is not None and rule.zone is None:
            completed["event"] = intent.event.model_copy(
                update={"recurrence": rule.model_copy(update={"zone": policy.zone})}
            )
    for attachment in attachments_of(intent):
        info = ctx.services.artifacts.info(attachment.artifact)
        if not info.live:
            raise ChamberlainError(ErrorCode.ARTIFACT_EXPIRED, "An attachment has expired.")
        if info.sha256 != attachment.sha256:
            raise ChamberlainError(
                ErrorCode.ARTIFACT_HASH_MISMATCH, "An attachment does not match its hash."
            )
    return intent.model_copy(update=completed)


def _filing_marks_read(ctx: Context, source: str, policy: WritePolicy) -> bool:
    """The filing preference acts only through an active read-state grant (G-24)."""
    if not read_state.options(ctx.db).filing_marks_read:
        return False
    if read_state.active_grant(ctx.db, source) is not None:
        return True
    read_state.propose_missing(ctx.db, ctx.clock, ctx.services.audit, [source], policy=policy)
    return False


def prepare_intent(
    ctx: Context, intent: WriteIntent, policy: WritePolicy
) -> tuple[WriteIntent, Located]:
    cap = capability(ctx, intent)
    located = locate(ctx, intent)
    _require_invitations(ctx, intent, located)
    _require_recurrence(ctx, intent)
    completed = complete(ctx, intent, located, policy)
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
        if isinstance(intent, OutsideAction):
            raise _no_connector()
        port = (
            ctx.sources.calendar_port(intent.source)
            if KINDS[intent.kind].calendar
            else ctx.sources.mail_port(intent.source)
        )
        return port.reconcile(prepared, key)
    except Exception:
        log_failure(log, "reconcile failed")
        return ReconcileResult(status=ReconcileStatus.UNKNOWN)


def _no_connector() -> ChamberlainError:
    return ChamberlainError(ErrorCode.INTERNAL_UNEXPECTED, "An outside action has no connector.")


def _check_scope(
    intent: CalendarUpdate | CalendarDelete, event: CalendarEvent, located: dict[str, str]
) -> None:
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
        return
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
        located[EVENT_WHEN_REF] = event.fields.start.isoformat()
    elif event.fields.recurrence is not None:
        located[EVENT_REPEATS_REF] = describe(event.fields.recurrence)


def _require_recurrence(ctx: Context, intent: WriteIntent) -> None:
    """A write that repeats needs a source that says it can; checked at propose (P-26)."""
    if not isinstance(
        intent, (CalendarCreate, CalendarUpdate, CalendarDelete, CalendarRespond)
    ) or isinstance(intent, CalendarRespond):
        return
    needed: CapabilityName | None = None
    if isinstance(intent, CalendarCreate):
        if intent.event.recurrence is not None:
            needed = CapabilityName.CALENDAR_RECURRENCE
    elif intent.scope == "occurrence":
        needed = CapabilityName.CALENDAR_OCCURRENCE
    elif intent.scope == "series":
        needed = CapabilityName.CALENDAR_RECURRENCE
    if needed is None:
        return
    caps = ctx.sources.calendar_port(intent.source).capabilities(intent.source)
    if caps.get(needed) is None:
        raise ChamberlainError(
            ErrorCode.CAPABILITY_UNSUPPORTED,
            "This calendar cannot write repeating events that way.",
            fix="Use a calendar that can, or write a single event.",
            detail=RECURRENCE_UNSUPPORTED,
        )


def _require_invitations(ctx: Context, intent: WriteIntent, located: Located) -> None:
    """A write that reaches other people needs a source that can send invitations (P-2).

    Checked at propose so the owner is never asked to confirm what cannot happen.
    """
    if not isinstance(intent, (CalendarCreate, CalendarUpdate, CalendarDelete, CalendarRespond)):
        return
    reaches = (
        isinstance(intent, CalendarRespond)
        or bool(located.event_attendees)
        or (isinstance(intent, CalendarCreate | CalendarUpdate) and bool(intent.event.attendees))
    )
    if not reaches:
        return
    caps = ctx.sources.calendar_port(intent.source).capabilities(intent.source)
    if caps.get(CapabilityName.CALENDAR_INVITE) is None:
        raise ChamberlainError(
            ErrorCode.CAPABILITY_UNSUPPORTED,
            "This calendar cannot send or answer invitations.",
            fix="Use a calendar that can, or write the event without other people.",
            detail=INVITATIONS_UNSUPPORTED,
        )


def _check_limits(ctx: Context, intent: WriteIntent, capability: Capability) -> None:
    """Connector limits are checked before `prepare` (P-2)."""
    limits = capability.limits
    most = limits.get("max_recipients")
    if most is not None and len(recipients_of(intent)) > most:
        raise ChamberlainError(ErrorCode.REQUEST_TOO_LARGE, "Too many recipients.")
    biggest = limits.get("max_attachment_bytes")
    if biggest is not None:
        for attachment in attachments_of(intent):
            if ctx.services.artifacts.info(attachment.artifact).size > biggest:
                raise ChamberlainError(ErrorCode.REQUEST_TOO_LARGE, "An attachment is too big.")
