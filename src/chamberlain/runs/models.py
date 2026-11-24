"""Plain rows of the runs track."""

from dataclasses import dataclass
from enum import StrEnum


class RunState(StrEnum):
    DUE = "due"
    RUNNING = "running"
    FINISHED = "finished"
    INCOMPLETE = "incomplete"
    MISSED = "missed"


class PassKind(StrEnum):
    MAIL = "mail"
    CALENDAR = "calendar"


@dataclass(frozen=True)
class ScheduleRow:
    name: str
    kind: PassKind
    at_local: str
    weekdays: frozenset[int]
    zone: str
    enabled: bool


@dataclass(frozen=True)
class RunRow:
    handle: str
    schedule: str
    day: str
    state: RunState
    due_at: str
    started_at: str | None
    finished_at: str | None
    session: str | None


@dataclass(frozen=True)
class NoteRow:
    id: int
    text: str
    state: str
    created_at: str
