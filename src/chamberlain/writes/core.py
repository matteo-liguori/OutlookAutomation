"""The write state machine: propose, do, reconcile (docs/contracts/04-writes.md, R1 to R7)."""

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
from chamberlain.domain.grant_rules import intent_hash, scope_mismatch
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
    MailSend,
    OutsideAction,
    WriteIntent,
    attachments_of,
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
from chamberlain.domain.senders import parse_instruction_ref
from chamberlain.domain.timefmt import ts
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
from chamberlain.writes import held, kinds, outside, repo, resend
from chamberlain.writes.authorise import (
    Allow,
    Decision,
    Deny,
    OperationFacts,
    Usage,
    authorise,
    requirement_needed,
)
from chamberlain.writes.control import release_holds
from chamberlain.writes.grants import GrantService
from chamberlain.writes.models import (
    GrantRow,
    OperationRow,
    Proposal,
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


def _stored_code(op: OperationRow) -> ErrorCode | None:
    try:
        return ErrorCode(op.error_code) if op.error_code else None
    except ValueError:
        return None


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
        summary: str,
        *,
        actor: str | None = None,
        expected_lease: str | bool | None = False,
        **columns: str | int | None,
    ) -> OperationRow | None:
        leased = (OperationState.DISPATCHED, OperationState.RECONCILING)
        if to in leased:
            columns["lease_owner"] = self.worker
            columns["lease_expires_at"] = ts(
                self.ctx.clock.now() + timedelta(seconds=LEASE_SECONDS)
            )
        elif op.state in leased:
            if expected_lease is False:
                expected_lease = self.worker
            columns.update(lease_owner=None, lease_expires_at=None)
        if (
            to
            in (
                OperationState.FAILED_CLEAN,
                OperationState.CONFIRMED_ABSENT,
                OperationState.ABANDONED,
                OperationState.SUPERSEDED,
            )
            and "receipt_json" not in columns
        ):
            columns["receipt_json"] = build_receipt(op, to, self.now())
        actor = self.actor() if actor is None else actor
        with transaction(self.db):
            moved = move_operation(
                self.db,
                self.ctx.clock,
                self.audit,
                op,
                to,
                actor=actor,
                summary=summary,
                expected_lease=expected_lease,
                **columns,
            )
            if (
                moved
                and moved.grant
                and to in (OperationState.CONFIRMED, OperationState.AGENT_REPORTED)
            ):
                self.grants.mark_spent(moved.grant, actor=actor)
            if moved and to in (
                OperationState.CONFIRMED,
                OperationState.CONFIRMED_ABSENT,
                OperationState.ABANDONED,
                OperationState.FAILED_CLEAN,
            ):
                release_holds(self.ctx.services.artifacts, moved)
            return moved

    def transition_required(
        self,
        op: OperationRow,
        to: OperationState,
        summary: str,
        *,
        changed: str = "That operation changed.",
        actor: str | None = None,
        expected_lease: str | bool | None = False,
        **columns: str | int | None,
    ) -> OperationRow:
        """`transition`; a row another worker moved first raises OPERATION_ALREADY_SETTLED."""
        moved = self.transition(
            op, to, summary, actor=actor, expected_lease=expected_lease, **columns
        )
        if moved is None:
            raise ChamberlainError(ErrorCode.OPERATION_ALREADY_SETTLED, changed)
        return moved

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

    def located(self, op: OperationRow) -> kinds.Located:
        return kinds.Located.from_refs(op.refs())

    # --- propose ------------------------------------------------------------------

    def propose_intent(
        self,
        intent: WriteIntent,
        *,
        card: str | None = None,
        evidence: tuple[str, ...] = (),
        hold_for: str | None = None,
    ) -> Proposal:
        """Store a complete intent and report the authority it needs (W-1, W-18, W-19).

        `hold_for` names the owner's card action a drafted send waits on (W-48): the send is
        held for the owner's confirmation in the viewer even when a standing grant covers it.
        """
        if hold_for is not None:
            card = held.check_hold(self.db, intent, card, hold_for)
        if isinstance(intent, OutsideAction):
            # No connector stands behind it: nothing to locate, complete or limit (W-44).
            outside.check_outside(self, intent, card)
            completed, located = intent, kinds.Located()
        else:
            completed, located = kinds.prepare_intent(self.ctx, intent, card)
        return self.propose_completed(completed, located, card, evidence, hold_for=hold_for)

    def propose_completed(
        self,
        intent: WriteIntent,
        located: kinds.Located,
        card: str | None,
        evidence: tuple[str, ...],
        *,
        redo: OperationRow | None = None,
        hold_for: str | None = None,
    ) -> Proposal:
        """One storage and duplicate-check path for fresh proposals and authorised resends."""
        refs = located.to_refs()
        sha = intent_hash(intent)
        existing = self._existing(intent, sha, card, redo)
        if existing is not None:
            if hold_for is not None:
                held.hold_existing(self, existing, intent, hold_for)
            return self.proposal_for(existing, intent)
        handle = new_handle(HandleKind.OPERATION)
        decision = self.decide(intent, handle, sha, card, reserved=None, located=located, redo=redo)
        if hold_for is not None:
            decision = Deny(ErrorCode.GRANT_REQUIRED, Requirement.HUMAN_CONFIRMATION)
        if redo is not None and isinstance(decision, Deny):
            raise ChamberlainError(
                decision.code,
                "The permission behind the first send no longer covers sending it again.",
                fix="Ask the owner, in chat, whether to send it again.",
            )
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
        if isinstance(intent, MailSend):
            self.scan_before_storing(intent, handle, refs, decision)
        with transaction(self.db):
            existing = self._existing(intent, sha, card, redo)
            if existing is not None:
                return self.proposal_for(existing, intent)
            if redo is not None and self.get(redo.handle).state is not OperationState.UNCERTAIN:
                raise ChamberlainError(
                    ErrorCode.OPERATION_ALREADY_SETTLED, "The write this one replaces just changed."
                )
            self.insert_proposal(
                intent, handle, sha, card, refs, redo_of=redo.handle if redo else None
            )
            if hold_for is not None:
                held.propose_held(self, intent, sha, card, hold_for)
            elif (
                isinstance(decision, Deny)
                and repo.proposed_one_off(self.db, intent.source, sha) is None
            ):
                self.grants.propose_one_off(
                    source_id=intent.source,
                    intent_sha256=sha,
                    title=grant_title(intent),
                    summary=grant_summary(intent),
                    requirement=decision.requirement,
                    card=card,
                    actor=self.actor(),
                )
        self.hold_inputs(intent, handle, evidence)
        return self.proposal_for(
            self.get(handle), intent, decision=decision if redo is None else None
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
            if scope.card is not None and repo.card_state(self.db, scope.card) != "open":
                raise ChamberlainError(
                    ErrorCode.REQUEST_INVALID,
                    "That card does not exist or is closed.",
                    fix="Bind it to an open card.",
                    field="standing",
                )
        else:
            for source in sorted(scope.all_sources()):
                for kind in scope.intent_kinds:
                    caps = kinds.capabilities(self.ctx, source, kind)
                    kinds.require_write(caps, kind)
                    if (
                        kind is IntentKind.CALENDAR_RESPOND
                        and caps.get(CapabilityName.CALENDAR_INVITE) is None
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
        intent = op.intent()
        if op.state is not OperationState.PROPOSED:
            return self.proposal_for(op, intent)
        grant = repo.proposed_one_off(self.db, op.source_id, op.intent_sha256)
        if grant is not None:
            needed = requirement_needed(
                intent, self._recipients(intent, self.located(op)), self.policy
            )
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

    def _recorded_in_session(self, evidence: str) -> bool:
        session = self.ctx.session
        return session is not None and self.ctx.services.evidence.created_in_session(
            evidence, session
        )

    def _check_evidence(self, evidence: str, intent: WriteIntent) -> None:
        """Approval evidence: the user's own words or an authorised sender's (G-13, G-13a)."""
        facts = repo.evidence_facts(self.db, evidence)
        ref = None if facts is None else facts[1]
        instruction = parse_instruction_ref(ref)
        if not self._recorded_in_session(evidence) or (ref is not None and instruction is None):
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

    def proposal_for(
        self, op: OperationRow, intent: WriteIntent, *, decision: Decision | None = None
    ) -> Proposal:
        """The current picture for an operation that already exists."""
        if decision is None and op.state in _RESERVABLE_STATES:
            decision, _ = self._reservation_decision(op, intent)
        grant = (
            repo.get_grant(self.db, decision.grant)
            if isinstance(decision, Allow)
            else repo.proposed_one_off(self.db, op.source_id, op.intent_sha256)
        )
        text, cut = kinds.describe_write(intent, self.located(op))
        return Proposal(
            operation=op,
            grant=grant,
            state=op.state,
            requirement=decision.requirement if isinstance(decision, Deny) else Requirement.NONE,
            ready=isinstance(decision, Allow),
            content_sha256=op.intent_sha256,
            preview=text,
            truncated=cut,
            origin=op.handle,
        )

    def _existing(
        self, intent: WriteIntent, sha: str, card: str | None, redo: OperationRow | None
    ) -> OperationRow | None:
        """An identical live operation, or a recent one that may not repeat (W-18, W-19)."""
        if redo is not None:
            return repo.resend_of(self.db, redo.handle)
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
        grants: list[GrantRow],
        located: kinds.Located,
        redo: OperationRow | None,
    ) -> Usage:
        now = self.ctx.clock.now()
        names = [g.handle for g in grants]
        skip: str | tuple[str, ...] = handle if redo is None else (handle, redo.handle)
        folders: frozenset[str] = frozenset()
        if isinstance(intent, MailMarkRead) and any(g.scope and g.scope.folders for g in grants):
            port = self.ctx.sources.mail_port(intent.source)
            folders = frozenset(
                port.folder_of(intent.source, located.refs[h]) for h in intent.messages
            )
        uses = repo.grant_usage(self.db, names, ts(now - timedelta(hours=24)), skip)
        return replace(
            self._recipients(intent, located),
            mail_folders=folders,
            live_uses=uses.reserved_or_spent,
            recent_uses=uses.recent,
            source_sends_last_hour=repo.sends_since(
                self.db, intent.source, ts(now - timedelta(hours=1)), handle
            ),
        )

    def _recipients(self, intent: WriteIntent, located: kinds.Located) -> Usage:
        known = repo.known_recipients(self.db, intent.source)
        if kinds.KINDS[intent.kind].calendar:
            known |= repo.known_recipients_anywhere(self.db)
        return Usage(
            known_recipients=known,
            event_attendees=located.event_attendees,
            owner_addresses=repo.owner_addresses(self.db),
        )

    def _grants_for(
        self, source: str, reserved: str | None, redo: OperationRow | None
    ) -> list[GrantRow]:
        pinned = redo.grant if redo is not None else reserved
        if self._only_grant is not None:
            if pinned is not None and pinned != self._only_grant:
                return []
            pinned = self._only_grant
        if pinned is None:
            return (
                []
                if redo is not None
                else repo.grants_in_states(self.db, (GrantState.ACTIVE,), source)
            )
        held = repo.get_grant(self.db, pinned)
        if held is None:
            return []
        # An expired one-off still covers the identical, unspent resend (W-38).
        if redo is not None and held.kind is GrantKind.ONE_OFF and held.state is GrantState.EXPIRED:
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
        located: kinds.Located,
        redo: OperationRow | None = None,
    ) -> Decision:
        grants = self._grants_for(intent.source, reserved, redo)
        pinned = reserved if redo is None else redo.grant
        holding = None if redo is not None else repo.held_one_off(self.db, intent.source, sha)
        if holding is not None:
            # A held send waits for the owner: no standing grant may release it (W-48).
            grants = [g for g in grants if g.handle == holding.handle]
        decision = authorise(
            intent,
            OperationFacts(handle, sha, card, pinned),
            grants,
            self._usage(intent, handle, grants, located, redo),
            self.policy,
            self.ctx.clock.now(),
        )
        if holding is not None and isinstance(decision, Deny):
            return Deny(decision.code, Requirement.HUMAN_CONFIRMATION)
        return decision

    # --- do -----------------------------------------------------------------------

    def _reservation_decision(
        self, op: OperationRow, intent: WriteIntent
    ) -> tuple[Decision, OperationRow | None]:
        original = None
        if op.redo_of is not None and op.state is OperationState.PROPOSED:
            original = resend.original_of(self, op)
            if original is None:
                return Deny(ErrorCode.OPERATION_ALREADY_SETTLED, Requirement.NONE), None
        decision = self.decide(
            intent, op.handle, op.intent_sha256, op.card, op.grant, self.located(op), original
        )
        return decision, original

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
        if op.state in MAY_HAVE_HAPPENED_STATES:
            self.recover()
            op = self.get(handle)
            if op.state in MAY_HAVE_HAPPENED_STATES:
                raise self.needs_reconcile(handle)
        if op.state in TERMINAL_STATES:
            return self.terminal_reply(op)
        intent = op.intent()
        if op.redo_of is not None and op.state is OperationState.PROPOSED:
            resend.guard_resend(self, op.redo_of)
        op = self.reserve(op, intent)
        op = self._prepare(op)
        final = self._dispatch(op)
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
            raise ChamberlainError(
                _stored_code(op) or ErrorCode.SOURCE_UNAVAILABLE,
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
            decision, original = self._reservation_decision(current, intent)
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
                release_holds(self.ctx.services.artifacts, replaced)
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
        return self.transition(
            op,
            OperationState.FAILED_CLEAN,
            summary=(
                f"Nothing happened ({code.value}"
                + (f": {detail[:200]}" if detail else "")
                + "); grant use released."
            ),
            expected_lease=expected_lease,
            error_code=code.value,
        )

    def _prepare(self, op: OperationRow) -> OperationRow:
        """Read-only. A failure cannot have changed anything and frees the grant (W-11)."""
        if op.state is OperationState.PREPARED:
            return op
        intent = op.intent()
        refs = op.refs()
        grant = repo.get_grant(self.db, op.grant) if op.grant else None
        if grant is not None and grant.scope is not None and grant.scope.allow_duplicate:
            refs[ALLOW_DUPLICATE_REF] = "1"
        try:
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
        return self.transition_required(
            op,
            OperationState.PREPARED,
            "Prepared by a read-only connector call.",
            prepared_json=prepared.model_dump_json(exclude_none=True),
        )

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
        intent = op.intent()
        if op.prepared_json is None:
            raise ChamberlainError(ErrorCode.INTERNAL_UNEXPECTED, "A prepared write is missing.")
        prepared = Prepared.model_validate_json(op.prepared_json)
        try:
            payloads = self._read_attachments(intent)
        except ChamberlainError as error:
            self._fail_clean(op, error.code)
            raise
        now = self.ctx.clock.now()
        journal = self.transition_required(
            op,
            OperationState.DISPATCHED,
            "Journalled before the one mutation call.",
            dispatched_at=ts(now),
            worker=self.worker,
        )
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
            summary="Outcome unknown; it may have happened.",
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
            held = self.transition(
                current, OperationState.RECONCILING, "Late result recorded as evidence."
            )
            if held is None:
                return self.get(journal.handle)
            self.confirm_receipt(
                held,
                receipt,
                actor="system:tick",
                summary="A late applied result was recorded as reconcile evidence.",
            )
        return self.get(journal.handle)

    # --- confirming ---------------------------------------------------------------

    def confirm_receipt(
        self, op: OperationRow, receipt: ProviderReceipt, summary: str, *, actor: str | None = None
    ) -> OperationRow | None:
        result_ref = kinds.result_ref(self.ctx, op, receipt)
        verified = receipt.verification
        if receipt.warnings:
            summary += " Warnings: " + ", ".join(receipt.warnings)
        moved = self.transition(
            op,
            OperationState.CONFIRMED,
            summary,
            actor=actor,
            receipt_json=build_receipt(
                op, OperationState.CONFIRMED, self.now(), provider=receipt, result_ref=result_ref
            ),
            client_verified=int(verified.client_verified),
            server_verified=int(verified.server_verified),
        )
        if moved is not None:
            kinds.after_confirmed(self.ctx, moved, moved.intent(), self.located(moved), receipt)
        return moved

    def _upgrade_verification(self, op: OperationRow, prepared: Prepared) -> None:
        """Try to upgrade a client-verified result by reading back (W-14, R6).

        Never changes the state; failure is silent.
        """
        if op.server_verified or op.intent_json is None:
            return
        intent = op.intent()
        try:
            reading = self._reconcile_loop(op, intent, prepared, self._window(op))
        except Exception:
            log_failure(log, "verification upgrade failed for %s", op.handle)
            return
        found = reading.receipt
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

    def _reconcile_loop(
        self, op: OperationRow, intent: WriteIntent, prepared: Prepared, window: tuple[int, int]
    ) -> ReconcileResult:
        """Bounded, read-only reconciles (R6). Stops at the first `present`."""
        attempts, seconds = window
        started = self.ctx.clock.now()
        reading = ReconcileResult(status=ReconcileStatus.UNKNOWN)
        for index in range(attempts):
            reading = kinds.port_reconcile(self.ctx, intent, prepared, op.idempotency_key)
            if reading.receipt is not None:
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
    ) -> tuple[OperationRow, ReconcileResult | None]:
        """`_reconcile`, and what the connector said (None when no read was made)."""
        who = actor or self.actor()
        intent = op.intent()
        if op.prepared_json is None:
            return op, None
        prepared = Prepared.model_validate_json(op.prepared_json)
        held = self.transition(
            op, OperationState.RECONCILING, "Read-only reconcile started.", actor=who
        )
        if held is None:
            return self.get(op.handle), None
        reading = self._reconcile_loop(held, intent, prepared, window)
        if reading.receipt is not None:
            confirmed = self.confirm_receipt(
                held,
                reading.receipt,
                actor=who,
                summary="Reconcile found the write.",
            )
            return (confirmed or self.get(op.handle)), reading
        if allow_absent and reading.proof and self._absence_proven(held, intent):
            absent = self.transition(
                held,
                OperationState.CONFIRMED_ABSENT,
                actor=who,
                summary="Reconcile proved the write did not happen; grant use released.",
            )
            return absent or self.get(op.handle), reading
        back = self.transition(
            held,
            OperationState.UNCERTAIN,
            actor=who,
            summary="Still uncertain after a read-only reconcile."
            + (" An exact copy is queued." if reading.status is ReconcileStatus.QUEUED else ""),
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
            capability = kinds.require_write(
                kinds.capabilities(self.ctx, intent.source, intent.kind), intent.kind
            )
        except ChamberlainError:
            return False
        return op.kind.value in capability.absence_provable

    def reconcile(
        self, handle: str, *, happened_evidence: str | None = None
    ) -> tuple[OperationRow, bool]:
        """Settle an uncertain operation by reading, never by resending (T-13, W-12)."""
        self.recover()
        before = self.get(handle)
        if before.state is not OperationState.UNCERTAIN:
            return before, False
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

    def _settle_happened(
        self, op: OperationRow, evidence: str | None, summary: str
    ) -> tuple[OperationRow, bool]:
        if evidence is not None:
            self._check_seen(evidence)
        with transaction(self.db):
            moved = self.transition_required(
                op,
                OperationState.CONFIRMED,
                summary,
                receipt_json=build_receipt(op, OperationState.CONFIRMED, self.now()),
            )
            if evidence is not None:
                self.ctx.services.evidence.link(evidence, moved.handle)
        return moved, True

    def _check_seen(self, evidence: str) -> None:
        """Evidence that it happened is something this session recorded. It frees nothing."""
        if not self._recorded_in_session(evidence):
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
            )
            moved += 1 if row is not None else 0
        return moved

    # --- views --------------------------------------------------------------------

    def view(self, op: OperationRow) -> OperationView:
        resent = repo.resend_of(self.db, op.handle)
        stored = op.receipt()
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
            error_code=_stored_code(op),
            result_ref=None if stored is None else stored.result_ref,
            items_ok=0 if stored is None else stored.summary.items_ok,
            items_failed=0 if stored is None else stored.summary.items_failed,
            warnings=() if stored is None else stored.warnings,
            settled_at=op.settled_at,
            recheck=recheck_text(op.recheck_result, op.recheck_json),
            redo_of=op.redo_of,
            redone_by=None if resent is None else resent.handle,
        )
