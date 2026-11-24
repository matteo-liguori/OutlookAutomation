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


def live_messages(lab: FakeLab, source: str) -> list[str]:
    """Subjects outside the trash."""
    world = lab.provider.world
    trash = next(f.id for f in world.folders[source] if f.role is FolderRole.TRASH)
    return [m.subject for m in world.mail[source] if m.folder_id != trash]


def seed_lookalikes(lab: FakeLab) -> None:
    for number, subject in enumerate(LOOKALIKES, start=1):
        lab.provider.mail.add_message(
            FAKE_MAIL_A, internet_message_id=f"<look{number}@example.test>", subject=subject
        )


def test_keep_leaves_the_items_and_sweep_removes_only_marked_ones(lab: FakeLab) -> None:
    confirm_all(lab)
    seed_lookalikes(lab)
    report = execute(lab.app, lab.env, lab.config, RunOptions(only=("self_send",), keep=True))
    assert report.kept
    assert {r.cleanup for r in report.results} == {"kept"}
    marked = [s for s in live_messages(lab, FAKE_MAIL_A) if carries_marker(s)]
    assert marked

    result = sweep(lab.app, lab.env, lab.config, RunOptions())
    assert result.mail_removed >= 2
    assert not result.problems
    assert not [s for s in live_messages(lab, FAKE_MAIL_A) if carries_marker(s)]
    assert sorted(live_messages(lab, FAKE_MAIL_A)) == sorted(LOOKALIKES)


def test_sweep_finds_marked_mail_of_a_run_that_crashed(lab: FakeLab) -> None:
    confirm_all(lab)
    lab.provider.mail.add_message(
        FAKE_MAIL_A,
        internet_message_id="<left@example.test>",
        subject="Re: CHAMBERLAIN LIVE TEST 20251101T101010-0f0f reply_thread x",
    )
    result = sweep(lab.app, lab.env, lab.config, RunOptions())
    assert result.mail_removed == 1
    assert live_messages(lab, FAKE_MAIL_A) == []


def test_sweep_stops_without_a_confirmed_grant(lab: FakeLab) -> None:
    lab.provider.mail.add_message(
        FAKE_MAIL_A,
        internet_message_id="<left@example.test>",
        subject="CHAMBERLAIN LIVE TEST 20251101T101010-0f0f x",
    )
    result = sweep(lab.app, lab.env, lab.config, RunOptions())
    assert "No confirmed live-test grant" in result.stopped
    assert len(live_messages(lab, FAKE_MAIL_A)) == 1


def test_sweep_disarms_a_pending_fault(lab: FakeLab) -> None:
    confirm_all(lab)
    lab.env.faults.arm_lost_response("20251101T101010-0f0f")
    sweep(lab.app, lab.env, lab.config, RunOptions())
    assert lab.env.faults.fired("20251101T101010-0f0f")


def test_calendar_events_are_swept_only_with_the_owners_approval(lab: FakeLab) -> None:
    confirm_all(lab)
    start = datetime(2026, 1, 7, 10, 0, tzinfo=UTC)
    lab.provider.calendar.add_event(
        FAKE_CAL_A,
        EventFields(
            title="CHAMBERLAIN LIVE TEST 20251101T101010-0f0f crud x", start=start, end=start
        ),
    )
    events = lab.provider.world.events[FAKE_CAL_A]
    assert any(e.status.value != "cancelled" for e in events.values())

    off = lab.config.model_copy(update={"approve_calendar_edits": False})
    left = sweep(lab.app, lab.env, off, RunOptions())
    assert left.events_removed == 0
    assert any("not swept" in note for note in left.notes)
    assert any(e.status.value != "cancelled" for e in events.values())

    done = sweep(lab.app, lab.env, lab.config, RunOptions())
    assert done.events_removed == 1
    assert all(e.status.value == "cancelled" for e in events.values())


def test_a_foreign_event_is_never_touched(lab: FakeLab) -> None:
    confirm_all(lab)
    start = datetime(2026, 1, 7, 10, 0, tzinfo=UTC)
    lab.provider.calendar.add_event(
        FAKE_CAL_A,
        EventFields(title="Dentist (CHAMBERLAIN LIVE TEST mention)", start=start, end=start),
    )
    sweep(lab.app, lab.env, lab.config, RunOptions())
    live = [
        e for e in lab.provider.world.events[FAKE_CAL_A].values() if e.status.value != "cancelled"
    ]
    assert len(live) == 1


def test_calendar_scenarios_skip_without_approval_and_record_no_evidence(lab: FakeLab) -> None:
    confirm_all(lab)
    off = lab.config.model_copy(update={"approve_calendar_edits": False})
    report = execute(lab.app, lab.env, off, RunOptions(only=("calendar.",)))
    reasons = {r.id: r.reason for r in report.results}
    assert "approval" in reasons["calendar.all_day[calendar:laba]"]
    assert all(r.status.value == "skipped" for r in report.results)
    count = lab.app.context.db.execute(
        "SELECT count(*) FROM evidence WHERE text LIKE '%live test%'"
    ).fetchone()[0]
    assert count == 0


def test_approval_evidence_names_the_setting_and_the_run(lab: FakeLab) -> None:
    confirm_all(lab)
    report = execute(lab.app, lab.env, lab.config, RunOptions(only=("all_day[calendar:laba]",)))
    texts = [
        row["text"]
        for row in lab.app.context.db.execute(
            "SELECT text FROM evidence WHERE text LIKE '%live test%'"
        )
    ]
    assert texts
    assert all("live_test.approve_calendar_edits" in t and report.run_id in t for t in texts)


def test_the_option_approves_one_run_and_says_so(lab: FakeLab) -> None:
    confirm_all(lab)
    off = lab.config.model_copy(update={"approve_calendar_edits": False})
    report = execute(
        lab.app,
        lab.env,
        off,
        RunOptions(only=("all_day[calendar:laba]",), approve_calendar_edits=True),
    )
    assert report.results[0].status.value == "pass"
    text = lab.app.context.db.execute(
        "SELECT text FROM evidence WHERE text LIKE '%live test%'"
    ).fetchone()["text"]
    assert "--approve-calendar-edits" in text
