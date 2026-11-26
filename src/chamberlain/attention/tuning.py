"""Attention-private numbers: priorities, re-raise intervals, mail lookback.

Contract: docs/contracts/07-attention.md. Numbers the contracts quote live in
`limits.py`; these are this track's own tunables (see the board INTERFACE entry).
"""

from datetime import timedelta
from typing import Final

from chamberlain.domain.events import EventKind
from chamberlain.domain.mail import FolderRole

PRIORITY: Final[dict[EventKind, int]] = {
    EventKind.OPERATION_UNCERTAIN: 95,
    EventKind.SEND_MISSING: 94,
    EventKind.GRANT_NEEDED: 85,
    EventKind.SOURCE_SIGN_IN_NEEDED: 80,
    EventKind.ACTION_DUE: 75,
    EventKind.STORAGE_OVER_BUDGET: 70,
    EventKind.REMINDER_DUE: 65,
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
    EventKind.HARNESS_REVIEW: 4,
}

RERAISE: Final[dict[EventKind, timedelta]] = {
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
MAIL_STANDARD: Final = timedelta(hours=24)
"""Mail up to this far behind now is read without asking. A longer uncovered span is read for the
most recent day only; the older part is held until the owner says yes (A-23)."""
MAIL_PAGE_LIMIT: Final = 200
MAIL_MAX_PAGES: Final = 5
"""Bounds one scan of one source in one tick (A-6); a larger backlog resumes next tick, oldest
mail first, in time slices that shrink until each one fits (A-20)."""
MAIL_ESTIMATE_PAGES: Final = 3
"""Pages read to estimate how much mail an older span holds; the rest is extrapolated."""
MAIL_SENT_KEEP: Final = timedelta(days=7)
"""A quiet "you sent this" line nobody handled is dropped after this long."""
MAIL_SENT_PRIORITY: Final = 25
"""Mail the owner sent that matches a card: a quiet line, never a triage item by itself."""
MAIL_HEADER_READS: Final = 20
"""Messages per source and tick that may be read in full to get their reply headers."""
MAIL_CORRESPONDENT_LOOKUPS: Final = 30
MAIL_SKIPPED_ROLES: Final = frozenset({FolderRole.TRASH, FolderRole.JUNK, FolderRole.DRAFTS})
"""Folders whose mail the owner does not want triaged, so the scan does not read them and the
harness does not sync them. Outbox is not part of the mailbox."""
SYNC_FRESH: Final = timedelta(minutes=10)
"""A folder synced longer ago than this is synced again before the scan or the agent reads it
(A-24). The owner can change it in the profile (`mail_sync.fresh_minutes`)."""
SYNC_LIMIT: Final = timedelta(seconds=300)
"""The whole sync of one source gives up after this long and goes on with the folders that
finished (A-25). Profile: `mail_sync.time_limit_seconds`. Measured on the owner's mailboxes
(2026-11-24): a whole mailbox normally syncs in 2 to 19 s, one slowdown took 132 s; a sync that
finishes returns at once, so this only bounds a stuck folder."""
CONFLICT_WINDOW: Final = timedelta(days=14)
"""How far ahead the tick looks for two of the owner's events that overlap (A-26)."""
CONFLICT_PAGES: Final = 4
CONFLICT_PAGE_LIMIT: Final = 250
"""Bounds one calendar's read in one tick; a calendar with more events than this in the window
counts as not fully read, so its conflicts are neither raised nor cleared."""
CONFLICT_MAX: Final = 10
"""At most this many overlaps are reported at once, the earliest first."""
SESSIONS_KEEP_ENDED: Final = timedelta(hours=24)
GUIDE_PAGE: Final = 25
