"""Event kinds and states.

Contract: docs/contracts/07-attention.md (Events).
"""

from enum import StrEnum


class EventKind(StrEnum):
    MAIL_NEW_UNMATCHED = "mail.new_unmatched"
    ACTION_DUE = "action.due"
    REMINDER_DUE = "reminder.due"
    CARD_REVIEW_DUE = "card.review_due"
    GRANT_NEEDED = "grant.needed"
    GRANT_REVIEW_DUE = "grant.review_due"
    OPERATION_UNCERTAIN = "operation.uncertain"
    SEND_MISSING = "send.missing"
    SOURCE_STALE = "source.stale"
    SOURCE_SIGN_IN_NEEDED = "source.sign_in_needed"
    CALENDAR_CONFLICT = "calendar.conflict"
    USER_NOTE = "user.note"
    STORAGE_OVER_BUDGET = "storage.over_budget"
    STORAGE_ORPHANS = "storage.orphans"
    UPDATE_AVAILABLE = "update.available"
    SCHEDULE_PASS_DUE = "schedule.pass_due"
    REPORT_DUE = "report.due"
    HARNESS_REVIEW = "harness.review"


class EventState(StrEnum):
    NEW = "new"
    DELIVERED = "delivered"
    HANDLED = "handled"
    EXPIRED = "expired"


NOT_AGENT_HANDLEABLE = frozenset({EventKind.SOURCE_SIGN_IN_NEEDED})
"""Events that close only when the condition ends (A-4)."""
