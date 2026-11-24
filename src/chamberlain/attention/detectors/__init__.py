"""One pure detector per event kind.

Contract: docs/contracts/07-attention.md (A-5, A-8). A detector reads; it
never writes and never calls a connector.
"""

from collections.abc import Callable, Iterable

from chamberlain.attention.detectors import due, grants, mail, operations, sources, storage, wave2
from chamberlain.attention.models import DetectorInput, EventDraft
from chamberlain.domain.events import EventKind

Detector = Callable[[DetectorInput], Iterable[EventDraft]]

DETECTORS: dict[EventKind, Detector] = {
    EventKind.MAIL_NEW_FOR_CARD: mail.new_for_card,
    EventKind.MAIL_NEW_UNMATCHED: mail.new_unmatched,
    EventKind.ACTION_DUE: due.action_due,
    EventKind.REMINDER_DUE: due.reminder_due,
    EventKind.CARD_REVIEW_DUE: due.card_review_due,
    EventKind.GRANT_NEEDED: grants.grant_needed,
    EventKind.GRANT_REVIEW_DUE: grants.grant_review_due,
    EventKind.OPERATION_UNCERTAIN: operations.operation_uncertain,
    EventKind.SEND_MISSING: operations.send_missing,
    EventKind.SOURCE_STALE: sources.source_stale,
    EventKind.SOURCE_SIGN_IN_NEEDED: sources.source_sign_in_needed,
    EventKind.CALENDAR_CONFLICT: wave2.calendar_conflict,
    EventKind.USER_NOTE: wave2.user_note,
    EventKind.STORAGE_OVER_BUDGET: storage.over_budget,
    EventKind.STORAGE_ORPHANS: storage.orphans,
    EventKind.UPDATE_AVAILABLE: wave2.update_available,
    EventKind.SCHEDULE_PASS_DUE: wave2.schedule_pass_due,
    EventKind.REPORT_DUE: wave2.report_due,
    EventKind.HARNESS_REVIEW: wave2.harness_review,
}
"""Exactly one detector for every `EventKind` (A-5)."""
