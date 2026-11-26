"""Pin the public alert names in docs/contracts/07-attention.md."""

from chamberlain.domain.events import EventKind


def test_a1_the_18_event_kinds_have_exact_public_names() -> None:
    # __members__ includes alias names. This contract currently declares no aliases.
    assert {name: kind.value for name, kind in EventKind.__members__.items()} == {
        "MAIL_NEW_UNMATCHED": "mail.new_unmatched",
        "ACTION_DUE": "action.due",
        "REMINDER_DUE": "reminder.due",
        "CARD_REVIEW_DUE": "card.review_due",
        "GRANT_NEEDED": "grant.needed",
        "GRANT_REVIEW_DUE": "grant.review_due",
        "OPERATION_UNCERTAIN": "operation.uncertain",
        "SEND_MISSING": "send.missing",
        "SOURCE_STALE": "source.stale",
        "SOURCE_SIGN_IN_NEEDED": "source.sign_in_needed",
        "CALENDAR_CONFLICT": "calendar.conflict",
        "USER_NOTE": "user.note",
        "STORAGE_OVER_BUDGET": "storage.over_budget",
        "STORAGE_ORPHANS": "storage.orphans",
        "UPDATE_AVAILABLE": "update.available",
        "SCHEDULE_PASS_DUE": "schedule.pass_due",
        "REPORT_DUE": "report.due",
        "HARNESS_REVIEW": "harness.review",
    }
