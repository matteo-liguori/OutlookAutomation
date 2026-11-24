"""Registered detectors that report nothing yet.

Wave 2 fills these: `calendar.conflict` needs calendar deltas, `user.note` comes
from the viewer, `update.available` from the updater, `schedule.pass_due` from
the schedule record. Until then they return no events (07-attention.md).
"""

from collections.abc import Iterable

from chamberlain.attention.models import DetectorInput, EventDraft


def calendar_conflict(data: DetectorInput) -> Iterable[EventDraft]:
    return ()


def user_note(data: DetectorInput) -> Iterable[EventDraft]:
    return ()


def update_available(data: DetectorInput) -> Iterable[EventDraft]:
    return ()


def schedule_pass_due(data: DetectorInput) -> Iterable[EventDraft]:
    return ()
