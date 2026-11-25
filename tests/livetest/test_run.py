"""The whole suite, end to end, against the fake world (the gate's self-test).

The test plays the owner: it confirms the grants the way the viewer would. The runner
under test never can (`test_authority.py`).
"""

from tests.livetest.conftest import confirm_all, seed_owner_state

from chamberlain.connectors.fake import Fault
from chamberlain.domain.mail import FolderRole
from chamberlain.livetest.fakeworld import (
    ADDRESS_A,
    ADDRESS_B,
    FAKE_CAL_A,
    FAKE_MAIL_A,
    FAKE_MAIL_B,
    FakeLab,
)
from chamberlain.livetest.markers import carries_marker
from chamberlain.livetest.model import RunReport, Status
from chamberlain.livetest.runner import RunOptions, execute

OWNED = {ADDRESS_A, ADDRESS_B}


def run_all(lab: FakeLab, **options: object) -> RunReport:
    confirm_all(lab)
    return execute(lab.app, lab.env, lab.config, RunOptions(**options))  # type: ignore[arg-type]


def test_the_whole_suite_runs_green_and_leaves_only_the_trash(lab: FakeLab) -> None:
    report = run_all(lab)
    assert [(r.id, r.reason) for r in report.results if r.status is Status.FAIL] == []
    assert report.leftovers == ()
    assert report.score.percent == 100
    assert {r.cleanup for r in report.results} == {"clean"}
    ran = {r.id for r in report.results if r.status is Status.PASS}
    assert len(ran) >= 30
    for needed in (
        "mail.fault_lost_response[mail:laba]",
        "calendar.invite_flow[calendar:laba]",
        "calendar.recurring[calendar:laba]",
        "calendar.recurring_invite[calendar:laba]",
    ):
        assert needed in ran
    skipped = [r for r in report.results if r.status is Status.SKIPPED]
    assert all(r.reason for r in skipped)
    assert {r.id.split("[")[0] for r in skipped} <= {
        "calendar.invite_flow",
        "calendar.recurring_invite",
        "mail.outbox_queued",
    }
    assert next(r for r in skipped if r.id == "mail.outbox_queued").manual

    world = lab.provider.world
    recipients: set[str] = set()
    for source in (FAKE_MAIL_A, FAKE_MAIL_B):
        trash = next(f.id for f in world.folders[source] if f.role is FolderRole.TRASH)
        for message in world.mail[source]:
            assert carries_marker(message.subject), "an unmarked message exists"
            assert message.folder_id == trash, "marked mail is left outside the trash"
            recipients.update(a.address for a in (*message.to, *message.cc))
    assert recipients
    assert recipients <= OWNED
    for events in world.events.values():
        assert all(e.status.value == "cancelled" for e in events.values())


def test_an_owners_existing_state_is_left_alone(lab: FakeLab) -> None:
    owned = seed_owner_state(lab)
    report = run_all(lab)
    assert report.score.percent == 100
    state = lab.app.context.db.execute(
        "SELECT state FROM operations WHERE handle = ?", (owned.foreign_operation,)
    ).fetchone()["state"]
    assert state == "uncertain"
    world = lab.provider.world
    for source, subjects in owned.inbox_subjects.items():
        inbox = [m for m in world.mail[source] if m.folder_id == "f-inbox"]
        assert sorted(m.subject for m in inbox) == sorted(subjects)
        assert all(m.unread for m in inbox)
    live = [
        e.fields.title for e in world.events[FAKE_CAL_A].values() if e.status.value != "cancelled"
    ]
    assert live == [owned.event_title]


def test_a_competing_card_fails_the_suggestion_by_name_and_nothing_else(lab: FakeLab) -> None:
    seed_owner_state(lab, competing_card_to=ADDRESS_B)
    report = run_all(lab)
    failed = {r.id: r.reason for r in report.results if r.status is Status.FAIL}
    assert list(failed) == ["mail.watermark_suggestion[mail:laba>mail:labb]"]
    assert "no suggested card" in failed["mail.watermark_suggestion[mail:laba>mail:labb]"]


def test_dry_run_only_and_skip_choose_scenarios_without_touching_anything(lab: FakeLab) -> None:
    confirm_all(lab)
    before = lab.app.context.db.execute("SELECT count(*) FROM operations").fetchone()[0]
    dry = execute(lab.app, lab.env, lab.config, RunOptions(dry_run=True))
    assert all(r.status is Status.SKIPPED for r in dry.results)
    assert lab.app.context.db.execute("SELECT count(*) FROM operations").fetchone()[0] == before
    assert lab.provider.dispatch_log == []

    chosen = execute(lab.app, lab.env, lab.config, RunOptions(only=("self_send", "doctor")))
    assert {r.id for r in chosen.results} == {
        "mail.self_send[mail:laba]",
        "mail.self_send[mail:labb]",
        "harness.doctor_clean",
    }
    left_out = execute(
        lab.app, lab.env, lab.config, RunOptions(only=("mail.*",), skip=("fault", "repeat"))
    )
    reasons = {r.id: (r.status, r.reason) for r in left_out.results}
    assert reasons["mail.fault_lost_response[mail:laba]"] == (Status.SKIPPED, "skipped by --skip")
    assert reasons["mail.self_send[mail:laba]"][0] is Status.PASS
    assert not any(i.startswith("calendar") for i in reasons)


def test_a_second_run_in_the_same_hour_skips_on_the_send_limit(lab: FakeLab) -> None:
    assert run_all(lab).score.failed == 0
    second = execute(lab.app, lab.env, lab.config, RunOptions())
    assert second.score.failed == 0
    assert any("limit" in r.reason for r in second.results)
    assert second.leftovers == ()


def test_a_missing_arrival_fails_with_a_reason_and_cleanup_still_runs(lab: FakeLab) -> None:
    confirm_all(lab)
    lab.delivery.cut_off(ADDRESS_A)
    config = lab.config.model_copy(update={"arrival_wait_seconds": 6})
    report = execute(lab.app, lab.env, config, RunOptions(only=("self_send[mail:laba]",)))
    [result] = report.results
    assert result.status is Status.FAIL
    assert "did not happen within 6 seconds" in result.reason
    assert result.cleanup == "clean"
    assert (report.leftovers, report.score.percent) == ((), 0)


def test_the_lost_response_scenario_needs_a_hook_that_fires(lab: FakeLab) -> None:
    only = ("fault_lost_response[mail:laba]",)
    report = run_all(lab, only=only)
    assert report.results[0].status is Status.PASS
    sends = [d for d in lab.provider.dispatch_log if d.intent_kind.value == "mail.send"]
    assert [d.applied for d in sends] == [True]
    assert not lab.provider.world.armed(Fault.LOSE_RESPONSE_AFTER_APPLY)

    lab.env.faults.fired = lambda run_id: False  # type: ignore[method-assign]
    unfired = execute(lab.app, lab.env, lab.config, RunOptions(only=only))
    assert unfired.results[0].status is Status.SKIPPED
    assert "restart Thunderbird" in unfired.results[0].reason

    class Unavailable:
        def why_unavailable(self) -> str:
            return "no hook here"

        def arm_lost_response(self, run_id: str) -> None:
            raise AssertionError("must not arm")

        def fired(self, run_id: str) -> bool:
            return False

        def disarm(self) -> None:
            return None

    lab.env.faults = Unavailable()
    refused = execute(lab.app, lab.env, lab.config, RunOptions(only=only))
    assert "no hook here" in refused.results[0].reason


def test_each_scenario_lands_in_the_report_at_once_and_a_retry_runs_only_the_rest(
    lab: FakeLab,
) -> None:
    """Owner, 2026-11-25: a stopped run keeps every result so far; retry only what is left."""
    from chamberlain.livetest.runner import NOT_RUN_YET, retry_ids

    seen: list[RunReport] = []
    report = run_all(lab, only=("mail.self_send", "mail.cross_send"), progress=seen.append)
    assert len(seen) >= 3
    assert not seen[0].finished
    assert all(r.reason == NOT_RUN_YET for r in seen[0].results if r.status is Status.SKIPPED)
    ran = [len([r for r in s.results if r.reason != NOT_RUN_YET]) for s in seen]
    assert ran == sorted(ran)
    assert report.finished
    assert retry_ids(report) == ()

    # A run stopped half-way: the first result passed, the others never ran.
    stopped = seen[1]
    left = retry_ids(stopped)
    assert left
    assert all(r.id in left for r in stopped.results if r.reason == NOT_RUN_YET)
    again = run_all(lab, retry_ids=left)
    assert {r.id for r in again.results} == set(left)
