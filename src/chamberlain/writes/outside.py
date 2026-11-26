"""Outside actions reserve and report without a connector (W-44 to W-47)."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

from chamberlain.domain.grant_rules import format_ts, intent_hash
from chamberlain.domain.intents import OutsideAction
from chamberlain.domain.operations import TERMINAL_STATES, OperationState
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.writes import core, repo
from chamberlain.writes.models import OperationRow
from chamberlain.writes.transitions import build_receipt

OUTSIDE_REPORT_HOURS: Final = 24


def check_outside(engine: core.Engine, intent: OutsideAction, card: str | None) -> None:
    """Refuse what only the owner does, and an action for a closed card, before any grant."""
    owner_only = [
        words
        for words, flag in (
            ("entering a password", intent.enters_password),
            ("paying", intent.pays),
            ("accepting terms", intent.accepts_terms),
        )
        if flag
    ]
    if owner_only:
        engine.audit.append(
            actor=engine.actor(),
            kind="outside.refused",
            subject=None,
            summary="Refused an outside action: it involves " + ", ".join(owner_only) + ".",
            payload_sha256=intent_hash(intent),
        )
        raise ChamberlainError(
            ErrorCode.REQUEST_INVALID,
            "Passwords, payments and accepting terms are never done for the owner.",
            fix="Stop before that step and ask the owner to do it themselves.",
            field="intent",
            detail="owner-only-step",
        )
    if card is not None and not repo.card_is_open(engine.db, card):
        raise ChamberlainError(
            ErrorCode.REQUEST_INVALID, "That card does not exist or is closed.", field="card"
        )


def do_outside(engine: core.Engine, op: OperationRow, report: str | None) -> OperationRow:
    """Reserve the use, or record the assistant's report. Never touches a port (W-46)."""
    if op.state in TERMINAL_STATES:
        return engine.terminal_reply(op)
    intent = engine.intent_of(op)
    if not isinstance(intent, OutsideAction):
        raise ChamberlainError(ErrorCode.INTERNAL_UNEXPECTED, "Not an outside action.")
    if op.state is OperationState.PROPOSED:
        if report is not None:
            raise ChamberlainError(
                ErrorCode.REQUEST_INVALID,
                "Take the permission first: call do without a report, then act.",
                field="report",
            )
        check_outside(engine, intent, op.card)
        return engine.reserve(op, intent)
    if op.state is not OperationState.AUTHORISED:
        raise ChamberlainError(ErrorCode.OPERATION_ALREADY_SETTLED, "That operation changed.")
    if report is None:
        return op
    moved = engine.transition(
        op,
        OperationState.AGENT_REPORTED,
        "The assistant reported doing this outside Chamberlain; not checked.",
        receipt_json=build_receipt(
            op, OperationState.AGENT_REPORTED, engine.now(), reported=report
        ),
        dispatched_at=engine.now(),  # when it counts against a daily limit
    )
    if moved is None:
        raise ChamberlainError(ErrorCode.OPERATION_ALREADY_SETTLED, "That operation changed.")
    return moved


def close_unreported_outside(engine: core.Engine) -> int:
    """An outside action allowed a day ago and never reported is closed, use kept (W-46)."""
    cutoff = format_ts(engine.ctx.clock.now() - timedelta(hours=OUTSIDE_REPORT_HOURS))
    closed = 0
    for op in repo.stale_outside(engine.db, cutoff):
        moved = engine.transition(
            op,
            OperationState.ABANDONED,
            actor="system:tick",
            summary="Allowed but never reported by the assistant; closed.",
            dispatched_at=format_ts(op.updated_at or engine.ctx.clock.now()),
        )
        closed += 1 if moved is not None else 0
    return closed
