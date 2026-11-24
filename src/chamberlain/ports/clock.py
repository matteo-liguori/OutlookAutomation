"""The only place that reads the system time.

Contract: docs/contracts/01-domain.md (D-7, D-8).
"""

from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """The current time: aware, UTC, second precision."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC).replace(microsecond=0)


class ManualClock:
    """A clock that moves only when told to. For tests and the fake provider."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = (start or datetime(2026, 1, 5, 9, 0, tzinfo=UTC)).astimezone(UTC)

    def now(self) -> datetime:
        return self._now.replace(microsecond=0)

    def advance(self, *, seconds: float = 0, minutes: float = 0, hours: float = 0) -> None:
        self._now += timedelta(seconds=seconds, minutes=minutes, hours=hours)

    def set(self, moment: datetime) -> None:
        self._now = moment.astimezone(UTC)
