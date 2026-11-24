"""Every number the contracts quote, defined once.

Contract: docs/contracts/10-targets.md (and the files it points to). Changing a
value here is a contract change: post it on docs/BOARD.md first.
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
CARD_OPEN_ACTIONS_MAX: Final = 7
ACTION_TITLE_MAX: Final = 160
NOTE_MAX: Final = 1_000
NOTES_PER_ACTION_MAX: Final = 5
EVIDENCE_MAX: Final = 2_000
EVIDENCE_PER_OPEN_CARD_TARGET: Final = 15
OUTCOME_MAX: Final = 240
REASON_MAX: Final = 120

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
OPERATION_OWNER_GRACE_HOURS: Final = 24
GRANT_COMPACT_DAYS: Final = 90
AUDIT_DAYS: Final = 730
BACKUP_DAILY_SLOTS: Final = 7
BACKUP_WEEKLY_SLOTS: Final = 4
LOG_DAYS: Final = 14
LOG_MAX_BYTES: Final = 20 * 1024 * 1024

# --- Writes and grants (04-writes.md, 05-grants.md) -------------------------
SETTLE_AFTER_MINUTES: Final = 10
REPEAT_WINDOW_HOURS: Final = 24
SENDS_PER_HOUR: Final = 20
MARK_READ_MAX: Final = 200
ONE_OFF_GRANT_DAYS: Final = 7
STANDING_GRANT_MAX_DAYS: Final = 365
PROPOSAL_LAPSE_DAYS: Final = 7
REVIEW_GRACE_DAYS: Final = 14
