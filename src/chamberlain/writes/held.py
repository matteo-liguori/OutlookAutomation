"""Held sends: a drafted reply waits in Chamberlain until the owner approves it (W-48, W-49).

The agent proposes the exact `mail.send` with `action`: the owner's card action that asks them
to approve it. That makes a one-off grant bound to the exact content, which only the owner's
confirmation in the viewer can activate; no standing grant releases it. When the owner
confirms, `send_approved` sends exactly the stored intent once, as `system:approved-send`, and
finishes the action. A failure or an uncertain outcome leaves the action open with a note.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Final, Literal

from chamberlain.app.context import Context
from chamberlain.domain.grants import GrantKind, GrantState, Requirement
from chamberlain.domain.intents import IntentKind, MailSend, WriteIntent
from chamberlain.domain.operations import MAY_HAVE_HAPPENED_STATES, OperationState
from chamberlain.memory.cards import CardService
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.store.db import transaction
from chamberlain.writes import core, repo
from chamberlain.writes.models import GrantRow, OperationRow, WritePolicy
from chamberlain.writes.render import grant_summary, grant_title

ACTOR: Final = "system:approved-send"
_SENDABLE: Final = frozenset(
    {OperationState.PROPOSED, OperationState.AUTHORISED, OperationState.PREPARED}
)


# --- holding --------------------------------------------------------------------------------


def check_hold(db: sqlite3.Connection, intent: WriteIntent, card: str | None, action: str) -> str:
    """The card the held send belongs to: the action's own. Refuses anything else."""
    if not isinstance(intent, MailSend):
        raise ChamberlainError(
            ErrorCode.REQUEST_INVALID,
            "Only a mail.send can wait for the owner's approval.",
            fix="Leave action out, or propose the reply as mail.send.",
            field="action",
        )
    row = db.execute(
        "SELECT card, state, for_owner FROM actions WHERE handle = ?", (action,)
    ).fetchone()
    if row is None or str(row["state"]) != "open" or not int(row["for_owner"]):
        raise ChamberlainError(
            ErrorCode.REQUEST_INVALID,
            "That is not an open action waiting for the owner.",
            fix="Add an action for the owner (for_owner) titled with the ask, then propose.",
            field="action",
        )
    owner_card = str(row["card"])
    if card is not None and card != owner_card:
        raise ChamberlainError(
            ErrorCode.REQUEST_INVALID,
            "The action belongs to another card.",
            fix="Leave card out, or give the action's own card.",
            field="card",
        )
    return owner_card


def propose_held(
    engine: core.Engine, intent: WriteIntent, sha: str, card: str | None, action: str
) -> None:
    """Inside the proposal's transaction: the one-off that holds it, unless one exists."""
    if repo.held_one_off(engine.db, intent.source, sha) is not None:
        return
    engine.grants.propose_one_off(
        source_id=intent.source,
        intent_sha256=sha,
        title=grant_title(intent),
        summary=grant_summary(intent),
        requirement=Requirement.HUMAN_CONFIRMATION,
        card=card,
        actor=engine.actor(),
        action=action,
    )


def hold_existing(engine: core.Engine, op: OperationRow, intent: WriteIntent, action: str) -> None:
    """The same send proposed again with an action: hold it, if it has not gone yet."""
    if op.state is not OperationState.PROPOSED:
        return
    check_hold(engine.db, intent, op.card, action)
    with transaction(engine.db):
        propose_held(engine, intent, op.intent_sha256, op.card, action)


# --- sending once confirmed -----------------------------------------------------------------


@dataclass(frozen=True)
class SendOutcome:
    """What the confirmation did with the approved send, in the owner's words."""

    operation: str
    state: str
    result: Literal["sent", "uncertain", "not_sent"]
    message: str


def _sendable(ctx: Context, grant: GrantRow) -> OperationRow | None:
    for op in reversed(repo.live_by_hash(ctx.db, grant.source_id, grant.content_sha256)):
        if op.kind is IntentKind.MAIL_SEND and op.state in _SENDABLE:
            return op
    return None


def send_approved(
    ctx: Context, grant: GrantRow, policy: WritePolicy | None = None
) -> SendOutcome | None:
    """Send the confirmed one-off's exact send once. None when there is nothing to send.

    A second confirmation, or one after the send went, finds no sendable operation: the
    engine's own state (and its idempotency key from the grant) keeps the send to one.
    """
    if grant.kind is not GrantKind.ONE_OFF or grant.state is not GrantState.ACTIVE:
        return None
    op = _sendable(ctx, grant)
    if op is None:
        return None
    engine = core.Engine(ctx, policy)
    error: ChamberlainError | None = None
    try:
        engine.run(op.intent(), actor=ACTOR, grant=grant.handle, card=op.card)
    except ChamberlainError as raised:
        error = raised
    final = repo.get_operation(ctx.db, op.handle) or op
    outcome = _outcome(ctx, final, error, engine.policy)
    _record(ctx, grant, outcome)
    return outcome


def _outcome(
    ctx: Context, op: OperationRow, error: ChamberlainError | None, policy: WritePolicy
) -> SendOutcome:
    state = op.state.value
    if op.state is OperationState.CONFIRMED:
        at = ctx.clock.now().astimezone(policy.tz).strftime("%H:%M")
        return SendOutcome(op.handle, state, "sent", f"Sent as approved at {at} (local)")
    if op.state in MAY_HAVE_HAPPENED_STATES:
        return SendOutcome(
            op.handle,
            state,
            "uncertain",
            f"The approved send {op.handle} may have gone and is not confirmed yet. "
            "Reconcile it; never send it again.",
        )
    why = error.code.value if error is not None else (op.error_code or state)
    return SendOutcome(
        op.handle,
        state,
        "not_sent",
        f"The approved send {op.handle} was not sent ({why}). Nothing went out.",
    )


def _record(ctx: Context, grant: GrantRow, outcome: SendOutcome) -> None:
    """Finish the linked action on success; otherwise leave it, with a note saying why."""
    ctx.services.audit.append(
        actor=ACTOR,
        kind=f"send.approved.{outcome.result}",
        subject=outcome.operation,
        summary=outcome.message[:200],
        payload_sha256=grant.content_sha256,
    )
    if grant.action is None:
        return
    cards = CardService(ctx.db, ctx.clock)
    try:
        if outcome.result == "sent":
            cards.finish_action(grant.action, None, outcome.message)
        else:
            cards.add_note(grant.action, outcome.message)
    except ChamberlainError:
        # The action was finished or is full of notes: the operation and the audit row
        # still carry the outcome for the next session.
        pass
