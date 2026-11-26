"""Important appointments: the owner's reminders that stay until the owner acknowledges them.

Contract: docs/contracts/06-memory.md (the report section) and 05-grants.md (G-17, the viewer is
the owner's channel). An important appointment is an open action on an open card that the agent
marked `important` (owner ruling 2026-11-24: the agent decides from the card's rules, the context
and its general rules; it is not a title prefix). Its due time is the appointment's start and its
snooze time is when that reminder should surface (the agent sets one action per reminder). A
leading "Important:" in the title is still trimmed for display.

A reminder waits for the owner while its snooze time has passed and the appointment has not
started. The owner acknowledges it in the viewer by holding a button; that finishes the action,
and the audit log names the owner (`human:viewer`). Nothing in the agent's tools calls
`acknowledge`: the viewer's own route is the only caller, behind its token and Origin checks.
"""

import re
from dataclasses import dataclass

from chamberlain.app.context import Context
from chamberlain.memory import repo as memory_repo
from chamberlain.memory.cards import CardService
from chamberlain.runs.tuning import VIEWER_ACTOR
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.store.db import transaction
from chamberlain.store.migrate import stamp

OUTCOME = "Acknowledged by the owner in the viewer."
_DASHES = "\N{EN DASH}\N{EM DASH}"
_MARKER = re.compile(rf"^\s*important\s*[:\-{_DASHES}]\s*(?P<rest>.+)$", re.IGNORECASE | re.DOTALL)


@dataclass(frozen=True)
class Appointment:
    action: str
    card: str
    card_title: str
    title: str
    start: str
    """The appointment's start, as a stored timestamp."""
    reminder_from: str | None
    due: bool
    """True while this reminder waits for the owner's acknowledgement."""


def upcoming(ctx: Context) -> list[Appointment]:
    """Important appointments that have not started, soonest first."""
    now = stamp(ctx.clock)
    found: list[Appointment] = []
    for row in memory_repo.action_lines(
        ctx.db,
        "c.state = 'open' AND a.due_at > ? AND a.important = 1",
        (now,),
    ):
        marker = _MARKER.match(row.title)
        title = marker.group("rest") if marker is not None else row.title
        waiting = row.snooze_until is None or row.snooze_until <= now
        found.append(
            Appointment(
                row.handle,
                row.card,
                row.card_title,
                " ".join(title.split()),
                str(row.due_at),
                row.snooze_until,
                waiting,
            )
        )
    return found


def acknowledge(ctx: Context, action: str) -> None:
    """The owner acknowledges one waiting reminder: it is finished and the audit says who."""
    match = next((a for a in upcoming(ctx) if a.action == action), None)
    if match is None or not match.due:
        raise ChamberlainError(
            ErrorCode.REQUEST_INVALID,
            "That reminder is not waiting for you any more. Refresh the page.",
        )
    with transaction(ctx.db):
        CardService(ctx.db, ctx.clock).finish_action(action, None, OUTCOME)
        ctx.services.audit.append(
            actor=VIEWER_ACTOR,
            kind="appointment.acknowledged",
            subject=action,
            summary="The owner acknowledged an important appointment reminder.",
        )
