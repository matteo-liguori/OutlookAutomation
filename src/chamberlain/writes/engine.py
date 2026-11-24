"""The write state machine (docs/contracts/04-writes.md, 05-grants.md; board R1 to R7).

Rule zero: no write without verifiable authority, and no blind resend.

* `propose` stores a complete intent and says what authority it needs.
* `do` reserves a grant use (authorise, in one transaction), prepares,
  journals `dispatched` (committed before the connector is called) and
  dispatches exactly once. Nothing ever dispatches an operation twice.
* An uncertain operation leaves that state only through read-only
  `reconcile` or the user's resolution in the viewer.
* A dispatch holds a lease (R5). Recovery touches only expired leases, and a
  late `applied` from a process that lost its lease is recorded as reconcile
  evidence.
"""

import json
import logging
import os
import secrets
import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Final

from pydantic import TypeAdapter

from chamberlain.app.context import Context
from chamberlain.domain.calendar import CalendarEvent
from chamberlain.domain.canonical import sha256_bytes, sha256_text
from chamberlain.domain.capabilities import Capability, CapabilityName
from chamberlain.domain.grant_rules import (
    format_ts,
    intent_hash,
    scope_mismatch,
)
from chamberlain.domain.grants import (
    ConfirmedVia,
    GrantKind,
    GrantState,
    Requirement,
    StandingScope,
)
from chamberlain.domain.handles import HandleKind, new_handle
from chamberlain.domain.intents import (
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
    WriteIntent,
)
from chamberlain.domain.operations import (
    MAY_HAVE_HAPPENED_STATES,
    TERMINAL_STATES,
    DispatchOutcome,
    DispatchResult,
    OperationState,
    Prepared,
    ProviderReceipt,
    ReconcileResult,
    ReconcileStatus,
    Verification,
    recheck_text,
)
from chamberlain.domain.permissions import recipients_of
from chamberlain.domain.recurrence import describe
from chamberlain.domain.senders import parse_instruction_ref
from chamberlain.limits import (
    ABSENCE_MARGIN_SECONDS,
    CONNECTOR_DEADLINE_SECONDS,
    REPEAT_WINDOW_HOURS,
    SETTLE_AFTER_MINUTES,
    UNCERTAIN_ABANDON_DAYS,
    UNCERTAIN_SEND_RECHECK_HOURS,
)
from chamberlain.ports.clock import ManualClock
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.schema.safelog import log_failure
from chamberlain.schema.tools import OperationView
from chamberlain.store.db import transaction
from chamberlain.store.migrate import stamp
from chamberlain.writes import repo
from chamberlain.writes.authorise import (
    Allow,
    Decision,
    Deny,
    OperationFacts,
    Usage,
    authorise,
    requirement_needed,
)
from chamberlain.writes.control import cancel_operation
from chamberlain.writes.grants import GrantService
from chamberlain.writes.imported import advice_for, parse_summary, repeats, summary_of_intent
from chamberlain.writes.imported_match import found_live
from chamberlain.writes.models import GrantRow, OperationRow, StoredReceipt, WritePolicy
from chamberlain.writes.recheck import look_in_recipient_mailboxes, sender_places
from chamberlain.writes.render import (
    EVENT_ATTENDEES_REF,
    EVENT_ORGANISER_REF,
    EVENT_REPEATS_REF,
    EVENT_WHEN_REF,
    describe_intent,
    describe_scope,
    grant_summary,
    grant_title,
)
from chamberlain.writes.transitions import build_receipt, move_operation

log = logging.getLogger("chamberlain.writes")

_RESERVABLE_STATES: Final = frozenset(
    {OperationState.PROPOSED, OperationState.AUTHORISED, OperationState.PREPARED}
)
"""The only states `do` may reserve from; later ones belong to the dispatching worker."""
_SENT_STATES: Final = frozenset(
    {
        OperationState.DISPATCHED,
        OperationState.UNCERTAIN,
        OperationState.RECONCILING,
        OperationState.CONFIRMED,
    }
)
"""States from `dispatched` onward, where nothing may be declared "failed clean" unseen."""

LEASE_SECONDS: Final = 300
"""Longer than a connector deadline plus the immediate-settle budget (R5, R6)."""
SETTLE_INTERVAL_SECONDS: Final = 10
MAIL_SETTLE_ATTEMPTS: Final = 3
MAIL_SETTLE_WINDOW_SECONDS: Final = 30
CALENDAR_SETTLE_ATTEMPTS: Final = 7
CALENDAR_SETTLE_WINDOW_SECONDS: Final = 60
ALLOW_DUPLICATE_REF: Final = "_allow_duplicate"
"""Reserved key in the `refs` mapping given to `prepare` (board R3)."""
INVITATIONS_UNSUPPORTED: Final = "invitations-not-supported"
RECURRENCE_UNSUPPORTED: Final = "recurrence-not-supported"
RECURRING_INVITATIONS_UNSUPPORTED: Final = "recurring-invitations-not-supported"

_INTENT: TypeAdapter[WriteIntent] = TypeAdapter(WriteIntent)
_CAPABILITY: dict[IntentKind, CapabilityName] = {
    IntentKind.MAIL_SEND: CapabilityName.MAIL_SEND,
    IntentKind.MAIL_DRAFT_SAVE: CapabilityName.MAIL_DRAFT,
    IntentKind.MAIL_DRAFT_UPDATE: CapabilityName.MAIL_DRAFT,
    IntentKind.MAIL_DRAFT_DISCARD: CapabilityName.MAIL_DRAFT,
    IntentKind.MAIL_MOVE: CapabilityName.MAIL_MOVE,
    IntentKind.MAIL_TRASH: CapabilityName.MAIL_TRASH,
    IntentKind.MAIL_MARK_READ: CapabilityName.MAIL_MARK_READ,
    IntentKind.CALENDAR_CREATE: CapabilityName.CALENDAR_WRITE,
    IntentKind.CALENDAR_UPDATE: CapabilityName.CALENDAR_WRITE,
    IntentKind.CALENDAR_DELETE: CapabilityName.CALENDAR_WRITE,
    IntentKind.CALENDAR_RESPOND: CapabilityName.CALENDAR_WRITE,
}
_CALENDAR_KINDS = frozenset(
    {
        IntentKind.CALENDAR_CREATE,
        IntentKind.CALENDAR_UPDATE,
        IntentKind.CALENDAR_DELETE,
        IntentKind.CALENDAR_RESPOND,
    }
)
_CALENDAR_INTENTS = (CalendarCreate, CalendarUpdate, CalendarDelete, CalendarRespond)
_PROCESS_TOKEN: list[str] = []


def process_token() -> str:
    """Identifies this process for dispatch leases: pid plus a start-time nonce (R5)."""
    if not _PROCESS_TOKEN:
        _PROCESS_TOKEN.append(f"{os.getpid()}-{secrets.token_hex(4)}")
    return _PROCESS_TOKEN[0]


def clock_pause(ctx: Context) -> Callable[[float], None]:
    """Wait on the clock: a manual clock is advanced, a real one is slept on (R6)."""
    clock = ctx.clock

    def pause(seconds: float) -> None:
        if isinstance(clock, ManualClock):
            clock.advance(seconds=seconds)
        else:
            time.sleep(seconds)

    return pause


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


@dataclass(frozen=True)
class Reading:
    """A connector's reconcile answer, reduced to what the engine acts on (R2, P-12)."""

    present: ProviderReceipt | None
    absent_proof: bool
    queued: bool
    absent: bool = False
    """The connector looked and found nothing (with or without proof)."""


def read_reconcile(result: ReconcileResult) -> Reading:
    """The one place a reconcile status is interpreted. `queued` never frees a grant."""
    status = result.status.value
    if status == "present" and result.receipt is not None:
        return Reading(result.receipt, False, False)
    return Reading(
        None, status == "absent" and result.proof, status == "queued", status == "absent"
    )


class Engine:
    """The write engine for one context. Cheap to build; holds no state beyond its worker id."""

    def __init__(
        self,
        ctx: Context,
        policy: WritePolicy | None = None,
        *,
        pause: Callable[[float], None] | None = None,
        worker: str | None = None,
    ) -> None:
        self.ctx = ctx
        self.db: sqlite3.Connection = ctx.db
        self.policy = policy or WritePolicy()
        self.pause = pause or clock_pause(ctx)
        self.worker = worker or process_token()
        self.grants = GrantService(ctx.db, ctx.clock, ctx.services.audit, self.policy)
        self._audit = ctx.services.audit

    # --- small helpers ------------------------------------------------------------

    def actor(self) -> str:
        return f"agent:{self.ctx.session}" if self.ctx.session else "agent:none"

    def _now(self) -> str:
        return stamp(self.ctx.clock)

    def get(self, handle: str) -> OperationRow:
        op = repo.get_operation(self.db, handle)
        if op is None:
            raise ChamberlainError(ErrorCode.REQUEST_UNKNOWN_HANDLE, "There is no such operation.")
        return op

    @staticmethod
    def intent_of(op: OperationRow) -> WriteIntent:
        if op.intent_json is None:
            raise ChamberlainError(
                ErrorCode.OPERATION_ALREADY_SETTLED, "That operation has been compacted."
            )
        return _INTENT.validate_json(op.intent_json)

    def _refs(self, op: OperationRow) -> dict[str, str]:
        loaded: dict[str, str] = json.loads(op.refs_json or "{}")
        return loaded

    def _capability(self, intent: WriteIntent) -> Capability:
        kind = IntentKind(intent.kind)
        if kind in _CALENDAR_KINDS:
            port_caps = self.ctx.sources.calendar_port(intent.source).capabilities(intent.source)
        else:
            port_caps = self.ctx.sources.mail_port(intent.source).capabilities(intent.source)
        capability = port_caps.get(_CAPABILITY[kind])
        if capability is None:
            raise ChamberlainError(
                ErrorCode.CAPABILITY_UNSUPPORTED, "This source cannot do that write."
            )
        return capability

    # --- propose ------------------------------------------------------------------

    def propose_intent(
        self,
        intent: WriteIntent,
        *,
        card: str | None = None,
        evidence: tuple[str, ...] = (),
    ) -> Proposal:
        """Store a complete intent and report the authority it needs (W-1, W-18, W-19)."""
        capability = self._capability(intent)
        refs = self._locate(intent)
        self._require_invitations(intent, refs)
        self._require_recurrence(intent)
        completed = self._complete(intent, refs)
        self._check_limits(completed, capability)
        sha = intent_hash(completed)
        existing = self._existing(completed, sha, card)
        if existing is not None:
            self._note_redo(existing)
            return self._proposal_for(existing, completed)
        self._note_repeat_of_imported(completed)
        handle = new_handle(HandleKind.OPERATION)
        decision = self._decide(completed, handle, sha, card, reserved=None, refs=refs)
        if isinstance(completed, MailSend):
            self._scan_before_storing(completed, handle, refs, decision)
        with transaction(self.db):
            repo.insert_operation(
                self.db,
                handle=handle,
                idempotency_key=sha256_text("proposed:" + handle),
                kind=IntentKind(completed.kind),
                source_id=completed.source,
                card=card,
                session=self.ctx.session,
                intent_sha256=sha,
                intent_json=completed.model_dump_json(exclude_none=True),
                refs_json=json.dumps(refs, sort_keys=True),
                at=stamp(self.ctx.clock),
            )
            self._audit.append(
                actor=self.actor(),
                kind="operation.proposed",
                subject=handle,
                summary=f"{completed.kind} proposed on {completed.source}.",
                payload_sha256=sha,
            )
            grant: GrantRow | None = None
            if isinstance(decision, Allow):
                grant = repo.get_grant(self.db, decision.grant)
                requirement = Requirement.NONE
            else:
                requirement = decision.requirement
                grant = repo.proposed_one_off(self.db, completed.source, sha)
                if grant is None:
                    grant = self.grants.propose_one_off(
                        source_id=completed.source,
                        intent_sha256=sha,
                        title=grant_title(completed),
                        summary=grant_summary(completed),
                        requirement=requirement,
                        card=card,
                        actor=self.actor(),
                    )
        for attachment in getattr(completed, "attachments", ()):
            self.ctx.services.artifacts.add_owner(attachment.artifact, handle)
        for item in evidence:
            self.ctx.services.evidence.link(item, handle)
        op = self.get(handle)
        text, cut = describe_intent(completed, refs=refs)
        return Proposal(
            operation=op,
            grant=grant,
            state=op.state,
            requirement=requirement,
            ready=isinstance(decision, Allow),
            content_sha256=sha,
            preview=text,
            truncated=cut,
            origin=handle,
        )

    def _scan_before_storing(
        self, intent: MailSend, handle: str, refs: Mapping[str, str], decision: Decision
    ) -> None:
        """Run the connector's read-only `prepare` so its duplicate scan fails early (R3).

        Nothing is stored yet, so a conflict leaves no operation behind. The token is a
        throwaway: the real one is derived from the idempotency key when `do` reserves.
        """
        scan_refs = dict(refs)
        scan, _ = _with_token(intent, sha256_text("scan:" + handle))
        if isinstance(decision, Allow) and decision.allow_duplicate:
            scan_refs[ALLOW_DUPLICATE_REF] = "1"
        try:
            self._port_prepare(scan, scan_refs)
        except ChamberlainError:
            raise
        except Exception as error:
            log_failure(log, "prepare failed while proposing a send")
            raise ChamberlainError(
                ErrorCode.SOURCE_UNAVAILABLE, "The source could not check the send."
            ) from error

    def propose_standing(self, scope: StandingScope, *, card: str | None = None) -> Proposal:
        """Ask for a standing grant. Only the viewer can activate it (G-12, G-14)."""
        for kind in scope.intent_kinds:
            capability = _CAPABILITY[kind]
            port_caps = (
                self.ctx.sources.calendar_port(scope.source).capabilities(scope.source)
                if kind in _CALENDAR_KINDS
                else self.ctx.sources.mail_port(scope.source).capabilities(scope.source)
            )
            if port_caps.get(capability) is None or (
                kind is IntentKind.CALENDAR_RESPOND
                and port_caps.get(CapabilityName.CALENDAR_INVITE) is None
            ):
                raise ChamberlainError(
                    ErrorCode.CAPABILITY_UNSUPPORTED, "This source cannot do that write."
                )
        grant = self.grants.propose_standing(scope, card=card, supersedes=None, actor=self.actor())
        return Proposal(
            operation=None,
            grant=grant,
            state=grant.state,
            requirement=Requirement.HUMAN_CONFIRMATION,
            ready=False,
            content_sha256=grant.content_sha256,
            preview=describe_scope(scope),
            truncated=False,
            origin=grant.handle,
        )

    def attach_approval(self, operation: str, evidence: str) -> Proposal:
        """Attach the user's in-chat approval to an earlier proposal (G-13, G-14)."""
        op = self.get(operation)
        intent = self.intent_of(op)
        if op.state is not OperationState.PROPOSED:
            return self._proposal_for(op, intent)
        grant = repo.proposed_one_off(self.db, op.source_id, op.intent_sha256)
        if grant is not None:
            needed = requirement_needed(intent, self._usage(intent, op.handle, None), self.policy)
            if grant.requirement is Requirement.HUMAN_CONFIRMATION or (
                needed is Requirement.HUMAN_CONFIRMATION
            ):
                raise ChamberlainError(ErrorCode.GRANT_HUMAN_CONFIRMATION_REQUIRED)
            self._check_evidence(evidence, intent)
            activated = self.grants.activate(
                grant.handle, via=ConfirmedVia.AGENT_EVIDENCE, actor=self.actor()
            )
            self.ctx.services.evidence.link(evidence, activated.handle)
        return self._proposal_for(self.get(operation), intent)

    def _check_evidence(self, evidence: str, intent: WriteIntent) -> None:
        """The approval is the user's own words from this session, or an authenticated email
        from an authorised sender recorded by `keep` (G-13, G-13a). Other remote text is refused."""
        facts = repo.evidence_facts(self.db, evidence)
        session = self.ctx.session
        recorded_in_session = session is not None and self.ctx.services.evidence.created_in_session(
            evidence, session
        )
        ref = None if facts is None else facts[1]
        instruction = parse_instruction_ref(ref)
        if not recorded_in_session or (ref is not None and instruction is None):
            raise ChamberlainError(
                ErrorCode.REQUEST_INVALID,
                "Approval must be evidence recording the user's own words in this session.",
                field="approval_evidence",
            )
        if instruction is not None and not instruction.covers(intent.kind.value):
            raise ChamberlainError(
                ErrorCode.REQUEST_INVALID,
                "That sender's email does not cover this kind of change.",
                fix="Ask the user in chat.",
                field="approval_evidence",
            )

    def _proposal_for(self, op: OperationRow, intent: WriteIntent) -> Proposal:
        """The current picture for an operation that already exists."""
        grant = repo.proposed_one_off(self.db, op.source_id, op.intent_sha256)
        ready = False
        requirement = Requirement.NONE
        if op.state in (
            OperationState.PROPOSED,
            OperationState.AUTHORISED,
            OperationState.PREPARED,
        ):
            original = self._original_of(op)
            if op.redo_of is not None and original is None:
                # The send it would replace has settled: nothing can authorise this any more.
                decision: Decision = Deny(ErrorCode.OPERATION_ALREADY_SETTLED, Requirement.NONE)
            else:
                decision = self._decide(
                    intent, op.handle, op.intent_sha256, op.card, op.grant, redo=original
                )
            if isinstance(decision, Allow):
                ready = True
                grant = repo.get_grant(self.db, decision.grant)
            else:
                requirement = decision.requirement
        text, cut = describe_intent(intent, refs=self._refs(op))
        return Proposal(
            operation=op,
            grant=grant,
            state=op.state,
            requirement=requirement,
            ready=ready,
            content_sha256=op.intent_sha256,
            preview=text,
            truncated=cut,
            origin=op.handle,
        )

    def _note_redo(self, op: OperationRow) -> None:
        """Something asked to do an unsettled write again: attention may speak about it again."""
        if op.state in MAY_HAVE_HAPPENED_STATES:
            repo.mark_redo(self.db, op.handle, stamp(self.ctx.clock))

    def _note_repeat_of_imported(self, intent: WriteIntent) -> None:
        """A new write that looks like an uncertain imported one marks that one as redone."""
        new = summary_of_intent(intent)
        if new is None:
            return
        for op in repo.operations_in_states(self.db, (OperationState.UNCERTAIN,)):
            old = parse_summary(op.summary_json)
            if (
                op.intent_json is None
                and op.source_id == intent.source
                and op.kind.value == intent.kind
                and old is not None
                and repeats(old, new)
            ):
                self._note_redo(op)

    def _existing(self, intent: WriteIntent, sha: str, card: str | None) -> OperationRow | None:
        """An identical live operation, or a recent one that may not repeat (W-18, W-19)."""
        window = self.ctx.clock.now() - timedelta(hours=REPEAT_WINDOW_HOURS)
        repeat_ok = self._repeat_allowed(intent, card)
        found = [
            op
            for op in repo.live_by_hash(self.db, intent.source, sha)
            if op.state not in TERMINAL_STATES or (op.created_at >= window and not repeat_ok)
        ]
        return found[-1] if found else None

    def _repeat_allowed(self, intent: WriteIntent, card: str | None) -> bool:
        for grant in repo.grants_in_states(self.db, (GrantState.ACTIVE,), intent.source):
            scope = grant.scope
            if scope is not None and scope.allow_repeat and not scope_mismatch(scope, intent, card):
                return True
        return False

    def _locate(self, intent: WriteIntent) -> dict[str, str]:
        """Stable locators for every handle the intent names (D-5, P-9)."""
        handles: list[str] = []
        for name in ("reply_to", "message", "draft", "event_ref"):
            value = getattr(intent, name, None)
            if isinstance(value, str):
                handles.append(value)
        handles.extend(getattr(intent, "messages", ()))
        located: dict[str, str] = {}
        for handle in handles:
            source, locator = self.ctx.services.refs.locate(handle)
            if source != intent.source:
                raise ChamberlainError(
                    ErrorCode.REQUEST_INVALID, "A handle belongs to a different source."
                )
            located[handle] = locator
        if isinstance(intent, CalendarUpdate | CalendarDelete | CalendarRespond):
            port = self.ctx.sources.calendar_port(intent.source)
            event = port.read(intent.source, located[intent.event_ref])
            if isinstance(intent, CalendarUpdate | CalendarDelete):
                self._check_scope(intent, event, located)
            if isinstance(intent, CalendarRespond):
                if event.organiser is None or event.my_response is None:
                    raise ChamberlainError(
                        ErrorCode.REQUEST_INVALID,
                        "That event is not an invitation to this calendar's owner.",
                        fix="Only an event someone else invited the owner to can be answered.",
                        field="event_ref",
                    )
                located[EVENT_ORGANISER_REF] = event.organiser
            else:
                if event.my_response is not None:
                    raise ChamberlainError(
                        ErrorCode.CAPABILITY_UNSUPPORTED,
                        "This is someone else's invitation; only its organiser can change it.",
                        fix="Answer it with calendar.respond instead.",
                        detail="invitee-copy",
                    )
                if event.fields.attendees:
                    located[EVENT_ATTENDEES_REF] = ",".join(sorted(event.fields.attendees))
        return located

    @staticmethod
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
        if event.fields.attendees or (
            isinstance(intent, CalendarUpdate) and intent.event.attendees
        ):
            raise ChamberlainError(
                ErrorCode.CAPABILITY_UNSUPPORTED,
                "A repeating event with guests cannot be changed here.",
                detail=RECURRING_INVITATIONS_UNSUPPORTED,
            )
        if scope == "occurrence":
            located[EVENT_WHEN_REF] = event.fields.start.isoformat()
        elif event.fields.recurrence is not None:
            located[EVENT_REPEATS_REF] = describe(event.fields.recurrence)

    def _require_recurrence(self, intent: WriteIntent) -> None:
        """A write that repeats needs a source that says it can; checked at propose (P-26)."""
        if not isinstance(intent, _CALENDAR_INTENTS) or isinstance(intent, CalendarRespond):
            return
        needed: CapabilityName | None = None
        if isinstance(intent, CalendarCreate):
            if intent.event.recurrence is not None:
                needed = CapabilityName.CALENDAR_RECURRENCE
                if intent.event.attendees:
                    raise ChamberlainError(
                        ErrorCode.CAPABILITY_UNSUPPORTED,
                        "A repeating event with guests cannot be created here.",
                        fix="Create it without guests.",
                        detail=RECURRING_INVITATIONS_UNSUPPORTED,
                    )
        elif intent.scope == "occurrence":
            needed = CapabilityName.CALENDAR_OCCURRENCE
        elif intent.scope == "series":
            needed = CapabilityName.CALENDAR_RECURRENCE
        if needed is None:
            return
        caps = self.ctx.sources.calendar_port(intent.source).capabilities(intent.source)
        if caps.get(needed) is None:
            raise ChamberlainError(
                ErrorCode.CAPABILITY_UNSUPPORTED,
                "This calendar cannot write repeating events that way.",
                fix="Use a calendar that can, or write a single event.",
                detail=RECURRENCE_UNSUPPORTED,
            )

    def _require_invitations(self, intent: WriteIntent, refs: Mapping[str, str]) -> None:
        """A write that reaches other people needs a source that can send invitations (P-2).

        Checked at propose so the owner is never asked to confirm what cannot happen.
        """
        if not isinstance(intent, _CALENDAR_INTENTS):
            return
        reaches = (
            isinstance(intent, CalendarRespond)
            or EVENT_ATTENDEES_REF in refs
            or (
                isinstance(intent, CalendarCreate | CalendarUpdate) and bool(intent.event.attendees)
            )
        )
        if not reaches:
            return
        caps = self.ctx.sources.calendar_port(intent.source).capabilities(intent.source)
        if caps.get(CapabilityName.CALENDAR_INVITE) is None:
            raise ChamberlainError(
                ErrorCode.CAPABILITY_UNSUPPORTED,
                "This calendar cannot send or answer invitations.",
                fix="Use a calendar that can, or write the event without other people.",
                detail=INVITATIONS_UNSUPPORTED,
            )

    def _complete(self, intent: WriteIntent, refs: Mapping[str, str]) -> WriteIntent:
        """Fill the harness-completed fields and drop agent-supplied tokens (W-1, R1)."""
        blank: dict[str, object] = {}
        for name in ("message_id", "uid"):
            if name in type(intent).model_fields:
                blank[name] = None
        if isinstance(intent, MailMove | MailTrash):
            seen = self.ctx.sources.mail_port(intent.source).resolve(
                intent.source, refs[intent.message]
            )
            blank["internet_message_id"] = seen.internet_message_id
            blank["from_folder"] = seen.folder_id
        if isinstance(intent, CalendarRespond):
            blank["organiser"] = refs[EVENT_ORGANISER_REF]
        if isinstance(intent, CalendarCreate | CalendarUpdate):
            rule = intent.event.recurrence
            if rule is not None and rule.zone is None:
                # Nine o'clock stays nine o'clock across a clock change (P-23).
                blank["event"] = intent.event.model_copy(
                    update={"recurrence": rule.model_copy(update={"zone": self.policy.zone})}
                )
        completed = intent.model_copy(update=blank)
        for attachment in getattr(completed, "attachments", ()):
            info = self.ctx.services.artifacts.info(attachment.artifact)
            if not info.live:
                raise ChamberlainError(ErrorCode.ARTIFACT_EXPIRED, "An attachment has expired.")
            if info.sha256 != attachment.sha256:
                raise ChamberlainError(
                    ErrorCode.ARTIFACT_HASH_MISMATCH, "An attachment does not match its hash."
                )
        return completed

    def _check_limits(self, intent: WriteIntent, capability: Capability) -> None:
        """Connector limits are checked before `prepare` (P-2)."""
        limits = capability.limits
        most = limits.get("max_recipients")
        if most is not None and len(recipients_of(intent)) > most:
            raise ChamberlainError(ErrorCode.REQUEST_TOO_LARGE, "Too many recipients.")
        biggest = limits.get("max_attachment_bytes")
        if biggest is not None:
            for attachment in getattr(intent, "attachments", ()):
                if self.ctx.services.artifacts.info(attachment.artifact).size > biggest:
                    raise ChamberlainError(ErrorCode.REQUEST_TOO_LARGE, "An attachment is too big.")

    # --- authorisation ------------------------------------------------------------

    def _usage(
        self,
        intent: WriteIntent,
        handle: str,
        reserved: str | None,
        refs: Mapping[str, str] | None = None,
        redo: OperationRow | None = None,
    ) -> Usage:
        now = self.ctx.clock.now()
        if refs is None:
            stored = repo.get_operation(self.db, handle)
            refs = {} if stored is None else self._refs(stored)
        grants = self._grants_for(intent.source, reserved, redo)
        names = [g.handle for g in grants]
        skip: str | tuple[str, ...] = handle if redo is None else (handle, redo.handle)
        return Usage(
            live_uses=repo.live_uses(self.db, names, skip),
            recent_uses=repo.recent_uses(
                self.db, names, format_ts(now - timedelta(hours=24)), skip
            ),
            source_sends_last_hour=repo.sends_since(
                self.db, intent.source, format_ts(now - timedelta(hours=1)), handle
            ),
            known_recipients=repo.known_recipients(self.db, intent.source),
            event_attendees=_attendees_in(refs),
            owner_addresses=repo.owner_addresses(self.db),
        )

    def _active_grants(self, source: str, reserved: str | None) -> list[GrantRow]:
        grants = repo.grants_in_states(self.db, (GrantState.ACTIVE,), source)
        if reserved is not None and all(g.handle != reserved for g in grants):
            held = repo.get_grant(self.db, reserved)
            if held is not None:
                grants.append(held)
        return grants

    def _grants_for(
        self, source: str, reserved: str | None, redo: OperationRow | None
    ) -> list[GrantRow]:
        """The grants that may authorise: for a resend, only the original send's grant (W-38).

        A one-off grant that timed out while its send was uncertain still stands for that one
        message, because the owner approved exactly this text and it has not been spent.
        """
        if redo is None:
            return self._active_grants(source, reserved)
        held = None if redo.grant is None else repo.get_grant(self.db, redo.grant)
        if held is None:
            return []
        if held.kind is GrantKind.ONE_OFF and held.state is GrantState.EXPIRED:
            held = replace(
                held,
                state=GrantState.ACTIVE,
                expires_at=self.ctx.clock.now() + timedelta(minutes=1),
            )
        return [held]

    def _decide(
        self,
        intent: WriteIntent,
        handle: str,
        sha: str,
        card: str | None,
        reserved: str | None,
        refs: Mapping[str, str] | None = None,
        redo: OperationRow | None = None,
    ) -> Decision:
        pinned = reserved if redo is None else redo.grant
        return authorise(
            intent,
            OperationFacts(handle, sha, card, pinned),
            self._grants_for(intent.source, reserved, redo),
            self._usage(intent, handle, reserved, refs, redo),
            self.policy,
            self.ctx.clock.now(),
        )

    # --- do -----------------------------------------------------------------------

    def do(self, handle: str) -> OperationRow:
        """Execute an authorised operation once (W-7, W-8, W-16, W-17)."""
        op = self.get(handle)
        if op.state in TERMINAL_STATES:
            return self._terminal_reply(op)
        if op.state in MAY_HAVE_HAPPENED_STATES:
            self.recover()
            op = self.get(handle)
            if op.state in TERMINAL_STATES:
                return self._terminal_reply(op)
            if op.state in MAY_HAVE_HAPPENED_STATES:
                raise self.needs_reconcile(handle)
        intent = self.intent_of(op)
        if op.redo_of is not None and op.state is OperationState.PROPOSED:
            self._guard_resend(op.redo_of)
        op = self._reserve(op, intent)
        op = self._prepare(op)
        final = self._dispatch(op)
        if final.state is OperationState.CONFIRMED:
            return final
        if final.state in TERMINAL_STATES:
            return self._terminal_reply(final)
        raise self.uncertain(final.handle)

    @staticmethod
    def needs_reconcile(handle: str) -> ChamberlainError:
        return ChamberlainError(
            ErrorCode.OPERATION_NEEDS_RECONCILE,
            f"Operation {handle} may have happened. Call reconcile; never send again.",
        )

    @staticmethod
    def uncertain(handle: str) -> ChamberlainError:
        return ChamberlainError(
            ErrorCode.OPERATION_UNCERTAIN,
            f"Operation {handle} may have happened and could not be confirmed yet.",
        )

    def _terminal_reply(self, op: OperationRow) -> OperationRow:
        """The stored outcome, with the same `ok` as the first time (W-16)."""
        if op.state is OperationState.CONFIRMED:
            return op
        if op.state is OperationState.FAILED_CLEAN:
            try:
                code = ErrorCode(op.error_code or "")
            except ValueError:
                code = ErrorCode.SOURCE_UNAVAILABLE
            raise ChamberlainError(
                code,
                f"Operation {op.handle} changed nothing. Propose the write again if still wanted.",
                fix="Nothing was changed. Propose the write again if it is still wanted.",
                retryable=False,
            )
        raise ChamberlainError(ErrorCode.OPERATION_ALREADY_SETTLED)

    def _reserve(self, op: OperationRow, intent: WriteIntent) -> OperationRow:
        """Authorise and reserve the grant use in one transaction (W-21)."""
        denied: ChamberlainError | None = None
        reserved: OperationRow | None = None
        with transaction(self.db):
            current = self.get(op.handle)
            if current.state not in _RESERVABLE_STATES:
                # A racing `do` may have moved it on: never touch it from here (W-17).
                if current.state in MAY_HAVE_HAPPENED_STATES:
                    raise self.needs_reconcile(current.handle)
                raise ChamberlainError(
                    ErrorCode.OPERATION_ALREADY_SETTLED, "That operation changed."
                )
            original = self._original_of(current)
            if current.redo_of is not None and current.state is OperationState.PROPOSED:
                if original is None:
                    raise ChamberlainError(
                        ErrorCode.OPERATION_ALREADY_SETTLED,
                        "The send this one replaces has settled. Nothing needs sending again.",
                    )
            else:
                original = None
            decision = self._decide(
                intent,
                current.handle,
                current.intent_sha256,
                current.card,
                current.grant,
                redo=original,
            )
            if isinstance(decision, Deny):
                denied = ChamberlainError(decision.code)
                if current.state is not OperationState.PROPOSED:
                    self._fail_clean(current, decision.code)
            elif current.state is OperationState.PROPOSED:
                attempt = repo.attempts_under(
                    self.db, decision.grant, current.intent_sha256, current.handle
                )
                seed = f"{decision.grant}:{current.intent_sha256}"
                key = sha256_text(seed if attempt == 0 else f"{seed}:{attempt}")
                completed, token = _with_token(intent, key)
                if original is not None:
                    self._supersede(original, current.handle)
                reserved = move_operation(
                    self.db,
                    self.ctx.clock,
                    self._audit,
                    current,
                    OperationState.AUTHORISED,
                    actor=self.actor(),
                    summary="Authorised; grant use reserved.",
                    grant=decision.grant,
                    idempotency_key=key,
                    reconcile_token=token,
                    intent_json=completed.model_dump_json(exclude_none=True),
                )
            else:
                reserved = current
        if denied is not None:
            raise denied
        if reserved is None:
            raise ChamberlainError(ErrorCode.OPERATION_ALREADY_SETTLED, "That operation changed.")
        if reserved.redo_of is not None:
            replaced = repo.get_operation(self.db, reserved.redo_of)
            if replaced is not None:
                self._release_attachments(replaced)
        return reserved

    def _original_of(self, op: OperationRow) -> OperationRow | None:
        """The still-uncertain send that a resend replaces; None once it has settled."""
        if op.redo_of is None:
            return None
        original = repo.get_operation(self.db, op.redo_of)
        if original is None or original.state is not OperationState.UNCERTAIN:
            return None
        return original

    def _supersede(self, original: OperationRow, resend: str) -> None:
        """The send ends `superseded`; its grant use passes to the resend (W-38)."""
        moved = move_operation(
            self.db,
            self.ctx.clock,
            self._audit,
            original,
            OperationState.SUPERSEDED,
            actor=self.actor(),
            summary=f"Sent again as {resend}; the grant use passed to it.",
            receipt_json=build_receipt(original, OperationState.SUPERSEDED, self._now()),
        )
        if moved is None:
            raise ChamberlainError(
                ErrorCode.OPERATION_ALREADY_SETTLED, "The send this one replaces just changed."
            )

    def _fail_clean(
        self, op: OperationRow, code: ErrorCode, *, expected_lease: str | bool | None = False
    ) -> OperationRow | None:
        """Proven nothing happened: release the grant use (W-10, W-11).

        From `dispatched` onward only the worker holding the lease can prove that, so
        a caller without `expected_lease` is refused.
        """
        if op.state in _SENT_STATES and not isinstance(expected_lease, str):
            raise ChamberlainError(
                ErrorCode.INTERNAL_UNEXPECTED,
                f"Operation {op.handle} may have happened and cannot be failed clean.",
            )
        row = move_operation(
            self.db,
            self.ctx.clock,
            self._audit,
            op,
            OperationState.FAILED_CLEAN,
            actor=self.actor(),
            summary=f"Nothing happened ({code.value}); grant use released.",
            expected_lease=expected_lease,
            receipt_json=build_receipt(op, OperationState.FAILED_CLEAN, self._now()),
            error_code=code.value,
            lease_owner=None,
            lease_expires_at=None,
        )
        if row is not None:
            self._release_attachments(row)
        return row

    def _release_attachments(self, op: OperationRow) -> None:
        if op.intent_json is None:
            return
        for attachment in getattr(self.intent_of(op), "attachments", ()):
            try:
                self.ctx.services.artifacts.release_owner(attachment.artifact, op.handle)
            except ChamberlainError:
                log.warning("could not release an attachment hold for %s", op.handle)

    def _prepare(self, op: OperationRow) -> OperationRow:
        """Read-only. A failure cannot have changed anything and frees the grant (W-11)."""
        if op.state is OperationState.PREPARED:
            return op
        intent = self.intent_of(op)
        refs = self._refs(op)
        grant = repo.get_grant(self.db, op.grant) if op.grant else None
        if grant is not None and grant.scope is not None and grant.scope.allow_duplicate:
            refs[ALLOW_DUPLICATE_REF] = "1"
        try:
            self._read_attachments(intent)
            prepared = self._port_prepare(intent, refs)
        except ChamberlainError as error:
            self._fail_clean(op, error.code)
            raise
        except Exception as error:
            log_failure(log, "prepare failed for %s", op.handle)
            self._fail_clean(op, ErrorCode.SOURCE_UNAVAILABLE)
            raise ChamberlainError(
                ErrorCode.SOURCE_UNAVAILABLE, "The source could not prepare the write."
            ) from error
        moved = move_operation(
            self.db,
            self.ctx.clock,
            self._audit,
            op,
            OperationState.PREPARED,
            actor=self.actor(),
            summary="Prepared by a read-only connector call.",
            prepared_json=prepared.model_dump_json(exclude_none=True),
        )
        if moved is None:
            raise ChamberlainError(ErrorCode.OPERATION_ALREADY_SETTLED, "That operation changed.")
        return moved

    def _read_attachments(self, intent: WriteIntent) -> dict[str, bytes]:
        """Attachment bytes from the artifact store, matched by hash (W-3)."""
        payloads: dict[str, bytes] = {}
        for attachment in getattr(intent, "attachments", ()):
            data = self.ctx.services.artifacts.read_bytes(attachment.artifact)
            if sha256_bytes(data) != attachment.sha256:
                raise ChamberlainError(
                    ErrorCode.ARTIFACT_HASH_MISMATCH, "An attachment does not match its hash."
                )
            payloads[attachment.sha256] = data
        return payloads

    def _dispatch(self, op: OperationRow) -> OperationRow:
        """Journal `dispatched`, commit, then make the one mutation call (W-7, W-8)."""
        intent = self.intent_of(op)
        if op.prepared_json is None:
            raise ChamberlainError(ErrorCode.INTERNAL_UNEXPECTED, "A prepared write is missing.")
        prepared = Prepared.model_validate_json(op.prepared_json)
        try:
            payloads = self._read_attachments(intent)
        except ChamberlainError as error:
            self._fail_clean(op, error.code)
            raise
        now = self.ctx.clock.now()
        journal = move_operation(
            self.db,
            self.ctx.clock,
            self._audit,
            op,
            OperationState.DISPATCHED,
            actor=self.actor(),
            summary="Journalled before the one mutation call.",
            lease_owner=self.worker,
            lease_expires_at=format_ts(now + timedelta(seconds=LEASE_SECONDS)),
            dispatched_at=format_ts(now),
            worker=self.worker,
        )
        if journal is None:
            raise ChamberlainError(ErrorCode.OPERATION_ALREADY_SETTLED, "That operation changed.")
        try:
            result = self._port_dispatch(intent, prepared, journal.idempotency_key, payloads)
        except Exception:
            log_failure(log, "dispatch raised for %s", journal.handle)
            result = DispatchResult(outcome=DispatchOutcome.UNKNOWN, detail="exception")
        return self._settle_dispatch(journal, prepared, result)

    def _settle_dispatch(
        self, journal: OperationRow, prepared: Prepared, result: DispatchResult
    ) -> OperationRow:
        """Map the connector's outcome to a state (W-10)."""
        if result.outcome is DispatchOutcome.APPLIED and result.receipt is not None:
            row = self._confirm(
                journal,
                result.receipt,
                expected_lease=self.worker,
                actor=self.actor(),
                summary="Dispatch applied.",
            )
            if row is None:
                return self._late_applied(journal, result.receipt)
            self._upgrade_verification(row, prepared)
            return self.get(row.handle)
        if result.outcome is DispatchOutcome.NOT_APPLIED:
            row = self._fail_clean(journal, ErrorCode.SOURCE_BUSY, expected_lease=self.worker)
            return row or self.get(journal.handle)
        moved = move_operation(
            self.db,
            self.ctx.clock,
            self._audit,
            journal,
            OperationState.UNCERTAIN,
            actor=self.actor(),
            summary="Outcome unknown; it may have happened.",
            expected_lease=self.worker,
            lease_owner=None,
            lease_expires_at=None,
        )
        if moved is None:
            return self.get(journal.handle)
        return self._reconcile(moved, window=self._window(moved), allow_absent=False)

    def _late_applied(self, journal: OperationRow, receipt: ProviderReceipt) -> OperationRow:
        """An `applied` that arrives after another process took over (R5).

        It is evidence, never a second dispatch: `uncertain -> reconciling ->
        confirmed`, through the reconcile path.
        """
        current = self.get(journal.handle)
        if current.state is not OperationState.UNCERTAIN:
            return current
        with transaction(self.db):
            held = self._enter_reconciling(current, summary="Late result recorded as evidence.")
            if held is None:
                return self.get(journal.handle)
            self._confirm(
                held,
                receipt,
                expected_lease=self.worker,
                actor="system:tick",
                summary="A late applied result was recorded as reconcile evidence.",
            )
        return self.get(journal.handle)

    # --- confirming ---------------------------------------------------------------

    def _confirm(
        self,
        op: OperationRow,
        receipt: ProviderReceipt,
        *,
        expected_lease: str | bool | None,
        actor: str,
        summary: str,
    ) -> OperationRow | None:
        intent = self.intent_of(op)
        result_ref = self._result_ref(op, intent, receipt)
        verification = receipt.verification
        with transaction(self.db):
            moved = move_operation(
                self.db,
                self.ctx.clock,
                self._audit,
                op,
                OperationState.CONFIRMED,
                actor=actor,
                summary=summary
                + (" Warnings: " + ", ".join(receipt.warnings) if receipt.warnings else ""),
                expected_lease=expected_lease,
                receipt_json=build_receipt(
                    op,
                    OperationState.CONFIRMED,
                    self._now(),
                    verification=verification,
                    items=receipt.items,
                    result_ref=result_ref,
                    warnings=receipt.warnings,
                ),
                client_verified=int(verification.client_verified),
                server_verified=int(verification.server_verified),
                lease_owner=None,
                lease_expires_at=None,
            )
            if moved is None:
                return None
            if moved.grant is not None:
                self.grants.mark_spent(moved.grant, actor=actor)
        self._after_confirmed(moved, intent, receipt)
        return moved

    def _result_ref(
        self, op: OperationRow, intent: WriteIntent, receipt: ProviderReceipt
    ) -> str | None:
        """The handle of what the write produced (a saved draft, a created event)."""
        if receipt.locator is None:
            return None
        refs = self.ctx.services.refs
        if isinstance(intent, MailDraftSave):
            return refs.remember(HandleKind.MESSAGE, op.source_id, receipt.locator)
        if isinstance(intent, CalendarCreate):
            return refs.remember(HandleKind.CALENDAR_EVENT, op.source_id, receipt.locator)
        return None

    def _after_confirmed(
        self, op: OperationRow, intent: WriteIntent, receipt: ProviderReceipt
    ) -> None:
        """Keep handles alive after filing (R4) and release the attachment holds."""
        old: str | None = None
        if isinstance(intent, MailMove | MailTrash):
            old = self._refs(op).get(intent.message)
        elif isinstance(intent, MailDraftUpdate):
            old = self._refs(op).get(intent.draft)
        if old is not None and receipt.locator is not None and receipt.locator != old:
            try:
                self.ctx.services.refs.repoint(old, receipt.locator, source=op.source_id)
            except Exception:
                log_failure(log, "could not repoint the handle of %s", op.handle)
        self._release_attachments(op)

    def _upgrade_verification(self, op: OperationRow, prepared: Prepared) -> None:
        """Try to upgrade a client-verified result by reading back (W-14, R6).

        Never changes the state; failure is silent.
        """
        if op.server_verified or op.intent_json is None:
            return
        intent = self.intent_of(op)
        try:
            reading = self._reconcile_loop(op, intent, prepared, self._window(op))
        except Exception:
            log_failure(log, "verification upgrade failed for %s", op.handle)
            return
        found = reading.present
        if found is not None and found.verification.server_verified:
            with transaction(self.db):
                upgraded = repo.update_operation(
                    self.db,
                    op.handle,
                    expected_state=OperationState.CONFIRMED,
                    server_verified=1,
                    updated_at=self._now(),
                )
                if upgraded:
                    self._audit.append(
                        actor="system:tick",
                        kind="operation.verified",
                        subject=op.handle,
                        summary="Read back from the server; verification upgraded.",
                        payload_sha256=op.intent_sha256,
                    )

    # --- reconcile ----------------------------------------------------------------

    @staticmethod
    def _window(op: OperationRow) -> tuple[int, int]:
        if op.kind in _CALENDAR_KINDS:
            return CALENDAR_SETTLE_ATTEMPTS, CALENDAR_SETTLE_WINDOW_SECONDS
        return MAIL_SETTLE_ATTEMPTS, MAIL_SETTLE_WINDOW_SECONDS

    def _enter_reconciling(
        self, op: OperationRow, *, summary: str, actor: str | None = None
    ) -> OperationRow | None:
        return move_operation(
            self.db,
            self.ctx.clock,
            self._audit,
            op,
            OperationState.RECONCILING,
            actor=actor or self.actor(),
            summary=summary,
            lease_owner=self.worker,
            lease_expires_at=format_ts(self.ctx.clock.now() + timedelta(seconds=LEASE_SECONDS)),
        )

    def _reconcile_loop(
        self, op: OperationRow, intent: WriteIntent, prepared: Prepared, window: tuple[int, int]
    ) -> Reading:
        """Bounded, read-only reconciles (R6). Stops at the first `present`."""
        attempts, seconds = window
        started = self.ctx.clock.now()
        reading = Reading(None, False, False)
        for index in range(attempts):
            reading = read_reconcile(self._port_reconcile(intent, prepared, op.idempotency_key))
            if reading.present is not None:
                return reading
            elapsed = (self.ctx.clock.now() - started).total_seconds()
            if index + 1 >= attempts or elapsed + SETTLE_INTERVAL_SECONDS > seconds:
                break
            self.pause(SETTLE_INTERVAL_SECONDS)
        return reading

    def _reconcile(
        self, op: OperationRow, *, window: tuple[int, int], allow_absent: bool
    ) -> OperationRow:
        """`uncertain -> reconciling -> confirmed | confirmed_absent | uncertain` (W-8, W-12)."""
        return self._reconcile_reading(op, window=window, allow_absent=allow_absent)[0]

    def _reconcile_reading(
        self,
        op: OperationRow,
        *,
        window: tuple[int, int],
        allow_absent: bool,
        actor: str | None = None,
    ) -> tuple[OperationRow, Reading | None]:
        """`_reconcile`, and what the connector said (None when no read was made)."""
        who = actor or self.actor()
        intent = self.intent_of(op)
        if op.prepared_json is None:
            return op, None
        prepared = Prepared.model_validate_json(op.prepared_json)
        held = self._enter_reconciling(op, summary="Read-only reconcile started.", actor=who)
        if held is None:
            return self.get(op.handle), None
        reading = self._reconcile_loop(held, intent, prepared, window)
        if reading.present is not None:
            confirmed = self._confirm(
                held,
                reading.present,
                expected_lease=self.worker,
                actor=who,
                summary="Reconcile found the write.",
            )
            return (confirmed or self.get(op.handle)), reading
        if allow_absent and reading.absent_proof and self._absence_proven(held, intent):
            absent = move_operation(
                self.db,
                self.ctx.clock,
                self._audit,
                held,
                OperationState.CONFIRMED_ABSENT,
                actor=who,
                summary="Reconcile proved the write did not happen; grant use released.",
                expected_lease=self.worker,
                receipt_json=build_receipt(held, OperationState.CONFIRMED_ABSENT, self._now()),
                lease_owner=None,
                lease_expires_at=None,
            )
            if absent is not None:
                self._release_attachments(absent)
                return absent, reading
            return self.get(op.handle), reading
        back = move_operation(
            self.db,
            self.ctx.clock,
            self._audit,
            held,
            OperationState.UNCERTAIN,
            actor=who,
            summary="Still uncertain after a read-only reconcile."
            + (" An exact copy is queued." if reading.queued else ""),
            expected_lease=self.worker,
            lease_owner=None,
            lease_expires_at=None,
        )
        return (back or self.get(op.handle)), reading

    def _absence_proven(self, op: OperationRow, intent: WriteIntent) -> bool:
        """`absent` frees the grant only with proof, capability and settled time (W-12)."""
        if op.dispatched_at is None:
            return False
        wait = max(
            timedelta(minutes=SETTLE_AFTER_MINUTES),
            timedelta(seconds=CONNECTOR_DEADLINE_SECONDS + ABSENCE_MARGIN_SECONDS),
        )
        settled = op.dispatched_at + wait
        if self.ctx.clock.now() < settled:
            return False
        try:
            capability = self._capability(intent)
        except ChamberlainError:
            return False
        return op.kind.value in capability.absence_provable

    def reconcile(
        self, handle: str, *, happened_evidence: str | None = None
    ) -> tuple[OperationRow, bool]:
        """Settle an uncertain operation by reading, never by resending (T-13).

        `happened_evidence` is the agent's own record that the write did happen (the safe
        direction). That it did not happen is never settled here without a connector's proof.
        """
        self.recover()
        before = self.get(handle)
        if before.state is not OperationState.UNCERTAIN:
            return before, False
        if before.intent_json is None:
            return self._reconcile_imported(before, happened_evidence)
        if happened_evidence is not None:
            return self._settle_happened(
                before, happened_evidence, "The agent recorded evidence that it happened."
            )
        if before.rechecked_at is not None:
            after, _ = self._look_for_send(
                before, actor=self.actor(), record=True, allow_absent=True
            )
            return after, after.state is not before.state
        after = self._reconcile(before, window=(1, 0), allow_absent=True)
        return after, after.state is not before.state

    def _reconcile_imported(
        self, op: OperationRow, evidence: str | None
    ) -> tuple[OperationRow, bool]:
        """An old journal's operation: found on the live source settles it, nothing else does.

        It has no intent, so nothing here can send it. Not found is not proof of absence.
        """
        if evidence is not None:
            return self._settle_happened(
                op, evidence, "The agent recorded evidence that it happened."
            )
        summary = parse_summary(op.summary_json)
        if summary is not None and found_live(self.ctx, op, summary):
            return self._settle_happened(op, None, "Reconcile found the item on the live source.")
        raise ChamberlainError(
            ErrorCode.OPERATION_UNCERTAIN,
            "This write came from the old harness and could not be found on the live source. "
            f"It will never be retried. {advice_for(op.kind)} If you can see it, call reconcile "
            "again with happened and your evidence; otherwise leave it: the harness closes it, "
            "and the owner can also resolve it in the viewer.",
        )

    def _settle_happened(
        self, op: OperationRow, evidence: str | None, summary: str
    ) -> tuple[OperationRow, bool]:
        if evidence is not None:
            self._check_seen(evidence)
        with transaction(self.db):
            moved = move_operation(
                self.db,
                self.ctx.clock,
                self._audit,
                op,
                OperationState.CONFIRMED,
                actor=self.actor(),
                summary=summary,
                receipt_json=build_receipt(op, OperationState.CONFIRMED, self._now()),
            )
            if moved is None:
                raise ChamberlainError(ErrorCode.OPERATION_ALREADY_SETTLED, "That changed.")
            if evidence is not None:
                self.ctx.services.evidence.link(evidence, moved.handle)
            if moved.grant is not None:
                self.grants.mark_spent(moved.grant, actor=self.actor())
        self._release_attachments(moved)
        return moved, True

    def abandon(self, op: OperationRow) -> OperationRow | None:
        """Close an uncertain operation nothing can resend. It frees no grant and claims nothing."""
        moved = move_operation(
            self.db,
            self.ctx.clock,
            self._audit,
            op,
            OperationState.ABANDONED,
            actor="system:tick",
            summary="Closed unsettled after "
            f"{UNCERTAIN_ABANDON_DAYS} days; nothing can send it again; no grant was freed.",
            receipt_json=build_receipt(op, OperationState.ABANDONED, self._now()),
        )
        if moved is not None:
            self._release_attachments(moved)
        return moved

    # --- the 24-hour look for an uncertain send, and the resend (W-37 to W-41) -------------

    def recheck_sends(self) -> int:
        """The tick's step: each uncertain send old enough gets one more look. Never raises."""
        cutoff = self.ctx.clock.now() - timedelta(hours=UNCERTAIN_SEND_RECHECK_HOURS)
        looked = 0
        for op in repo.sends_due_recheck(self.db, format_ts(cutoff)):
            try:
                _, outcome = self._look_for_send(op, actor="system:tick", record=True)
            except Exception:
                log_failure(log, "re-check failed for %s", op.handle)
                continue
            looked += 1 if outcome is not None else 0
        return looked

    def _look_for_send(
        self, op: OperationRow, *, actor: str, record: bool, allow_absent: bool = False
    ) -> tuple[OperationRow, str | None]:
        """Read Sent, Outbox and Drafts (the connector's reconcile), then any recipient Inbox.

        Returns the operation and `found` (settled confirmed), `queued`, `missing`, or None when
        nothing could be concluded (the read failed, or the operation is no longer uncertain).
        With `record`, a `queued` or `missing` answer is stored on the operation and audited.
        """
        before = self.get(op.handle)
        if before.state is not OperationState.UNCERTAIN or before.intent_json is None:
            return before, None
        intent = self.intent_of(before)
        row, reading = self._reconcile_reading(
            before, window=(1, 0), allow_absent=allow_absent, actor=actor
        )
        if row.state is OperationState.CONFIRMED:
            return row, "found"
        if (
            row.state is not OperationState.UNCERTAIN
            or reading is None
            or not isinstance(intent, MailSend)
        ):
            return row, None
        looked = [sender_places(row.source_id)]
        if reading.queued:
            outcome = "queued"
        elif reading.absent:
            nearby = look_in_recipient_mailboxes(self.ctx, row, intent)
            if nearby.receipt is not None:
                done = self._confirm(
                    row,
                    nearby.receipt,
                    expected_lease=False,
                    actor=actor,
                    summary="The re-check found it in a recipient's own mailbox.",
                )
                return (done or self.get(row.handle)), "found"
            looked.extend(nearby.looked)
            outcome = "missing"
        else:
            return row, None
        if record:
            self._record_recheck(row, outcome, looked, actor)
        return row, outcome

    def _record_recheck(
        self, op: OperationRow, outcome: str, looked: list[str], actor: str
    ) -> None:
        age = self.ctx.clock.now() - (op.dispatched_at or op.created_at)
        with transaction(self.db):
            stored = repo.record_recheck(
                self.db,
                op.handle,
                result=outcome,
                looked_json=json.dumps({"looked": looked}),
                at=self._now(),
            )
            if stored:
                self._audit.append(
                    actor=actor,
                    kind="operation.rechecked",
                    subject=op.handle,
                    summary=f"Looked again after {int(age.total_seconds() // 3600)} hours: "
                    f"{outcome}. Looked in {'; '.join(looked)}.",
                    payload_sha256=op.intent_sha256,
                )

    def propose_redo(
        self,
        original: str,
        *,
        card: str | None = None,
        evidence: tuple[str, ...] = (),
    ) -> Proposal:
        """Send an uncertain, missing send again, on its original authority (W-38).

        Only after the harness's 24-hour look found it missing. The text is the original's, never
        the agent's. The original ends `superseded` only when `do` runs the resend.
        """
        old = self.get(original)
        intent = self._resendable(old)
        existing = repo.resend_of(self.db, old.handle)
        if existing is not None:
            return self._proposal_for(existing, intent)
        self._guard_resend(old.handle)
        old = self.get(old.handle)
        for attachment in getattr(intent, "attachments", ()):
            if not self.ctx.services.artifacts.info(attachment.artifact).live:
                raise ChamberlainError(ErrorCode.ARTIFACT_EXPIRED, "An attachment has expired.")
        completed = intent.model_copy(update={"message_id": None})
        sha = intent_hash(completed)
        refs = self._refs(old)
        handle = new_handle(HandleKind.OPERATION)
        decision = self._decide(completed, handle, sha, card or old.card, None, refs, redo=old)
        if isinstance(decision, Deny):
            raise ChamberlainError(
                decision.code,
                "The permission behind the first send no longer covers sending it again.",
                fix="Ask the owner, in chat, whether to send it again.",
            )
        self._scan_before_storing(completed, handle, refs, decision)
        with transaction(self.db):
            repo.insert_operation(
                self.db,
                handle=handle,
                idempotency_key=sha256_text("proposed:" + handle),
                kind=IntentKind(completed.kind),
                source_id=completed.source,
                card=card or old.card,
                session=self.ctx.session,
                intent_sha256=sha,
                intent_json=completed.model_dump_json(exclude_none=True),
                refs_json=json.dumps(refs, sort_keys=True),
                at=self._now(),
                redo_of=old.handle,
            )
            self._audit.append(
                actor=self.actor(),
                kind="operation.proposed",
                subject=handle,
                summary=f"{completed.kind} proposed on {completed.source} as a resend of "
                f"{old.handle}.",
                payload_sha256=sha,
            )
        for attachment in getattr(completed, "attachments", ()):
            self.ctx.services.artifacts.add_owner(attachment.artifact, handle)
        for item in evidence:
            self.ctx.services.evidence.link(item, handle)
        return self._proposal_for(self.get(handle), completed)

    def _resendable(self, old: OperationRow) -> MailSend:
        """Refuse, with a plain reason, anything that is not an uncertain send awaiting a resend."""
        if old.kind is not IntentKind.MAIL_SEND or old.intent_json is None:
            raise ChamberlainError(
                ErrorCode.REQUEST_WRONG_KIND,
                "Only a mail send the harness holds the text of can be sent again.",
                field="redo_of",
            )
        intent = self.intent_of(old)
        assert isinstance(intent, MailSend)
        if repo.resend_of(self.db, old.handle) is not None:
            return intent
        if old.redo_of is not None:
            raise ChamberlainError(
                ErrorCode.REQUEST_INVALID,
                "That send was already a resend. A second resend needs the owner's word.",
                fix="Ask the owner, in chat, whether to send it again.",
                field="redo_of",
            )
        self.recover()
        old = self.get(old.handle)
        if old.state is not OperationState.UNCERTAIN:
            raise ChamberlainError(
                ErrorCode.OPERATION_ALREADY_SETTLED,
                f"That send is {old.state.value}; there is nothing to send again.",
            )
        if old.rechecked_at is None:
            raise ChamberlainError(
                ErrorCode.REQUEST_INVALID,
                "Too early. A send is not sent again before the harness has looked for it "
                f"after {UNCERTAIN_SEND_RECHECK_HOURS} hours.",
                fix="Wait for the report that it is missing. Until then do not send it again.",
                field="redo_of",
            )
        return intent

    def _guard_resend(self, original: str) -> None:
        """One last read-only look just before a resend. Anything but `missing` stops it."""
        old = self.get(original)
        if old.state is not OperationState.UNCERTAIN:
            raise ChamberlainError(
                ErrorCode.OPERATION_ALREADY_SETTLED,
                f"That send is {old.state.value}; there is nothing to send again.",
            )
        row, outcome = self._look_for_send(old, actor=self.actor(), record=True)
        if row.state is OperationState.CONFIRMED:
            raise ChamberlainError(
                ErrorCode.OPERATION_ALREADY_SETTLED,
                "It turned up, so it was sent. Nothing needs sending again.",
            )
        if outcome == "queued":
            raise ChamberlainError(
                ErrorCode.REQUEST_INVALID,
                "It is waiting in the mail client's Outbox. Sending again would make two.",
                fix="Tell the owner it is stuck in the Outbox; do not send it again.",
                field="redo_of",
            )
        if outcome != "missing":
            raise ChamberlainError(
                ErrorCode.SOURCE_UNAVAILABLE,
                "The mailbox could not be read just now, so it is not known to be missing.",
            )

    def _check_seen(self, evidence: str) -> None:
        """Evidence that it happened is something this session recorded. It frees nothing."""
        session = self.ctx.session
        if session is None or not self.ctx.services.evidence.created_in_session(evidence, session):
            raise ChamberlainError(
                ErrorCode.REQUEST_INVALID,
                "Give evidence recorded in this session of what you saw.",
                field="evidence",
            )

    def recover(self) -> int:
        """Move operations whose lease has expired to `uncertain` (W-9, R5)."""
        now = self.ctx.clock.now()
        moved = 0
        for op in repo.operations_in_states(
            self.db, (OperationState.DISPATCHED, OperationState.RECONCILING)
        ):
            if op.lease_expires_at is not None and op.lease_expires_at > now:
                continue
            row = move_operation(
                self.db,
                self.ctx.clock,
                self._audit,
                op,
                OperationState.UNCERTAIN,
                actor="system:tick",
                summary="Lease expired; treated as may have happened.",
                expected_lease=op.lease_owner,
                lease_owner=None,
                lease_expires_at=None,
            )
            moved += 1 if row is not None else 0
        return moved

    def resolve(self, handle: str, *, happened: bool, actor: str) -> OperationRow:
        """The user's resolution of an uncertain operation in the viewer (W-13)."""
        self.recover()
        op = self.get(handle)
        if op.state is not OperationState.UNCERTAIN:
            if op.state in TERMINAL_STATES:
                raise ChamberlainError(ErrorCode.OPERATION_ALREADY_SETTLED)
            raise ChamberlainError(ErrorCode.REQUEST_WRONG_KIND, "That operation is not uncertain.")
        target = OperationState.CONFIRMED if happened else OperationState.CONFIRMED_ABSENT
        with transaction(self.db):
            moved = move_operation(
                self.db,
                self.ctx.clock,
                self._audit,
                op,
                target,
                actor=actor,
                summary="The user resolved it: "
                + ("it happened." if happened else "it did not happen."),
                receipt_json=build_receipt(op, target, self._now()),
            )
            if moved is None:
                raise ChamberlainError(ErrorCode.OPERATION_ALREADY_SETTLED, "That changed.")
            if happened and moved.grant is not None:
                self.grants.mark_spent(moved.grant, actor=actor)
        self._release_attachments(moved)
        return moved

    def cancel(self, handle: str, *, actor: str) -> OperationRow:
        """Withdraw an operation before it is prepared (T-11). Never touches dispatched ones."""
        return cancel_operation(
            self.db,
            self.ctx.clock,
            self._audit,
            self.grants,
            handle,
            actor=actor,
            release=self._release_attachments,
        )

    # --- views --------------------------------------------------------------------

    def view(self, op: OperationRow) -> OperationView:
        resent = repo.resend_of(self.db, op.handle)
        stored = StoredReceipt.model_validate_json(op.receipt_json) if op.receipt_json else None
        code: ErrorCode | None
        try:
            code = ErrorCode(op.error_code) if op.error_code else None
        except ValueError:
            code = None
        return OperationView(
            handle=op.handle,
            intent_kind=op.kind,
            source=op.source_id,
            state=op.state,
            grant=op.grant,
            verification=Verification(
                client_verified=op.client_verified, server_verified=op.server_verified
            ),
            may_have_happened=op.state in MAY_HAVE_HAPPENED_STATES,
            error_code=code,
            result_ref=None if stored is None else stored.result_ref,
            items_ok=0 if stored is None else stored.summary.items_ok,
            items_failed=0 if stored is None else stored.summary.items_failed,
            warnings=() if stored is None else stored.warnings,
            settled_at=op.settled_at,
            recheck=recheck_text(op.recheck_result, op.recheck_json),
            redo_of=op.redo_of,
            redone_by=None if resent is None else resent.handle,
        )

    # --- connector calls (the only ones) --------------------------------------------

    def _port_prepare(self, intent: WriteIntent, refs: Mapping[str, str]) -> Prepared:
        if isinstance(intent, _CALENDAR_INTENTS):
            return self.ctx.sources.calendar_port(intent.source).prepare(intent, refs)
        return self.ctx.sources.mail_port(intent.source).prepare(intent, refs)

    def _port_dispatch(
        self, intent: WriteIntent, prepared: Prepared, key: str, payloads: Mapping[str, bytes]
    ) -> DispatchResult:
        if isinstance(intent, _CALENDAR_INTENTS):
            return self.ctx.sources.calendar_port(intent.source).dispatch(prepared, key)
        return self.ctx.sources.mail_port(intent.source).dispatch(prepared, key, payloads)

    def _port_reconcile(self, intent: WriteIntent, prepared: Prepared, key: str) -> ReconcileResult:
        """Read-only. Any failure is `unknown`, never a reason to resend."""
        try:
            if isinstance(intent, _CALENDAR_INTENTS):
                return self.ctx.sources.calendar_port(intent.source).reconcile(prepared, key)
            return self.ctx.sources.mail_port(intent.source).reconcile(prepared, key)
        except Exception:
            log_failure(log, "reconcile failed")
            return ReconcileResult(status=ReconcileStatus.UNKNOWN)


def _attendees_in(refs: Mapping[str, str]) -> frozenset[str]:
    """The stored event's attendees noted at propose; empty when unknown (an older `1`)."""
    listed = refs.get(EVENT_ATTENDEES_REF, "")
    return frozenset(a for a in listed.split(",") if "@" in a)


def _with_token(intent: WriteIntent, key: str) -> tuple[WriteIntent, str | None]:
    """Complete the harness-assigned token, derived from the idempotency key (R1, Q4)."""
    if isinstance(intent, MailSend | MailDraftSave | MailDraftUpdate):
        token = f"<{key[:32]}@chamberlain.invalid>"
        return intent.model_copy(update={"message_id": token}), token
    if isinstance(intent, CalendarCreate):
        return intent.model_copy(update={"uid": key[:32]}), key[:32]
    return intent, None
