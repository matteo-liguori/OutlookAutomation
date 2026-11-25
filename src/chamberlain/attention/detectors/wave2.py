"""Detectors fed by the viewer and runs track (wave 2), and the calendar conflict.

`calendar.conflict` reads the owner's events collected from every calendar. `user.note` comes
from the viewer's notes,
`update.available` from the update checker's recorded tag and `schedule.pass_due` from the
schedule record (the runs track's tables, read through `collect`). A detector only reads
`DetectorInput` (A-5).
"""

import hashlib
from collections.abc import Iterable
from datetime import datetime

from chamberlain.attention.models import CalendarSlot, DetectorInput, EventAttrs, EventDraft
from chamberlain.attention.tuning import CONFLICT_MAX, PRIORITY
from chamberlain.domain.events import EventKind
from chamberlain.limits import SUMMARY_MAX

NOTE_PREFIX = "Note from the user: "


def _stored(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _one_each(slots: Iterable[CalendarSlot]) -> list[CalendarSlot]:
    """Sorted by start, with a copy of an event held by two calendars counted once."""
    seen: set[tuple[str, datetime] | str] = set()
    unique: list[CalendarSlot] = []
    for slot in sorted(slots, key=lambda s: (s.start, s.source, s.locator)):
        if (slot.uid, slot.start) in seen or slot.fingerprint in seen:
            continue
        seen.update({(slot.uid, slot.start), slot.fingerprint})
        unique.append(slot)
    return unique


def calendar_conflict(data: DetectorInput) -> Iterable[EventDraft]:
    """One event for each pair of the owner's events that overlap, the earliest few (A-26).

    The line names the time and the calendars, never the titles: those are remote text."""
    slots = _one_each(data.calendar_slots)
    found: list[tuple[CalendarSlot, CalendarSlot]] = []
    for index, first in enumerate(slots):
        for second in slots[index + 1 :]:
            if second.start >= first.end:
                break
            found.append((first, second))
    for first, second in found[:CONFLICT_MAX]:
        begins, ends = second.start, min(first.end, second.end)
        key = f"{first.source}|{first.locator}|{first.start}|{second.source}|{second.locator}"
        where = (
            first.source if first.source == second.source else f"{first.source}, {second.source}"
        )
        yield EventDraft(
            EventKind.CALENDAR_CONFLICT,
            None,
            (
                f"Two events overlap on {begins:%Y-%m-%d} from {begins:%H:%M} to {ends:%H:%M} UTC "
                f"({where})."
            )[:SUMMARY_MAX],
            PRIORITY[EventKind.CALENDAR_CONFLICT],
            EventAttrs(source=first.source, since=_stored(begins), until=_stored(ends)),
            discriminator=hashlib.sha256(f"{key}|{second.start}".encode()).hexdigest()[:12],
        )


def user_note(data: DetectorInput) -> Iterable[EventDraft]:
    """One event per open note; the note is the person's own words, shown as written."""
    for note in data.notes:
        yield EventDraft(
            EventKind.USER_NOTE,
            None,
            (NOTE_PREFIX + note.text)[:SUMMARY_MAX],
            PRIORITY[EventKind.USER_NOTE],
            EventAttrs(),
            discriminator=str(note.id),
        )


def update_available(data: DetectorInput) -> Iterable[EventDraft]:
    if data.update_tag:
        yield EventDraft(
            EventKind.UPDATE_AVAILABLE,
            None,
            f"A newer release, {data.update_tag}, is available. Offer to tell the user.",
            PRIORITY[EventKind.UPDATE_AVAILABLE],
            EventAttrs(),
            discriminator=data.update_tag,
        )


def schedule_pass_due(data: DetectorInput) -> Iterable[EventDraft]:
    """One event for the pass being worked, else for the oldest pass that is due (A-2).

    A second due pass waits its turn, so the session working the first never gets its item.
    """
    running = [run for run in data.runs_due if run.state == "running"]
    waiting = [run for run in data.runs_due if run.state == "due"][:1]
    for run in running or waiting:
        yield EventDraft(
            EventKind.SCHEDULE_PASS_DUE,
            run.handle,
            f"The {run.kind} pass {run.schedule} for {run.day} is due. "
            f"Keep its report under {run.handle}.",
            PRIORITY[EventKind.SCHEDULE_PASS_DUE],
            EventAttrs(reason=run.kind),
        )


def report_due(data: DetectorInput) -> Iterable[EventDraft]:
    """The last item of a pass being worked: its report. It ends when a report is kept."""
    for run in data.runs_due:
        if run.state == "running" and not run.has_report and run.outline is not None:
            yield EventDraft(
                EventKind.REPORT_DUE,
                run.outline,
                f"Last step of the {run.schedule} pass for {run.day}: write its report and "
                f"keep it under {run.handle}. Open this item for what to write.",
                PRIORITY[EventKind.REPORT_DUE],
                EventAttrs(reason=run.kind),
            )


def harness_review(data: DetectorInput) -> Iterable[EventDraft]:
    """One event per open review round; the round ends when the agent handles it (review track).

    It comes after every pass item, so it never delays the work or the report.
    """
    for review in data.reviews:
        yield EventDraft(
            EventKind.HARNESS_REVIEW,
            None,
            "Review how the harness is being used. Open this item for the list to read.",
            PRIORITY[EventKind.HARNESS_REVIEW],
            EventAttrs(),
            discriminator=str(review.id),
        )
