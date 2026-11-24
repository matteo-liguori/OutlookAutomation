"""Attention-private numbers: priorities, re-raise intervals, mail lookback.

Contract: docs/contracts/07-attention.md. Numbers the contracts quote live in
`limits.py`; these are this track's own tunables (see the board INTERFACE entry).
"""

from datetime import timedelta
from typing import Final

from chamberlain.domain.events import EventKind

PRIORITY: Final[dict[EventKind, int]] = {
    EventKind.OPERATION_UNCERTAIN: 95,
    EventKind.GRANT_NEEDED: 85,
    EventKind.SOURCE_SIGN_IN_NEEDED: 80,
    EventKind.ACTION_DUE: 75,
    EventKind.STORAGE_OVER_BUDGET: 70,
    EventKind.REMINDER_DUE: 65,
    EventKind.MAIL_NEW_FOR_CARD: 65,
    EventKind.SOURCE_STALE: 60,
    EventKind.GRANT_REVIEW_DUE: 60,
    EventKind.CARD_REVIEW_DUE: 50,
    EventKind.MAIL_NEW_UNMATCHED: 40,
    EventKind.STORAGE_ORPHANS: 30,
    EventKind.CALENDAR_CONFLICT: 60,
    EventKind.USER_NOTE: 70,
    EventKind.UPDATE_AVAILABLE: 10,
    EventKind.SCHEDULE_PASS_DUE: 70,
}

RERAISE: Final[dict[EventKind, timedelta]] = {
    EventKind.OPERATION_UNCERTAIN: timedelta(hours=24),
    EventKind.GRANT_REVIEW_DUE: timedelta(days=7),
}
"""Kinds with a re-raise interval after being handled (A-3). Others never re-raise."""

MAIL_LOOKBACK: Final = timedelta(hours=48)
MAIL_PAGE_LIMIT: Final = 25
SESSIONS_KEEP_ENDED: Final = timedelta(hours=24)
GUIDE_PAGE: Final = 25
