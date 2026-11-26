"""Every number the contracts quote, defined once.

Contract: docs/contracts/10-targets.md (and the files it points to). Changing a
value here is a contract change; update its contract and proving tests together.
"""

from typing import Final

# --- Principle 1: attention (07-attention.md, 02-tools.md) -------------------
TOOL_COUNT: Final = 12
STANDING_PROMPT_MAX: Final = 400
TOOL_DESCRIPTION_MAX: Final = 200
RULE_MAX_CHARS: Final = 600
GUIDANCE_TOTAL_CHARS: Final = 12_000
GUIDANCE_REPLY_CHARS: Final = 800
GUIDANCE_POINTER_COST: Final = 60
GUIDANCE_POINTERS_MAX: Final = 3
NOTICES_MAX: Final = 3
NEXT_STEPS_MAX: Final = 3
NOTICE_PRIORITY: Final = 80
START_WORK_ITEMS: Final = 5
NEXT_WORK_ITEMS_MAX: Final = 10
TICK_MIN_INTERVAL_SECONDS: Final = 60
SESSION_IDLE_HOURS: Final = 4

# --- Envelope (03-envelope-errors.md) ---------------------------------------
MAX_REPLY_CHARS: Final = 6_000
PAGE_CHARS: Final = 4_000
SUMMARY_MAX: Final = 160
WHY_MAX: Final = 120
FIX_MAX: Final = 240
HIT_LINE_MAX: Final = 200
FIND_LIMIT_MAX: Final = 25

# --- Principle 2: memory (06-memory.md) -------------------------------------
CARD_TITLE_MAX: Final = 120
CARD_GOAL_MAX: Final = 400
CARD_POSITION_MAX: Final = 1_500
CARD_RULES_MAX: Final = 600
CARD_MAX_CHARS: Final = 3_000
COMPLEX_POSITION_MAX: Final = 4_000
COMPLEX_CARD_MAX_CHARS: Final = 6_000
COMPLEX_DECLINE_DAYS: Final = 30
"""After the owner declines a larger limit, the agent may not ask again for that card this long."""
COMPLEX_REASON_MAX: Final = 240
IMPORTED_CONTEXT_DAYS: Final = 60
"""Context carried over from the old harness expires on its own after this many days."""
CARD_OPEN_ACTIONS_MAX: Final = 7
ACTION_TITLE_MAX: Final = 160
NOTE_MAX: Final = 1_000
NOTES_PER_ACTION_MAX: Final = 5
EVIDENCE_MAX: Final = 2_000
EVIDENCE_PER_OPEN_CARD_TARGET: Final = 15
OUTCOME_MAX: Final = 240
REASON_MAX: Final = 120

ATTACHMENT_TEXT_MAX_BYTES: Final = 10 * 1024 * 1024
"""The largest attachment `open` will read for text; bigger ones can be leased as a file."""
ATTACHMENT_TEXT_MAX_PAGES: Final = 100
"""PDF pages read for text. A longer file is cut short and says so."""
ATTACHMENT_TEXT_MAX_CHARS: Final = 400_000
"""Characters of extracted text kept per attachment; the rest is cut and the reply says so."""
ARTIFACT_DEFAULT_DAYS: Final = 30
ARTIFACT_MAX_DAYS: Final = 180
SESSION_ARTIFACT_HOURS: Final = 24
IMPORT_MAX_BYTES: Final = 25 * 1024 * 1024
CARD_ARTIFACT_BYTES: Final = 20 * 1024 * 1024
TOTAL_ARTIFACT_BYTES: Final = 500 * 1024 * 1024
LEASE_MINUTES: Final = 30
CURSOR_HOURS: Final = 2
REF_DAYS: Final = 30
NOTE_IDLE_DAYS: Final = 30
EVENT_TERMINAL_DAYS: Final = 14
QUARANTINE_DAYS: Final = 14
CARD_OUTCOME_DAYS: Final = 365
ACTION_OUTCOME_DAYS: Final = 90
OPERATION_COMPACT_DAYS: Final = 30
UNCERTAIN_ABANDON_DAYS: Final = 14
"""An uncertain operation nothing can resend is closed unsettled after this many days.
Never applies to a send the harness holds the text of (W-40)."""
UNCERTAIN_SEND_RECHECK_HOURS: Final = 24
"""An uncertain mail send is looked for once more after this long (W-37)."""
OPERATION_OWNER_GRACE_HOURS: Final = 24
GRANT_COMPACT_DAYS: Final = 90
AUDIT_DAYS: Final = 730
BACKUP_DAILY_SLOTS: Final = 2
BACKUP_WEEKLY_SLOTS: Final = 2
LOG_DAYS: Final = 14
LOG_MAX_BYTES: Final = 20 * 1024 * 1024
EXPORT_DAYS: Final = 7
"""Exports (`exports/`, owner utilities) are deleted by the sweeper after this many days."""
EXPORT_MAX_BYTES: Final = 200 * 1024 * 1024
"""The attachments one export may hold."""

# --- Writes and grants (04-writes.md, 05-grants.md) -------------------------
SETTLE_AFTER_MINUTES: Final = 10
CONNECTOR_DEADLINE_SECONDS: Final = 30
"""The longest default write deadline of any connector (08-ports.md, P-17)."""
ABSENCE_MARGIN_SECONDS: Final = 120
"""Safety margin after the deadline: a timed-out write may land late (W-12)."""
REPEAT_WINDOW_HOURS: Final = 24
SENDS_PER_HOUR: Final = 200
"""Sends per mailbox per hour (owner, 2026-11-25: 20 was too low). Mail only to the owner's
own addresses is not capped, so a live test never hits it."""
MARK_READ_MAX: Final = 200
MOVE_BATCH_MAX: Final = 500
"""Messages one `mail.move` or `mail.trash` write moves together (one client call, one check)."""
ONE_OFF_GRANT_DAYS: Final = 7
STANDING_GRANT_MAX_DAYS: Final = 365
PROPOSAL_LAPSE_DAYS: Final = 7
REVIEW_GRACE_DAYS: Final = 14

# --- Recurring events (08-ports.md P-23 to P-27) ------------------------------
RECURRENCE_INTERVAL_MAX: Final = 99
RECURRENCE_COUNT_MAX: Final = 1_000
RECURRENCE_ZONE_MAX: Final = 64
RECURRENCE_EXPAND_MAX: Final = 500
"""Most occurrences one window read may expand from a single series."""

FIND_MAIL_DAYS: Final = 90
"""A mail `find` without `since` searches this many days back. Thunderbird lists a folder oldest
first and the add-on scans at most a bounded number of messages, so an unbounded search of a
large mailbox missed recent mail or timed out (found live, 2026-11-25)."""
