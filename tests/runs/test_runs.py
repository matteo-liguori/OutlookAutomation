"""Runs: the schedule record, due passes, the wrong-day stop and run-owned reports.

Rules: docs/contracts/07-attention.md (`schedule.pass_due`, A-2, R10) and
docs/contracts/06-memory.md (M-4, M-10, lifetime table).
"""

from typing import Any

import pytest
from runs_rig import Rig

from chamberlain.app.wiring import COMPACTORS, run_tick
from chamberlain.memory.artifacts import blob_path
from chamberlain.memory.sweeper import sweep
from chamberlain.runs import repo
from chamberlain.runs.models import PassKind, RunState
from chamberlain.runs.plan import INCOMPLETE_REPORT, due_passes, plan_runs
from chamberlain.runs.reports import read_reports, store_report
from chamberlain.runs.schedule import ScheduleSpec, apply_schedules, list_schedules, set_schedule
from chamberlain.schema.errors import ChamberlainError

MORNING = ScheduleSpec(
    name="morning-mail",
    kind=PassKind.MAIL,
    at_local="08:00",
    zone="UTC",
    weekdays=frozenset({1, 2, 3, 4, 5}),
)


def states(rig: Rig) -> list[str]:
    return [run.state.value for run in repo.recent_runs(rig.ctx.db, 10)]


def pass_events(rig: Rig) -> list[Any]:
    return rig.rows(
        "SELECT * FROM events WHERE kind = 'schedule.pass_due' AND state IN ('new', 'delivered')"
    )


def due_at_eight(rig: Rig) -> None:
    set_schedule(rig.ctx, MORNING)
    rig.clock.advance(hours=2)  # 08:00 UTC on Monday 2026-01-05
    run_tick(rig.app)


def test_the_schedule_record_is_validated_and_replaced_as_a_whole(rig: Rig) -> None:
    for bad in (
        ScheduleSpec("Bad Name", PassKind.MAIL, "08:00", "UTC"),
        ScheduleSpec("a", PassKind.MAIL, "8:00", "UTC"),
        ScheduleSpec("a", PassKind.MAIL, "25:00", "UTC"),
        ScheduleSpec("a", PassKind.MAIL, "08:00", "Not/AZone"),
        ScheduleSpec("a", PassKind.MAIL, "08:00", "UTC", weekdays=frozenset({0})),
    ):
        with pytest.raises(ChamberlainError) as raised:
            set_schedule(rig.ctx, bad)
        assert raised.value.code.value == "config.invalid"
    assert list_schedules(rig.ctx) == []
    other = ScheduleSpec("calendar-check", PassKind.CALENDAR, "07:30", "Europe/London")
    apply_schedules(rig.ctx, [MORNING, other])
    assert [row.name for row in list_schedules(rig.ctx)] == ["calendar-check", "morning-mail"]
    apply_schedules(rig.ctx, [other])
    assert [row.name for row in list_schedules(rig.ctx)] == ["calendar-check"]
    with pytest.raises(ChamberlainError):
        apply_schedules(rig.ctx, [other, ScheduleSpec("zzz", PassKind.MAIL, "99:99", "UTC")])
    assert [row.name for row in list_schedules(rig.ctx)] == ["calendar-check"]


def test_a_pass_is_due_from_its_time_on_its_weekdays_once_a_day(rig: Rig) -> None:
    set_schedule(rig.ctx, MORNING)
    run_tick(rig.app)
    assert states(rig) == []  # 06:00, not yet
    rig.clock.advance(hours=2)
    run_tick(rig.app)
    run_tick(rig.app)
    assert states(rig) == ["due"]
    assert len(pass_events(rig)) == 1
    assert [p["kind"] for p in due_passes(rig.ctx)] == ["mail"]
    rig.clock.advance(hours=5 * 24)  # Saturday 2026-01-10: no run on a weekend
    run_tick(rig.app)
    assert states(rig) == ["missed"]  # Monday's unworked pass ended with its day
    assert all(run.day != "2026-01-10" for run in repo.recent_runs(rig.ctx.db, 10))


def test_a_pass_first_noticed_much_too_late_is_recorded_missed_not_run(rig: Rig) -> None:
    set_schedule(rig.ctx, MORNING)
    rig.clock.advance(hours=12)  # 18:00
    run_tick(rig.app)
    assert states(rig) == ["missed"]
    assert pass_events(rig) == []
    assert due_passes(rig.ctx) == []


def test_the_due_pass_reaches_the_scheduled_start_as_a_work_item(rig: Rig) -> None:
    due_at_eight(rig)
    run = repo.recent_runs(rig.ctx.db, 1)[0]
    started = rig.start(purpose="scheduled", scheduled_day=run.day)
    assert started["ok"], started
    item = next(w for w in started["data"]["work"] if w["kind"] == "schedule.pass_due")
    assert item["subject"] == run.handle
    assert run.handle in item["summary"]
    assert rig.call("next")["ok"]  # the next tick sees the scheduled session
    assert repo.get_run(rig.ctx.db, run.handle).state is RunState.RUNNING  # type: ignore[union-attr]


def test_a_scheduled_pass_produces_one_report_owned_by_and_1_related_checks(rig: Rig) -> None:
    # a_scheduled_pass_produces_one_report_owned_by_its_run
    due_at_eight(rig)
    run = repo.recent_runs(rig.ctx.db, 1)[0]
    rig.start(purpose="scheduled", scheduled_day=run.day)
    kept = rig.call(
        "keep", kind="artifact", owner=run.handle, reason="Morning mail report", text="3 new."
    )
    assert kept["ok"], kept
    rig.clock.advance(minutes=2)
    run_tick(rig.app)
    done = repo.get_run(rig.ctx.db, run.handle)
    assert done is not None
    assert done.state is RunState.FINISHED
    reports = read_reports(rig.ctx, run.handle)
    assert [r.text for r in reports] == ["3 new."]
    assert [
        r["owner_kind"]
        for r in rig.rows(
            "SELECT o.owner_kind FROM artifact_owners o JOIN artifacts a ON a.handle = o.artifact "
            "WHERE a.filename IS NOT 'report-outline.txt'"
        )
    ] == ["run"]
    assert pass_events(rig) == []  # the condition ended, so the event expired (A-2)
    assert store_report(rig.ctx, run.handle, "ignored") == reports[0].handle  # one report only
    sha = rig.rows("SELECT sha256 FROM artifacts WHERE handle = ?", reports[0].handle)[0]["sha256"]
    assert blob_path(rig.ctx.data_root, sha).exists()
    rig.clock.advance(hours=31 * 24)  # past the 30-day default, before the run row goes (35 days)
    sweep(rig.ctx, rig.clock.now(), compactors=COMPACTORS)
    sweep(rig.ctx, rig.clock.now(), compactors=COMPACTORS)
    assert repo.get_run(rig.ctx.db, run.handle) is not None  # the report expired on its own
    assert read_reports(rig.ctx, run.handle) == []
    assert not blob_path(rig.ctx.data_root, sha).exists()

    # a_run_owned_report_survives_sweeps_until_its_lifetime_then_the_sweeper_removes_it
    due_at_eight(rig)
    run = repo.recent_runs(rig.ctx.db, 1)[0]
    handle = store_report(rig.ctx, run.handle, "Harness-written report.")
    assert handle is not None
    sha = rig.rows("SELECT sha256 FROM artifacts WHERE handle = ?", handle)[0]["sha256"]
    rig.clock.advance(hours=120)
    sweep(rig.ctx, rig.clock.now(), compactors=COMPACTORS)
    assert [r.text for r in read_reports(rig.ctx, run.handle)] == ["Harness-written report."]
    rig.clock.advance(hours=240)  # past the 14-day lifetime the harness gives its reports
    sweep(rig.ctx, rig.clock.now(), compactors=COMPACTORS)
    assert read_reports(rig.ctx, run.handle) == []
    assert rig.rows("SELECT state FROM artifacts WHERE handle = ?", handle)[0]["state"] in (
        "released",
        "deleted",
    )
    sweep(rig.ctx, rig.clock.now(), compactors=COMPACTORS)
    assert not blob_path(rig.ctx.data_root, sha).exists()


def test_r10_the_hold_of_a_run_ends_with_the_run_row_and_1_related_checks(rig: Rig) -> None:
    # the_hold_of_a_run_ends_with_the_run_row
    due_at_eight(rig)
    run = repo.recent_runs(rig.ctx.db, 1)[0]
    store_report(rig.ctx, run.handle, "Report.")
    rig.clock.advance(hours=864)  # the run row goes 35 days after its day
    sweep(rig.ctx, rig.clock.now(), compactors=COMPACTORS)
    assert repo.get_run(rig.ctx.db, run.handle) is None
    sweep(rig.ctx, rig.clock.now(), compactors=COMPACTORS)
    assert rig.rows("SELECT * FROM artifact_owners") == []

    # r10_a_scheduled_start_for_an_earlier_day_must_stop
    due_at_eight(rig)
    run = repo.recent_runs(rig.ctx.db, 1)[0]
    rig.clock.advance(hours=24)  # the wake adapter fired a day late
    stale = rig.start(purpose="scheduled", scheduled_day=run.day)
    assert stale["ok"] is False
    assert stale["error"]["code"] == "session.stale"
    run_tick(rig.app)
    assert repo.get_run(rig.ctx.db, run.handle).state is RunState.MISSED  # type: ignore[union-attr]
    assert run.handle not in [e["subject"] for e in pass_events(rig)]  # only today's is due
    today = rig.clock.now().date().isoformat()
    assert rig.start(purpose="scheduled", scheduled_day=today)["ok"]


def test_r10_a_scheduled_session_from_an_earlier_day_stops_and_1_related_checks(
    rig: Rig,
) -> None:
    # r10_a_scheduled_session_from_an_earlier_day_stops_on_every_tool
    due_at_eight(rig)
    run = repo.recent_runs(rig.ctx.db, 1)[0]
    assert rig.start(purpose="scheduled", scheduled_day=run.day)["ok"]
    rig.clock.advance(hours=25)
    reply = rig.call("next")
    assert reply["ok"] is False
    assert reply["error"]["code"] == "session.stale"
    run_tick(rig.app)
    assert repo.get_run(rig.ctx.db, run.handle).state is RunState.INCOMPLETE  # type: ignore[union-attr]

    # a_live_chat_never_supersedes_or_is_stopped_by_a_scheduled_run
    due_at_eight(rig)
    run = repo.recent_runs(rig.ctx.db, 1)[0]
    scheduled = rig.start(purpose="scheduled", scheduled_day=run.day)["data"]["session"]
    chat = rig.start()["data"]["session"]
    assert chat != scheduled
    assert rig.rows("SELECT ended_at FROM sessions WHERE handle = ?", scheduled)[0][0] is None


def test_a_scheduled_session_that_ends_without_a_report_leaves_an_incomplete_run(
    rig: Rig,
) -> None:
    due_at_eight(rig)
    run = repo.recent_runs(rig.ctx.db, 1)[0]
    rig.start(purpose="scheduled", scheduled_day=run.day)
    rig.clock.advance(hours=5)  # the session idles out after four hours
    run_tick(rig.app)
    assert repo.get_run(rig.ctx.db, run.handle).state is RunState.INCOMPLETE  # type: ignore[union-attr]
    assert [r.text for r in read_reports(rig.ctx, run.handle)] == [INCOMPLETE_REPORT]
    run_tick(rig.app)
    assert len(read_reports(rig.ctx, run.handle)) == 1


def test_a_report_kept_before_any_scheduled_session_still_finishes_the_run(rig: Rig) -> None:
    due_at_eight(rig)
    run = repo.recent_runs(rig.ctx.db, 1)[0]
    rig.start()  # an interactive chat did the pass
    assert rig.call("keep", kind="artifact", owner=run.handle, reason="Pass report", text="Done.")[
        "ok"
    ]
    plan_runs(rig.ctx)
    assert repo.get_run(rig.ctx.db, run.handle).state is RunState.FINISHED  # type: ignore[union-attr]


def test_there_is_one_run_per_schedule_and_day(rig: Rig) -> None:
    due_at_eight(rig)
    rig.clock.advance(minutes=30)
    plan_runs(rig.ctx)
    plan_runs(rig.ctx)
    assert len(repo.recent_runs(rig.ctx.db, 10)) == 1
    rig.clock.advance(hours=24)
    plan_runs(rig.ctx)
    assert len(repo.recent_runs(rig.ctx.db, 10)) == 2
