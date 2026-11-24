"""Numbers of the runs track (the lifetime lines live in docs/contracts/06-memory.md)."""

from datetime import timedelta
from typing import Final

MAX_LATE: Final = timedelta(hours=6)
"""A pass first noticed this long after its time is recorded as missed, not run late."""
RUN_KEEP: Final = timedelta(days=35)
"""A run row is deleted this long after its day; reports have their own `expires_at`."""
REPORT_HOURS: Final = 14 * 24
"""Lifetime of a report the harness itself stores."""
NOTE_OPEN: Final = timedelta(days=7)
NOTE_KEEP: Final = timedelta(days=14)
NOTE_MAX_CHARS: Final = 150
UPDATE_CHECK_EVERY: Final = timedelta(hours=24)
UPDATE_RETRY_AFTER: Final = timedelta(hours=1)
TAG_PREFIX: Final = "chamberlain/v"
WEEKDAYS_ALL: Final = "1,2,3,4,5,6,7"
