"""The write state machine (docs/contracts/04-writes.md, 05-grants.md; board R1 to R7).

Rule zero: no write without verifiable authority, and no blind resend.

* `propose` stores a complete intent and says what authority it needs.
* `do` reserves a grant use (authorise, in one transaction), prepares,
  journals `dispatched` (committed before the connector is called) and
  dispatches exactly once. Nothing ever dispatches an operation twice.
* An uncertain operation leaves that state only through read-only
  `reconcile`.
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
from dataclasses import replace
from datetime import timedelta
from typing import Final

from chamberlain.app.context import Context
from chamberlain.domain.actors import agent_actor
from chamberlain.domain.canonical import sha256_bytes, sha256_text
from chamberlain.domain.capabilities import CapabilityName
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
    IntentKind,
    MailMarkRead,
    MailMove,
    MailSend,
    MailTrash,
    OutsideAction,
    WriteIntent,
    attachments_of,
)
from chamberlain.domain.mail import bare
from chamberlain.domain.operations import (
    MAY_HAVE_HAPPENED_STATES,
    TERMINAL_STATES,
    DispatchOutcome,
    DispatchResult,
    OperationState,
    Prepared,
    ProviderReceipt,
    ReconcileResult,
    Verification,
    recheck_text,
)
from chamberlain.domain.senders import parse_instruction_ref
from chamberlain.limits import (
    ABSENCE_MARGIN_SECONDS,
    CONNECTOR_DEADLINE_SECONDS,
    REPEAT_WINDOW_HOURS,
    SETTLE_AFTER_MINUTES,
)
from chamberlain.ports.clock import ManualClock
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.schema.safelog import log_failure
from chamberlain.schema.tools import OperationView
from chamberlain.store.db import transaction
from chamberlain.store.migrate import stamp
from chamberlain.writes import kinds, outside, repo, resend
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
from chamberlain.writes.imported import advice_for, parse_summary
from chamberlain.writes.imported_match import found_live
from chamberlain.writes.models import (
    GrantRow,
    OperationRow,
    Proposal,
    Reading,
    WritePolicy,
)
from chamberlain.writes.render import (
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
ALLOW_DUPLICATE_REF = kinds.ALLOW_DUPLICATE_REF
"""Reserved key in the `refs` mapping given to `prepare` (board R3)."""
OUTSIDE_REPORT_HOURS: Final = 24
"""How long the assistant has to report an outside action it was allowed to do (W-46)."""
INVITATIONS_UNSUPPORTED: Final = "invitations-not-supported"
RECURRENCE_UNSUPPORTED: Final = "recurrence-not-supported"

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
        actor: str | None = None,
    ) -> None:
        self.ctx = ctx
        self.db: sqlite3.Connection = ctx.db
        self.policy = ctx.write_policy if policy is None else policy
        self._actor = actor
        self._only_grant: str | None = None
        self.pause = pause or clock_pause(ctx)
        self.worker = worker or process_token()
        self.grants = GrantService(ctx.db, ctx.clock, ctx.services.audit, self.policy)
        self.audit = ctx.services.audit

    # Both the compare-and-set and its audit row commit in move_operation (W-7).
    def transition(
        self,
        op: OperationRow,
        to: OperationState,
        *,
        summary: str,
        actor: str | None = None,
        expected_lease: str | bool | None = False,
        **columns: str | int | None,
    ) -> OperationRow | None:
        return move_operation(
            self.db,
            self.ctx.clock,
            self.audit,
            op,
            to,
            actor=self.actor() if actor is None else actor,
            summary=summary,
            expected_lease=expected_lease,
            **columns,
        )

    def close_unreported_outside(self) -> int:
        return outside.close_unreported_outside(self)

    def recheck_sends(self) -> int:
        return resend.recheck_sends(self)

    def abandon(self, op: OperationRow) -> OperationRow | None:
        return resend.abandon(self, op)

    def propose_redo(
        self,
        original: str,
        *,
        card: str | None = None,
        evidence: tuple[str, ...] = (),
    ) -> Proposal:
        return resend.propose_redo(self, original, card=card, evidence=evidence)

    # --- small helpers ------------------------------------------------------------

    def actor(self) -> str:
        return self._actor or agent_actor(self.ctx.session)

    def run(
        self, intent: WriteIntent, *, actor: str, grant: str, card: str | None = None
    ) -> OperationRow | None:
        """Execute an internal write using exactly the caller's selected grant and audit actor."""
        worker = Engine(self.ctx, self.policy, pause=self.pause, worker=self.worker, actor=actor)
        worker._only_grant = grant
        proposal = worker.propose_intent(intent, card=card)
        op = proposal.operation
        if not proposal.ready or op is None or op.card != card or op.grant not in (None, grant):
            return None
        return worker.do(op.handle)

    def now(self) -> str:
        return stamp(self.ctx.clock)

    def get(self, handle: str) -> OperationRow:
        op = repo.get_operation(self.db, handle)
        if op is None:
            raise ChamberlainError(ErrorCode.REQUEST_UNKNOWN_HANDLE, "There is no such operation.")
        return op

    @staticmethod
    def intent_of(op: OperationRow) -> WriteIntent:
        return op.intent()

    def located(self, op: OperationRow) -> kinds.Located:
        return kinds.Located.from_refs(op.refs())

    # --- propose ------------------------------------------------------------------

    def propose_intent(
        self,
        intent: WriteIntent,
        *,
        card: str | None = None,
        evidence: tuple[str, ...] = (),
    ) -> Proposal:
        """Store a complete intent and report the authority it needs (W-1, W-18, W-19)."""
        if isinstance(intent, OutsideAction):
            # No connector stands behind it: nothing to locate, complete or limit (W-44).
            outside.check_outside(self, intent, card)
            completed, located = intent, kinds.Located()
        else:
            completed, located = kinds.prepare_intent(self.ctx, intent, self.policy, card)
        refs = located.to_refs()
        sha = intent_hash(completed)
        existing = self._existing(completed, sha, card)
        if existing is not None:
            return self.proposal_for(existing, completed)
        handle = new_handle(HandleKind.OPERATION)
        decision = self.decide(completed, handle, sha, card, reserved=None, located=located)
        if self._only_grant is not None and isinstance(decision, Deny):
            return Proposal(
                operation=None,
                grant=None,
                state=OperationState.PROPOSED,
                requirement=decision.requirement,
                ready=False,
                content_sha256=sha,
                preview="",
                truncated=False,
                origin=handle,
            )
        if isinstance(completed, MailSend):
            self.scan_before_storing(completed, handle, refs, decision)
        with transaction(self.db):
            existing = self._existing(completed, sha, card)
            if existing is not None:
                return self.proposal_for(existing, completed)
            self.insert_proposal(completed, handle, sha, card, refs)
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
        self.hold_inputs(completed, handle, evidence)
        op = self.get(handle)
        text, cut = kinds.describe_write(completed, located)
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

    def insert_proposal(
        self,
        intent: WriteIntent,
        handle: str,
        sha: str,
        card: str | None,
        refs: Mapping[str, str],
        *,
        redo_of: str | None = None,
    ) -> None:
        """Insert and audit a proposal inside the workflow's transaction."""
        repo.insert_operation(
            self.db,
            handle=handle,
            idempotency_key=sha256_text("proposed:" + handle),
            kind=IntentKind(intent.kind),
            source_id=intent.source,
            card=card,
            session=self.ctx.session,
            intent_sha256=sha,
            intent_json=intent.model_dump_json(exclude_none=True),
            refs_json=json.dumps(dict(refs), sort_keys=True),
            at=self.now(),
            redo_of=redo_of,
        )
        suffix = f" as a resend of {redo_of}." if redo_of is not None else "."
        self.audit.append(
            actor=self.actor(),
            kind="operation.proposed",
            subject=handle,
            summary=f"{intent.kind} proposed on {intent.source}{suffix}",
            payload_sha256=sha,
        )

    def hold_inputs(self, intent: WriteIntent, handle: str, evidence: tuple[str, ...]) -> None:
        for attachment in attachments_of(intent):
            self.ctx.services.artifacts.add_owner(attachment.artifact, handle)
        for item in evidence:
            self.ctx.services.evidence.link(item, handle)

    def scan_before_storing(
        self, intent: MailSend, handle: str, refs: Mapping[str, str], decision: Decision
    ) -> None:
        """Run the connector's read-only `prepare` so its duplicate scan fails early (R3).

        Nothing is stored yet, so a conflict leaves no operation behind. The token is a
        throwaway: the real one is derived from the idempotency key when `do` reserves.
        """
        scan_refs = dict(refs)
        scan, _ = kinds.with_token(intent, sha256_text("scan:" + handle))
        if isinstance(decision, Allow) and decision.allow_duplicate:
            scan_refs[ALLOW_DUPLICATE_REF] = "1"
        try:
            kinds.port_prepare(self.ctx, scan, scan_refs)
        except ChamberlainError:
            raise
        except Exception as error:
            log_failure(log, "prepare failed while proposing a send")
            raise ChamberlainError(
                ErrorCode.SOURCE_UNAVAILABLE, "The source could not check the send."
            ) from error

    def propose_standing(self, scope: StandingScope, *, card: str | None = None) -> Proposal:
        """Ask for a standing grant. Only the viewer can activate it (G-12, G-14)."""
        if IntentKind.OUTSIDE_ACTION in scope.intent_kinds:
            if card is not None and card != scope.card:
                raise ChamberlainError(
                    ErrorCode.REQUEST_INVALID,
                    "A standing permission for outside actions belongs to one card.",
                    field="card",
                )
            if scope.card is not None and not repo.card_is_open(self.db, scope.card):
                raise ChamberlainError(
                    ErrorCode.REQUEST_INVALID,
                    "That card does not exist or is closed.",
                    fix="Bind it to an open card.",
                    field="standing",
                )
        else:
            for source in sorted(scope.all_sources()):
                for kind in scope.intent_kinds:
                    spec = kinds.KINDS[kind]
                    port_caps = (
                        self.ctx.sources.calendar_port(source).capabilities(source)
                        if spec.calendar
                        else self.ctx.sources.mail_port(source).capabilities(source)
                    )
                    if (
                        spec.capability is None
                        or port_caps.get(spec.capability) is None
                        or (
                            kind is IntentKind.CALENDAR_RESPOND
                            and port_caps.get(CapabilityName.CALENDAR_INVITE) is None
                        )
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
            return self.proposal_for(op, intent)
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
        return self.proposal_for(self.get(operation), intent)

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

    def proposal_for(self, op: OperationRow, intent: WriteIntent) -> Proposal:
        """The current picture for an operation that already exists."""
        grant = repo.proposed_one_off(self.db, op.source_id, op.intent_sha256)
        ready = False
        requirement = Requirement.NONE
        if op.state in (
            OperationState.PROPOSED,
            OperationState.AUTHORISED,
            OperationState.PREPARED,
        ):
            original = resend.original_of(self, op)
            if op.redo_of is not None and original is None:
                # The send it would replace has settled: nothing can authorise this any more.
                decision: Decision = Deny(ErrorCode.OPERATION_ALREADY_SETTLED, Requirement.NONE)
            else:
                decision = self.decide(
                    intent, op.handle, op.intent_sha256, op.card, op.grant, redo=original
                )
            if isinstance(decision, Allow):
                ready = True
                grant = repo.get_grant(self.db, decision.grant)
            else:
                requirement = decision.requirement
        text, cut = kinds.describe_write(intent, self.located(op))
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

    def _existing(self, intent: WriteIntent, sha: str, card: str | None) -> OperationRow | None:
        """An identical live operation, or a recent one that may not repeat (W-18, W-19)."""
        window = self.ctx.clock.now() - timedelta(hours=REPEAT_WINDOW_HOURS)
        # The same form may be filled again later: only a live outside action matches (W-44).
        repeat_ok = kinds.KINDS[intent.kind].terminal_repeat or self._repeat_allowed(intent, card)
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

    # --- authorisation ------------------------------------------------------------

    def _usage(
        self,
        intent: WriteIntent,
        handle: str,
        reserved: str | None,
        located: kinds.Located | None = None,
        redo: OperationRow | None = None,
    ) -> Usage:
        now = self.ctx.clock.now()
        if located is None:
            stored = repo.get_operation(self.db, handle)
            located = kinds.Located() if stored is None else self.located(stored)
        grants = self._grants_for(intent.source, reserved, redo)
        names = [g.handle for g in grants]
        skip: str | tuple[str, ...] = handle if redo is None else (handle, redo.handle)
        folders: frozenset[str] = frozenset()
        if isinstance(intent, MailMarkRead) and any(g.scope and g.scope.folders for g in grants):
            port = self.ctx.sources.mail_port(intent.source)
            folders = frozenset(
                port.folder_of(intent.source, located.refs[h]) for h in intent.messages
            )
        return Usage(
            mail_folders=folders,
            live_uses=repo.live_uses(self.db, names, skip),
            recent_uses=repo.recent_uses(
                self.db, names, format_ts(now - timedelta(hours=24)), skip
            ),
            source_sends_last_hour=repo.sends_since(
                self.db, intent.source, format_ts(now - timedelta(hours=1)), handle
            ),
            known_recipients=self._known_for(intent),
            event_attendees=located.event_attendees,
            owner_addresses=repo.owner_addresses(self.db),
        )

    def _known_for(self, intent: WriteIntent) -> frozenset[str]:
        """Mail: this mailbox's known recipients. A calendar invitation: those of any source."""
        known = repo.known_recipients(self.db, intent.source)
        if kinds.KINDS[intent.kind].calendar:
            known |= repo.known_recipients_anywhere(self.db)
        return known

    def _active_grants(self, source: str, reserved: str | None) -> list[GrantRow]:
        grants = repo.grants_in_states(self.db, (GrantState.ACTIVE,), source)
        if reserved is not None and all(g.handle != reserved for g in grants):
            held = repo.get_grant(self.db, reserved)
            if held is not None:
                grants.append(held)
        if self._only_grant is not None:
            return [grant for grant in grants if grant.handle == self._only_grant]
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

    def decide(
        self,
        intent: WriteIntent,
        handle: str,
        sha: str,
        card: str | None,
        reserved: str | None,
        located: kinds.Located | None = None,
        redo: OperationRow | None = None,
    ) -> Decision:
        pinned = reserved if redo is None else redo.grant
        return authorise(
            intent,
            OperationFacts(handle, sha, card, pinned),
            self._grants_for(intent.source, reserved, redo),
            self._usage(intent, handle, reserved, located, redo),
            self.policy,
            self.ctx.clock.now(),
        )

    # --- do -----------------------------------------------------------------------

    def do(self, handle: str, report: str | None = None) -> OperationRow:
        """Execute an authorised operation once (W-7, W-8, W-16, W-17).

        For an outside action the harness does nothing outside: the first call reserves the use,
        the second, with `report`, records what the assistant did (W-46)."""
        op = self.get(handle)
        if op.kind is IntentKind.OUTSIDE_ACTION:
            return outside.do_outside(self, op, report)
        if report is not None:
            raise ChamberlainError(
                ErrorCode.REQUEST_INVALID,
                "A report is only for an outside action.",
                fix="Leave report out.",
                field="report",
            )
        if op.state in TERMINAL_STATES:
            return self.terminal_reply(op)
        if op.state in MAY_HAVE_HAPPENED_STATES:
            self.recover()
            op = self.get(handle)
            if op.state in TERMINAL_STATES:
                return self.terminal_reply(op)
            if op.state in MAY_HAVE_HAPPENED_STATES:
                raise self.needs_reconcile(handle)
        intent = self.intent_of(op)
        if op.redo_of is not None and op.state is OperationState.PROPOSED:
            resend.guard_resend(self, op.redo_of)
        op = self.reserve(op, intent)
        op = self._prepare(op)
        final = self._dispatch(op)
        if final.state is OperationState.CONFIRMED:
            return final
        if final.state in TERMINAL_STATES:
            return self.terminal_reply(final)
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

    def terminal_reply(self, op: OperationRow) -> OperationRow:
        """The stored outcome, with the same `ok` as the first time (W-16)."""
        if op.state in (OperationState.CONFIRMED, OperationState.AGENT_REPORTED):
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

    def reserve(self, op: OperationRow, intent: WriteIntent) -> OperationRow:
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
            original = resend.original_of(self, current)
            if current.redo_of is not None and current.state is OperationState.PROPOSED:
                if original is None:
                    raise ChamberlainError(
                        ErrorCode.OPERATION_ALREADY_SETTLED,
                        "The send this one replaces has settled. Nothing needs sending again.",
                    )
            else:
                original = None
            decision = self.decide(
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
                completed, token = kinds.with_token(intent, key)
                if original is not None:
                    resend.supersede(self, original, current.handle)
                reserved = self.transition(
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
                self.release_attachments(replaced)
        return reserved

    def _fail_clean(
        self,
        op: OperationRow,
        code: ErrorCode,
        *,
        expected_lease: str | bool | None = False,
        detail: str = "",
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
        row = self.transition(
            op,
            OperationState.FAILED_CLEAN,
            actor=self.actor(),
            summary=(
                f"Nothing happened ({code.value}"
                + (f": {detail[:200]}" if detail else "")
                + "); grant use released."
            ),
            expected_lease=expected_lease,
            receipt_json=build_receipt(op, OperationState.FAILED_CLEAN, self.now()),
            error_code=code.value,
            lease_owner=None,
            lease_expires_at=None,
        )
        if row is not None:
            self.release_attachments(row)
        return row

    def release_attachments(self, op: OperationRow) -> None:
        if op.intent_json is None:
            return
        for attachment in attachments_of(self.intent_of(op)):
            try:
                self.ctx.services.artifacts.release_owner(attachment.artifact, op.handle)
            except ChamberlainError:
                log.warning("could not release an attachment hold for %s", op.handle)

    def _prepare(self, op: OperationRow) -> OperationRow:
        """Read-only. A failure cannot have changed anything and frees the grant (W-11)."""
        if op.state is OperationState.PREPARED:
            return op
        intent = self.intent_of(op)
        refs = self.located(op).to_refs()
        grant = repo.get_grant(self.db, op.grant) if op.grant else None
        if grant is not None and grant.scope is not None and grant.scope.allow_duplicate:
            refs[ALLOW_DUPLICATE_REF] = "1"
        try:
            original = refs.get("_original_move_intent")
            if original and isinstance(intent, MailMove | MailTrash):
                old = json.loads(original)
                if old.get("internet_message_id") is not None:
                    port = self.ctx.sources.mail_port(intent.source)
                    current = port.resolve(intent.source, refs[intent.messages[0]])
                    if bare(current.internet_message_id) != bare(old["internet_message_id"]):
                        raise ChamberlainError(
                            ErrorCode.REMOTE_CHANGED, "The message identity differs."
                        )
            self._read_attachments(intent)
            prepared = kinds.port_prepare(self.ctx, intent, refs)
        except ChamberlainError as error:
            self._fail_clean(op, error.code)
            raise
        except Exception as error:
            log_failure(log, "prepare failed for %s", op.handle)
            self._fail_clean(op, ErrorCode.SOURCE_UNAVAILABLE)
            raise ChamberlainError(
                ErrorCode.SOURCE_UNAVAILABLE, "The source could not prepare the write."
            ) from error
        moved = self.transition(
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
        for attachment in attachments_of(intent):
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
        journal = self.transition(
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
            result = kinds.port_dispatch(
                self.ctx, intent, prepared, journal.idempotency_key, payloads
            )
        except Exception:
            log_failure(log, "dispatch raised for %s", journal.handle)
            result = DispatchResult(outcome=DispatchOutcome.UNKNOWN, detail="exception")
        return self._settle_dispatch(journal, prepared, result)

    def _settle_dispatch(
        self, journal: OperationRow, prepared: Prepared, result: DispatchResult
    ) -> OperationRow:
        """Map the connector's outcome to a state (W-10)."""
        if result.outcome is DispatchOutcome.APPLIED and result.receipt is not None:
            row = self.confirm_receipt(
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
            row = self._fail_clean(
                journal, ErrorCode.SOURCE_BUSY, expected_lease=self.worker, detail=result.detail
            )
            return row or self.get(journal.handle)
        moved = self.transition(
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
            self.confirm_receipt(
                held,
                receipt,
                expected_lease=self.worker,
                actor="system:tick",
                summary="A late applied result was recorded as reconcile evidence.",
            )
        return self.get(journal.handle)

    # --- confirming ---------------------------------------------------------------

    def confirm_receipt(
        self,
        op: OperationRow,
        receipt: ProviderReceipt,
        *,
        expected_lease: str | bool | None,
        actor: str,
        summary: str,
    ) -> OperationRow | None:
        intent = self.intent_of(op)
        result_ref = kinds.result_ref(self.ctx, op, receipt)
        verification = receipt.verification
        with transaction(self.db):
            moved = self.transition(
                op,
                OperationState.CONFIRMED,
                actor=actor,
                summary=summary
                + (" Warnings: " + ", ".join(receipt.warnings) if receipt.warnings else ""),
                expected_lease=expected_lease,
                receipt_json=build_receipt(
                    op,
                    OperationState.CONFIRMED,
                    self.now(),
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
        kinds.after_confirmed(self.ctx, moved, intent, self.located(moved), receipt)
        self.release_attachments(moved)
        return moved

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
                    updated_at=self.now(),
                )
                if upgraded:
                    self.audit.append(
                        actor="system:tick",
                        kind="operation.verified",
                        subject=op.handle,
                        summary="Read back from the server; verification upgraded.",
                        payload_sha256=op.intent_sha256,
                    )

    # --- reconcile ----------------------------------------------------------------

    @staticmethod
    def _window(op: OperationRow) -> tuple[int, int]:
        if kinds.KINDS[op.kind].calendar:
            return CALENDAR_SETTLE_ATTEMPTS, CALENDAR_SETTLE_WINDOW_SECONDS
        return MAIL_SETTLE_ATTEMPTS, MAIL_SETTLE_WINDOW_SECONDS

    def _enter_reconciling(
        self, op: OperationRow, *, summary: str, actor: str | None = None
    ) -> OperationRow | None:
        return self.transition(
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
            reading = read_reconcile(
                kinds.port_reconcile(self.ctx, intent, prepared, op.idempotency_key)
            )
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
        return self.reconcile_reading(op, window=window, allow_absent=allow_absent)[0]

    def reconcile_reading(
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
            confirmed = self.confirm_receipt(
                held,
                reading.present,
                expected_lease=self.worker,
                actor=who,
                summary="Reconcile found the write.",
            )
            return (confirmed or self.get(op.handle)), reading
        if allow_absent and reading.absent_proof and self._absence_proven(held, intent):
            absent = self.transition(
                held,
                OperationState.CONFIRMED_ABSENT,
                actor=who,
                summary="Reconcile proved the write did not happen; grant use released.",
                expected_lease=self.worker,
                receipt_json=build_receipt(held, OperationState.CONFIRMED_ABSENT, self.now()),
                lease_owner=None,
                lease_expires_at=None,
            )
            if absent is not None:
                self.release_attachments(absent)
                return absent, reading
            return self.get(op.handle), reading
        back = self.transition(
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
            capability = kinds.capability(self.ctx, intent)
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
            after, _ = resend.look_for_send(
                self, before, actor=self.actor(), record=True, allow_absent=True
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
            "again with happened and your evidence; otherwise leave it: the old harness owns it.",
        )

    def _settle_happened(
        self, op: OperationRow, evidence: str | None, summary: str
    ) -> tuple[OperationRow, bool]:
        if evidence is not None:
            self._check_seen(evidence)
        with transaction(self.db):
            moved = self.transition(
                op,
                OperationState.CONFIRMED,
                actor=self.actor(),
                summary=summary,
                receipt_json=build_receipt(op, OperationState.CONFIRMED, self.now()),
            )
            if moved is None:
                raise ChamberlainError(ErrorCode.OPERATION_ALREADY_SETTLED, "That changed.")
            if evidence is not None:
                self.ctx.services.evidence.link(evidence, moved.handle)
            if moved.grant is not None:
                self.grants.mark_spent(moved.grant, actor=self.actor())
        self.release_attachments(moved)
        return moved, True

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
            row = self.transition(
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

    def cancel(self, handle: str, *, actor: str) -> OperationRow:
        """Withdraw an operation before it is prepared (T-11). Never touches dispatched ones."""
        return cancel_operation(
            self.db,
            self.ctx.clock,
            self.audit,
            self.grants,
            handle,
            actor=actor,
            release=self.release_attachments,
        )

    # --- views --------------------------------------------------------------------

    def view(self, op: OperationRow) -> OperationView:
        resent = repo.resend_of(self.db, op.handle)
        stored = op.receipt()
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
