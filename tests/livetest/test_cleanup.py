"""Cleanup: `--keep` leaves items, `sweep` removes only marked ones, calendar needs approval."""

from datetime import UTC, datetime

from tests.livetest.conftest import confirm_all

from chamberlain.domain.calendar import EventFields
from chamberlain.domain.mail import FolderRole
from chamberlain.livetest.fakeworld import FAKE_CAL_A, FAKE_MAIL_A, FakeLab
from chamberlain.livetest.markers import carries_marker
from chamberlain.livetest.runner import RunOptions, execute
from chamberlain.livetest.sweep import sweep

LOOKALIKES = (
    "Meeting about CHAMBERLAIN LIVE TEST 20260105T090000-ab12 plans",
    "CHAMBERLAIN LIVE TEST notes",
    "chamberlain live test 20260105T090000-ab12 (lower case, no marker)",
    "Re: lunch on Friday",
)
OLD_RUN = "20251101T101010-0f0f"


def live_messages(lab: FakeLab, source: str) -> list[str]:
    """Subjects outside the trash."""
    world = lab.provider.world
    trash = next(f.id for f in world.folders[source] if f.role is FolderRole.TRASH)
    return [m.subject for m in world.mail[source] if m.folder_id != trash]


def test_keep_leaves_items_and_sweep_removes_only_marked_mail(lab: FakeLab) -> None:
    without_grant = sweep(lab.app, lab.env, lab.config, RunOptions())
    assert "No confirmed live-test grant" in without_grant.stopped

    confirm_all(lab)
    for number, subject in enumerate(LOOKALIKES, start=1):
        lab.provider.mail.add_message(
            FAKE_MAIL_A, internet_message_id=f"<look{number}@example.test>", subject=subject
        )
    lab.provider.mail.add_message(
        FAKE_MAIL_A,
        internet_message_id="<left@example.test>",
        subject=f"Re: CHAMBERLAIN LIVE TEST {OLD_RUN} reply_thread x",
    )
    report = execute(lab.app, lab.env, lab.config, RunOptions(only=("self_send",), keep=True))
    assert report.kept
    assert {r.cleanup for r in report.results} == {"kept"}
    assert len([s for s in live_messages(lab, FAKE_MAIL_A) if carries_marker(s)]) >= 3

    lab.env.faults.arm_lost_response(OLD_RUN)
    result = sweep(lab.app, lab.env, lab.config, RunOptions())
    assert result.mail_removed >= 3
    assert not result.problems
    assert sorted(live_messages(lab, FAKE_MAIL_A)) == sorted(LOOKALIKES)
    assert lab.env.faults.fired(OLD_RUN)  # a pending fault was disarmed


def test_events_are_swept_only_with_approval_and_foreign_ones_never(lab: FakeLab) -> None:
    confirm_all(lab)
    start = datetime(2026, 1, 7, 10, 0, tzinfo=UTC)
    for title in (f"CHAMBERLAIN LIVE TEST {OLD_RUN} crud x", "Dentist (CHAMBERLAIN LIVE TEST)"):
        lab.provider.calendar.add_event(
            FAKE_CAL_A, EventFields(title=title, start=start, end=start)
        )
    events = lab.provider.world.events[FAKE_CAL_A]

    def live() -> list[str]:
        return [e.fields.title for e in events.values() if e.status.value != "cancelled"]

    off = lab.config.model_copy(update={"approve_calendar_edits": False})
    left = sweep(lab.app, lab.env, off, RunOptions())
    assert left.events_removed == 0
    assert any("not swept" in note for note in left.notes)
    assert len(live()) == 2

    done = sweep(lab.app, lab.env, lab.config, RunOptions())
    assert done.events_removed == 1
    assert live() == ["Dentist (CHAMBERLAIN LIVE TEST)"]


def test_calendar_approval_is_the_owners_setting_or_option_and_is_recorded(lab: FakeLab) -> None:
    confirm_all(lab)
    off = lab.config.model_copy(update={"approve_calendar_edits": False})
    skipped = execute(lab.app, lab.env, off, RunOptions(only=("calendar.",)))
    assert all(r.status.value == "skipped" for r in skipped.results)
    assert any("approval" in r.reason for r in skipped.results)
    query = "SELECT text FROM evidence WHERE text LIKE '%live test%'"
    assert lab.app.context.db.execute(query).fetchall() == []  # nothing approved on its own

    one = ("all_day[calendar:laba]",)
    by_option = execute(lab.app, lab.env, off, RunOptions(only=one, approve_calendar_edits=True))
    assert by_option.results[0].status.value == "pass"
    by_setting = execute(lab.app, lab.env, lab.config, RunOptions(only=one))
    texts = [row["text"] for row in lab.app.context.db.execute(query)]
    assert len(texts) == 4  # a create and a delete per run
    assert "--approve-calendar-edits" in texts[0]
    assert by_option.run_id in texts[0]
    assert "live_test.approve_calendar_edits" in texts[-1]
    assert by_setting.run_id in texts[-1]
