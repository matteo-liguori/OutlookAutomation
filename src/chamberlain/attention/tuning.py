"""Attention-private numbers: priorities, re-raise intervals, mail lookback.

Contract: docs/contracts/07-attention.md. Numbers the contracts quote live in
`limits.py`; these are this track's own tunables (see the board INTERFACE entry).
"""

from datetime import timedelta
from typing import Final

from chamberlain.domain.events import EventKind

PRIORITY: Final[dict[EventKind, int]] = {
    EventKind.OPERATION_UNCERTAIN: 95,
    EventKind.SEND_MISSING: 94,
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
    EventKind.REPORT_DUE: 5,
}

RERAISE: Final[dict[EventKind, timedelta]] = {
    EventKind.OPERATION_UNCERTAIN: timedelta(hours=24),
    EventKind.GRANT_REVIEW_DUE: timedelta(days=7),
}
"""Kinds with a re-raise interval after being handled (A-3). Others never re-raise."""

NOTE_OPEN: Final = timedelta(days=7)
"""A viewer note keeps raising `user.note` this long (runs track `user_notes`)."""
MAIL_BOOTSTRAP: Final = timedelta(hours=48)
"""How far back the first scan of a never-watched source looks; recorded as `watching_since`."""
MAIL_OVERLAP: Final = timedelta(hours=24)
"""Each scan starts this far before the watermark: a message dated earlier than it arrived
(Date headers are the sender's) is still caught. Older-dated late arrivals are the known residue."""
MAIL_REQUIRED_OVERLAP: Final = timedelta(hours=1)
"""The part of the overlap a scan must always finish; the rest is best effort when a mailbox is
larger than one tick's page budget. Mail dated older than this that arrives later may be missed."""
MAIL_PAGE_LIMIT: Final = 200
MAIL_MAX_PAGES: Final = 5
"""Bounds one scan of one source in one tick (A-6); a larger backlog resumes next tick."""
MAIL_HEADER_READS: Final = 20
"""Messages per source and tick that may be read in full to get their reply headers."""
MAIL_CORRESPONDENT_LOOKUPS: Final = 30
SESSIONS_KEEP_ENDED: Final = timedelta(hours=24)
GUIDE_PAGE: Final = 25
