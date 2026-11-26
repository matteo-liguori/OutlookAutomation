"""The bounded send recheck and the linked, one-authority resend (W-37 to W-41)."""

from __future__ import annotations

import json
import logging
from datetime import timedelta

from chamberlain.domain.grant_rules import format_ts, intent_hash
from chamberlain.domain.handles import HandleKind, new_handle
from chamberlain.domain.intents import MailSend, WriteIntent, attachments_of
from chamberlain.domain.operations import OperationState
from chamberlain.limits import UNCERTAIN_ABANDON_DAYS, UNCERTAIN_SEND_RECHECK_HOURS
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.schema.safelog import log_failure
from chamberlain.store.db import transaction
from chamberlain.writes import core, repo
from chamberlain.writes.authorise import Deny
from chamberlain.writes.models import OperationRow, Proposal
from chamberlain.writes.recheck import look_in_recipient_mailboxes, sender_places
from chamberlain.writes.transitions import build_receipt

log = logging.getLogger("chamberlain.writes")


def original_of(engine: core.Engine, op: OperationRow) -> OperationRow | None:
    """The still-uncertain send that a resend replaces; None once it has settled."""
    if op.redo_of is None:
        return None
    original = repo.get_operation(engine.db, op.redo_of)
    if original is None or original.state is not OperationState.UNCERTAIN:
        return None
    return original


def supersede(engine: core.Engine, original: OperationRow, resend: str) -> None:
    """The send ends `superseded`; its grant use passes to the resend (W-38)."""
    moved = engine.transition(
        original,
        OperationState.SUPERSEDED,
        actor=engine.actor(),
        summary=f"Done again as {resend}; the grant use passed to it.",
        receipt_json=build_receipt(original, OperationState.SUPERSEDED, engine.now()),
    )
    if moved is None:
        raise ChamberlainError(
            ErrorCode.OPERATION_ALREADY_SETTLED, "The send this one replaces just changed."
        )


def abandon(engine: core.Engine, op: OperationRow) -> OperationRow | None:
    """Close an uncertain operation nothing can resend. It frees no grant and claims nothing."""
    moved = engine.transition(
        op,
        OperationState.ABANDONED,
        actor="system:tick",
        summary="Closed unsettled after "
        f"{UNCERTAIN_ABANDON_DAYS} days; nothing can send it again; no grant was freed.",
        receipt_json=build_receipt(op, OperationState.ABANDONED, engine.now()),
    )
    if moved is not None:
        engine.release_attachments(moved)
    return moved


def recheck_sends(engine: core.Engine) -> int:
    """The tick's step: each uncertain send old enough gets one more look. Never raises."""
    cutoff = engine.ctx.clock.now() - timedelta(hours=UNCERTAIN_SEND_RECHECK_HOURS)
    looked = 0
    for op in repo.sends_due_recheck(engine.db, format_ts(cutoff)):
        try:
            _, outcome = look_for_send(engine, op, actor="system:tick", record=True)
        except Exception:
            log_failure(log, "re-check failed for %s", op.handle)
            continue
        looked += 1 if outcome is not None else 0
    return looked


def look_for_send(
    engine: core.Engine,
    op: OperationRow,
    *,
    actor: str,
    record: bool,
    allow_absent: bool = False,
) -> tuple[OperationRow, str | None]:
    """Read the target (a send: Sent, Outbox and Drafts, then any recipient Inbox).

    Returns the operation and `found` (settled confirmed), `queued`, `missing`, or None when
    nothing could be concluded (the read failed, or the operation is no longer uncertain).
    With `record`, a `queued` or `missing` answer is stored on the operation and audited.
    """
    before = engine.get(op.handle)
    if before.state is not OperationState.UNCERTAIN or before.intent_json is None:
        return before, None
    intent = engine.intent_of(before)
    row, reading = engine.reconcile_reading(
        before, window=(1, 0), allow_absent=allow_absent, actor=actor
    )
    if row.state is OperationState.CONFIRMED:
        return row, "found"
    if row.state is not OperationState.UNCERTAIN or reading is None:
        return row, None
    if not isinstance(intent, MailSend):
        if reading.absent and not reading.queued:
            if record:
                _record_recheck(engine, row, "missing", [f"the {row.kind.value} target"], actor)
            return row, "missing"
        return row, None
    looked = [sender_places(row.source_id)]
    if reading.queued:
        outcome = "queued"
    elif reading.absent:
        nearby = look_in_recipient_mailboxes(engine.ctx, row, intent)
        if nearby.receipt is not None:
            done = engine.confirm_receipt(
                row,
                nearby.receipt,
                expected_lease=False,
                actor=actor,
                summary="The re-check found it in a recipient's own mailbox.",
            )
            return (done or engine.get(row.handle)), "found"
        looked.extend(nearby.looked)
        outcome = "missing"
    else:
        return row, None
    if record:
        _record_recheck(engine, row, outcome, looked, actor)
    return row, outcome


def _record_recheck(
    engine: core.Engine, op: OperationRow, outcome: str, looked: list[str], actor: str
) -> None:
    age = engine.ctx.clock.now() - (op.dispatched_at or op.created_at)
    with transaction(engine.db):
        stored = repo.record_recheck(
            engine.db,
            op.handle,
            result=outcome,
            looked_json=json.dumps({"looked": looked}),
            at=engine.now(),
        )
        if stored:
            engine.audit.append(
                actor=actor,
                kind="operation.rechecked",
                subject=op.handle,
                summary=f"Looked again after {int(age.total_seconds() // 3600)} hours: "
                f"{outcome}. Looked in {'; '.join(looked)}.",
                payload_sha256=op.intent_sha256,
            )


def propose_redo(
    engine: core.Engine,
    original: str,
    *,
    card: str | None = None,
    evidence: tuple[str, ...] = (),
) -> Proposal:
    """Send an uncertain, missing send again, on its original authority (W-38).

    Only after the harness's 24-hour look found it missing. The text is the original's, never
    the agent's. The original ends `superseded` only when `do` runs the resend.
    """
    old = engine.get(original)
    intent = _resendable(engine, old)
    existing = repo.resend_of(engine.db, old.handle)
    if existing is not None:
        return engine.proposal_for(existing, intent)
    guard_resend(engine, old.handle)
    old = engine.get(old.handle)
    for attachment in attachments_of(intent):
        if not engine.ctx.services.artifacts.info(attachment.artifact).live:
            raise ChamberlainError(ErrorCode.ARTIFACT_EXPIRED, "An attachment has expired.")
    completed = (
        intent.model_copy(update={"message_id": None}) if isinstance(intent, MailSend) else intent
    )
    sha = intent_hash(completed)
    located = engine.located(old)
    refs = located.to_refs()
    handle = new_handle(HandleKind.OPERATION)
    decision = engine.decide(completed, handle, sha, card or old.card, None, located, redo=old)
    if isinstance(decision, Deny):
        raise ChamberlainError(
            decision.code,
            "The permission behind the first send no longer covers sending it again.",
            fix="Ask the owner, in chat, whether to send it again.",
        )
    if isinstance(completed, MailSend):
        engine.scan_before_storing(completed, handle, refs, decision)
    with transaction(engine.db):
        existing = repo.resend_of(engine.db, old.handle)
        if existing is not None:
            return engine.proposal_for(existing, completed)
        if engine.get(old.handle).state is not OperationState.UNCERTAIN:
            raise ChamberlainError(
                ErrorCode.OPERATION_ALREADY_SETTLED, "The write this one replaces just changed."
            )
        engine.insert_proposal(completed, handle, sha, card or old.card, refs, redo_of=old.handle)
    engine.hold_inputs(completed, handle, evidence)
    return engine.proposal_for(engine.get(handle), completed)


def _resendable(engine: core.Engine, old: OperationRow) -> WriteIntent:
    """Refuse, with a plain reason, anything that is not an uncertain send awaiting a resend."""
    if old.intent_json is None:
        raise ChamberlainError(
            ErrorCode.REQUEST_WRONG_KIND,
            "Only a write the harness holds the full request of can be done again. "
            "One from the old harness cannot.",
            field="redo_of",
        )
    intent = engine.intent_of(old)
    if repo.resend_of(engine.db, old.handle) is not None:
        return intent
    if old.redo_of is not None:
        raise ChamberlainError(
            ErrorCode.REQUEST_INVALID,
            "That send was already a resend. A second resend needs the owner's word.",
            fix="Ask the owner, in chat, whether to send it again.",
            field="redo_of",
        )
    engine.recover()
    old = engine.get(old.handle)
    if old.state is not OperationState.UNCERTAIN:
        raise ChamberlainError(
            ErrorCode.OPERATION_ALREADY_SETTLED,
            f"That write is {old.state.value}; there is nothing to do again.",
        )
    if old.rechecked_at is None:
        raise ChamberlainError(
            ErrorCode.REQUEST_INVALID,
            "Too early. A write is not done again before the harness has looked for it "
            f"after {UNCERTAIN_SEND_RECHECK_HOURS} hours.",
            fix="Wait for the item that says it is missing. Until then do not do it again.",
            field="redo_of",
        )
    return intent


def guard_resend(engine: core.Engine, original: str) -> None:
    """One last read-only look just before a resend. Anything but `missing` stops it."""
    old = engine.get(original)
    if old.state is not OperationState.UNCERTAIN:
        raise ChamberlainError(
            ErrorCode.OPERATION_ALREADY_SETTLED,
            f"That write is {old.state.value}; there is nothing to do again.",
        )
    row, outcome = look_for_send(engine, old, actor=engine.actor(), record=True)
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
