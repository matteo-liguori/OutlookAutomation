"""Detectors fed by the viewer and runs track (wave 2), plus the calendar one still empty.

`calendar.conflict` still needs calendar deltas. `user.note` comes from the viewer's notes,
`update.available` from the update checker's recorded tag and `schedule.pass_due` from the
schedule record (the runs track's tables, read through `collect`). A detector only reads
`DetectorInput` (A-5).
"""

from collections.abc import Iterable

from chamberlain.attention.models import DetectorInput, EventAttrs, EventDraft
from chamberlain.attention.tuning import PRIORITY
from chamberlain.domain.events import EventKind
from chamberlain.limits import SUMMARY_MAX

NOTE_PREFIX = "Note from the user: "


def calendar_conflict(data: DetectorInput) -> Iterable[EventDraft]:
    return ()


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
    """One event while a pass is due or running; it ends when the run does (A-2)."""
    for run in data.runs_due:
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
