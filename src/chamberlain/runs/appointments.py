"""Important appointments: the owner's reminders that stay until the owner acknowledges them.

Contract: docs/contracts/06-memory.md (the report section) and 05-grants.md (G-17, the viewer is
the owner's channel). An important appointment is an open action on an open card that the agent
marked `important` (owner ruling 2026-11-24: the agent decides from the card's rules, the context
and its general rules; it is not a title prefix). Its due time is the appointment's start and its
snooze time is when that reminder should surface (the agent sets one action per reminder). A
leading "Important:" in the title is still trimmed for display.

Several reminder actions for the same appointment (same card, same start, the same title once
the "three weeks away" wording is removed) are one important date (`dates`): one calendar mark,
one pop-up and one report line, with its reminder times listed.

A reminder waits for the owner while its snooze time has passed and the appointment has not
started. The owner acknowledges it in the viewer by holding a button; that finishes the action,
and the audit log names the owner (`human:viewer`). Nothing in the agent's tools calls
`acknowledge`: the viewer's own route is the only caller, behind its token and Origin checks.
"""

import re
from dataclasses import dataclass, field

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
_AWAY = re.compile(
    r"\s+(?:is|are)\s+(?:[\w-]+\s+){1,3}(?:away|from now)(?:,\s*on\s+[^.]*)?\.?", re.IGNORECASE
)
_ACK_NOTE = re.compile(r"\s*acknowledge this reminder(?: when seen)?\.?\s*$", re.IGNORECASE)
_LABEL = re.compile(r"^[^:]{1,40}:\s+(?P<rest>.+)$")


def clean_title(title: str) -> str:
    """The appointment's own name, without one reminder's wording ("is three weeks away, on 24
    January. Acknowledge this reminder when seen."). Anything that does not fit stays as it is."""
    text = _ACK_NOTE.sub("", title)
    text = _AWAY.sub("", text, count=1).strip().rstrip(".").strip()
    label = _LABEL.match(text)
    if label is not None and _AWAY.search(title):
        text = label.group("rest")
    text = text[:1].upper() + text[1:] if text else ""
    return text or " ".join(title.split())


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


@dataclass(frozen=True)
class ImportantDate:
    """One appointment with all its reminders."""

    card: str
    card_title: str
    title: str
    start: str
    reminders: tuple[Appointment, ...] = field(default=())

    @property
    def due(self) -> bool:
        return any(r.due for r in self.reminders)

    @property
    def action(self) -> str:
        """The reminder the viewer acknowledges: the latest due one, else the next to come."""
        due = [r for r in self.reminders if r.due]
        if due:
            return max(due, key=lambda r: r.reminder_from or "").action
        return min(self.reminders, key=lambda r: r.reminder_from or r.start).action

    @property
    def next_reminder(self) -> str | None:
        later = [r.reminder_from for r in self.reminders if not r.due and r.reminder_from]
        return min(later) if later else None


def dates(ctx: Context) -> list[ImportantDate]:
    """Important dates that have not started, soonest first, each with its reminders."""
    groups: dict[tuple[str, str, str], list[Appointment]] = {}
    for item in upcoming(ctx):
        key = (item.card, item.start, clean_title(item.title).casefold())
        groups.setdefault(key, []).append(item)
    found = [
        ImportantDate(
            items[0].card,
            items[0].card_title,
            clean_title(items[0].title),
            items[0].start,
            tuple(sorted(items, key=lambda r: r.reminder_from or r.start)),
        )
        for items in groups.values()
    ]
    return sorted(found, key=lambda d: (d.start, d.title))


def acknowledge(ctx: Context, action: str) -> None:
    """The owner acknowledges a waiting reminder. Every reminder of the same date that is due by
    now is finished with it (an earlier one the owner never saw is not asked again); later ones
    stay. The audit says who."""
    group = next((d for d in dates(ctx) for r in d.reminders if r.action == action), None)
    due = [r for r in group.reminders if r.due] if group is not None else []
    if not any(r.action == action for r in due):
        raise ChamberlainError(
            ErrorCode.REQUEST_INVALID,
            "That reminder is not waiting for you any more. Refresh the page.",
        )
    with transaction(ctx.db):
        cards = CardService(ctx.db, ctx.clock)
        for reminder in due:
            cards.finish_action(reminder.action, None, OUTCOME)
            ctx.services.audit.append(
                actor=VIEWER_ACTOR,
                kind="appointment.acknowledged",
                subject=reminder.action,
                summary="The owner acknowledged an important appointment reminder.",
            )
