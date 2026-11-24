"""The whole suite, end to end, against the fake world (the gate's self-test).

The test plays the owner: it confirms the grants the way the viewer would. The runner
under test never can (`test_layering.py`).
"""

from tests.livetest.conftest import confirm_all

from chamberlain.connectors.fake import Fault
from chamberlain.domain.mail import FolderRole
from chamberlain.livetest.fakeworld import ADDRESS_A, ADDRESS_B, FAKE_MAIL_A, FAKE_MAIL_B, FakeLab
from chamberlain.livetest.markers import carries_marker
from chamberlain.livetest.model import RunReport, Status
from chamberlain.livetest.runner import RunOptions, execute

OWNED = {ADDRESS_A, ADDRESS_B}
EXPECTED_SKIPS = {"calendar.recurring", "calendar.invite_flow", "mail.outbox_queued"}


def run_all(lab: FakeLab, **options: object) -> RunReport:
    confirm_all(lab)
    return execute(lab.app, lab.env, lab.config, RunOptions(**options))  # type: ignore[arg-type]


def by_id(report: RunReport) -> dict[str, tuple[Status, str]]:
    return {r.id: (r.status, r.reason) for r in report.results}


def test_the_whole_suite_runs_green_on_the_fake_world(lab: FakeLab) -> None:
    report = run_all(lab)
    failed = [(r.id, r.reason) for r in report.results if r.status is Status.FAIL]
    assert not failed
    assert report.leftovers == ()
    assert report.score.failed == 0
    assert report.score.percent == 100
    assert {r.cleanup for r in report.results} == {"clean"}
    ran = {r.id for r in report.results if r.status is Status.PASS}
    for needed in (
        "mail.self_send[mail:laba]",
        "mail.cross_send[mail:laba>mail:labb]",
        "mail.reply_thread[mail:labb>mail:laba]",
        "mail.draft_flow[mail:laba]",
        "mail.attachment[mail:labb]",
        "mail.read_state[mail:laba]",
        "mail.move_back[mail:laba]",
        "mail.trash_restore[mail:laba]",
        "mail.find_by_message_id[mail:laba]",
        "mail.repeat_do[mail:laba]",
        "mail.refuse_non_owned[mail:laba]",
        "mail.fault_lost_response[mail:laba]",
        "mail.watermark_suggestion[mail:laba>mail:labb]",
        "calendar.create_update_delete[calendar:laba]",
        "calendar.all_day[calendar:laba]",
        "calendar.timezone[calendar:laba]",
        "harness.guidance_budgets",
        "harness.doctor_clean",
    ):
        assert needed in ran, needed


def test_every_skip_has_a_reason(lab: FakeLab) -> None:
    report = run_all(lab)
    skipped = [r for r in report.results if r.status is Status.SKIPPED]
    assert skipped
    assert all(r.reason for r in skipped)
    assert {r.id.split("[")[0] for r in skipped} == EXPECTED_SKIPS
    outbox = next(r for r in skipped if r.id == "mail.outbox_queued")
    assert outbox.manual


def test_it_sends_only_to_owned_addresses_and_only_marked_mail(lab: FakeLab) -> None:
    run_all(lab)
    recipients: set[str] = set()
    for source in (FAKE_MAIL_A, FAKE_MAIL_B):
        for message in lab.provider.world.mail[source]:
            assert carries_marker(message.subject), "an unmarked message exists"
            recipients.update(a.address for a in (*message.to, *message.cc))
    assert recipients
    assert recipients <= OWNED


def test_nothing_marked_is_left_outside_the_trash_or_cancelled(lab: FakeLab) -> None:
    run_all(lab)
    world = lab.provider.world
    for source in (FAKE_MAIL_A, FAKE_MAIL_B):
        trash = next(f.id for f in world.folders[source] if f.role is FolderRole.TRASH)
        assert {m.folder_id for m in world.mail[source]} == {trash}
    for events in world.events.values():
        assert all(e.status.value == "cancelled" for e in events.values())


def test_a_dry_run_changes_nothing(lab: FakeLab) -> None:
    confirm_all(lab)
    before = lab.app.context.db.execute("SELECT count(*) FROM operations").fetchone()[0]
    report = execute(lab.app, lab.env, lab.config, RunOptions(dry_run=True))
    assert report.dry_run
    assert all(r.status is Status.SKIPPED for r in report.results)
    assert any(r.reason == "planned (dry run)" for r in report.results)
    assert lab.app.context.db.execute("SELECT count(*) FROM operations").fetchone()[0] == before
    assert lab.provider.dispatch_log == []
    assert all(not messages for messages in lab.provider.world.mail.values())


def test_only_and_skip_choose_scenarios(lab: FakeLab) -> None:
    report = run_all(lab, only=("self_send", "doctor"))
    assert {r.id for r in report.results} == {
        "mail.self_send[mail:laba]",
        "mail.self_send[mail:labb]",
        "harness.doctor_clean",
    }
    again = execute(
        lab.app, lab.env, lab.config, RunOptions(only=("mail.*",), skip=("fault", "repeat"))
    )
    reasons = by_id(again)
    assert reasons["mail.fault_lost_response[mail:laba]"] == (Status.SKIPPED, "skipped by --skip")
    assert reasons["mail.repeat_do[mail:laba]"][0] is Status.SKIPPED
    assert reasons["mail.self_send[mail:laba]"][0] is Status.PASS
    assert not any(i.startswith("calendar") for i in reasons)


def test_a_second_run_in_the_same_hour_skips_instead_of_failing(lab: FakeLab) -> None:
    first = run_all(lab)
    second = execute(lab.app, lab.env, lab.config, RunOptions())
    assert first.score.failed == 0
    assert second.score.failed == 0
    limited = [r for r in second.results if "limit" in r.reason]
    assert limited, "the hourly send limit was never reached"
    assert second.leftovers == ()


def test_a_missing_delivery_fails_the_scenario_with_a_reason_and_still_cleans_up(
    lab: FakeLab,
) -> None:
    confirm_all(lab)
    config = lab.config.model_copy(update={"arrival_wait_seconds": 6})
    lab.delivery.cut_off(ADDRESS_A)
    report = execute(lab.app, lab.env, config, RunOptions(only=("self_send[mail:laba]",)))
    result = report.results[0]
    assert result.status is Status.FAIL
    assert "did not happen within 6 seconds" in result.reason
    assert result.cleanup == "clean"
    assert report.leftovers == ()
    assert report.score.percent == 0


def test_the_lost_response_scenario_is_skipped_when_it_cannot_be_armed(lab: FakeLab) -> None:
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
    report = run_all(lab, only=("fault_lost",))
    assert {r.status for r in report.results} == {Status.SKIPPED}
    assert "no hook here" in report.results[0].reason


def test_a_lost_response_is_settled_with_exactly_one_copy(lab: FakeLab) -> None:
    report = run_all(lab, only=("fault_lost_response[mail:laba]",))
    assert report.results[0].status is Status.PASS
    sends = [d for d in lab.provider.dispatch_log if d.intent_kind.value == "mail.send"]
    assert [d.applied for d in sends] == [True]
    assert not lab.provider.world.armed(Fault.LOSE_RESPONSE_AFTER_APPLY)


def test_a_fault_that_never_fires_is_skipped_with_the_reason(lab: FakeLab) -> None:
    lab.env.faults.fired = lambda run_id: False  # type: ignore[method-assign]
    report = run_all(lab, only=("fault_lost_response[mail:laba]",))
    assert report.results[0].status is Status.SKIPPED
    assert "restart Thunderbird" in report.results[0].reason
