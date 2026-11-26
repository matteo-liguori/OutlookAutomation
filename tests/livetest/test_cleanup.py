"""Cleanup: `--keep` leaves items, `sweep` removes only marked ones, calendar needs approval."""

from datetime import UTC, datetime

import pytest
from tests.livetest.conftest import confirm_all

from chamberlain.connectors.fake import Fault
from chamberlain.domain.calendar import EventFields
from chamberlain.domain.mail import FolderRole
from chamberlain.domain.operations import Prepared, ReconcileResult, ReconcileStatus
from chamberlain.livetest.env import MailCopy
from chamberlain.livetest.fakeworld import FAKE_CAL_A, FAKE_MAIL_A, FakeLab
from chamberlain.livetest.lab import ScenarioFailure
from chamberlain.livetest.markers import carries_marker
from chamberlain.livetest.registry import Target
from chamberlain.livetest.runner import RunOptions, execute, open_lab
from chamberlain.livetest.scenarios_harness import doctor_clean, doctor_states
from chamberlain.livetest.sweep import sweep
from chamberlain.writes.core import MAIL_SETTLE_ATTEMPTS

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


@pytest.mark.parametrize("already_uncertain", [False, True])
def test_cleanup_confirms_a_late_trash_by_reading_the_same_operation(
    lab: FakeLab, monkeypatch: pytest.MonkeyPatch, already_uncertain: bool
) -> None:
    confirm_all(lab)
    runner = open_lab(lab.app, lab.env, lab.config, OLD_RUN, RunOptions())
    lab.provider.mail.add_message(
        FAKE_MAIL_A,
        internet_message_id="<late-trash@example.test>",
        subject=runner.subject("late trash"),
    )
    lab.provider.arm(Fault.LOSE_RESPONSE_AFTER_APPLY)
    reconcile = lab.provider.mail.reconcile
    reads = 0

    def delayed_read(prepared: Prepared, key: str) -> ReconcileResult:
        nonlocal reads
        reads += 1
        if reads <= MAIL_SETTLE_ATTEMPTS:
            return ReconcileResult(status=ReconcileStatus.UNKNOWN)
        return reconcile(prepared, key)

    monkeypatch.setattr(lab.provider.mail, "reconcile", delayed_read)

    if already_uncertain:
        copies = runner.copies
        [copy] = copies(FAKE_MAIL_A, runner.marker)
        operation = runner.prepare(
            {"kind": "mail.trash", "source": FAKE_MAIL_A, "messages": [runner.handle_of(copy)]}
        )
        assert runner.do(operation).code == "operation.uncertain"
        # Cleanup can still be working from a folder observation taken before the trash landed.
        prior = iter([[copy]])

        def stale_on_first_read(source: str, text: str) -> list[MailCopy]:
            observed = next(prior, None)
            return observed if observed is not None else copies(source, text)

        monkeypatch.setattr(runner, "copies", stale_on_first_read)
    assert runner.remove_mail(FAKE_MAIL_A, runner.marker) == 1
    assert reads == MAIL_SETTLE_ATTEMPTS + 1
    assert [dispatch.intent_kind.value for dispatch in lab.provider.dispatch_log] == ["mail.trash"]
    assert not live_messages(lab, FAKE_MAIL_A)


def test_events_are_swept_only_with_approval_and_foreign_ones_never(lab: FakeLab) -> None:
    confirm_all(lab)
    start = datetime(2026, 1, 7, 10, 0, tzinfo=UTC)
    for title in (f"CHAMBERLAIN LIVE TEST {OLD_RUN} crud x", "Dentist (CHAMBERLAIN LIVE TEST)"):
        lab.provider.calendar.add_event(
            FAKE_CAL_A, EventFields(title=title, start=start, end=start)
        )
    series = EventFields.model_validate(
        {
            "title": f"CHAMBERLAIN LIVE TEST {OLD_RUN} series x",
            "start": start,
            "end": start,
            "recurrence": {"frequency": "daily", "count": 3, "zone": "UTC"},
        }
    )
    lab.provider.calendar.add_event(FAKE_CAL_A, series)  # three dates, swept as one series
    events = lab.provider.world.events[FAKE_CAL_A]

    def live() -> list[str]:
        return [e.fields.title for e in events.values() if e.status.value != "cancelled"]

    off = lab.config.model_copy(update={"approve_calendar_edits": False})
    left = sweep(lab.app, lab.env, off, RunOptions())
    assert left.events_removed == 0
    assert any("not swept" in note for note in left.notes)
    assert len(live()) == 3

    done = sweep(lab.app, lab.env, lab.config, RunOptions())
    assert done.events_removed == 2
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


def test_final_doctor_settles_delayed_trash_only_when_the_provider_can_prove_it(
    lab: FakeLab, monkeypatch: pytest.MonkeyPatch
) -> None:
    confirm_all(lab)
    runner = open_lab(lab.app, lab.env, lab.config, OLD_RUN, RunOptions())
    runner.baseline = doctor_states(runner)
    lab.provider.mail.add_message(
        FAKE_MAIL_A,
        internet_message_id="<doctor-late@example.test>",
        subject=runner.subject("late"),
    )
    [copy] = runner.copies(FAKE_MAIL_A, runner.marker)
    op = runner.prepare(
        {"kind": "mail.trash", "source": FAKE_MAIL_A, "messages": [runner.handle_of(copy)]}
    )
    lab.provider.arm(Fault.LOSE_RESPONSE_AFTER_APPLY)
    with monkeypatch.context() as patch:
        patch.setattr(
            lab.provider.mail,
            "reconcile",
            lambda prepared, key: ReconcileResult(status=ReconcileStatus.UNKNOWN),
        )
        assert runner.do(op).code == "operation.uncertain"
        with pytest.raises(ScenarioFailure, match=op):
            doctor_clean(runner, Target())
    doctor_clean(runner, Target())
    assert runner.ok("open", handle=op)["view"]["state"] == "confirmed"
    assert [d.intent_kind.value for d in lab.provider.dispatch_log] == ["mail.trash"]
