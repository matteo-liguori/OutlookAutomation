"""Scheduled passes start promptly, one session each, and never run late (S3)."""

from runs_rig import Rig

from chamberlain.app.wiring import run_tick
from chamberlain.attention.tick import WAKE_EXIT_STATUS
from chamberlain.runs import repo
from chamberlain.runs.models import PassKind, RunState
from chamberlain.runs.plan import due_passes
from chamberlain.runs.schedule import ScheduleSpec, apply_schedules

WEEKDAYS = frozenset({1, 2, 3, 4, 5})
FIRST = ScheduleSpec("first", PassKind.MAIL, "08:00", "UTC", WEEKDAYS)
SECOND = ScheduleSpec("second", PassKind.MAIL, "08:30", "UTC", WEEKDAYS)


def run_states(rig: Rig) -> dict[str, str]:
    return {run.schedule: run.state.value for run in repo.recent_runs(rig.ctx.db, 10)}


def test_a_chat_never_holds_back_a_pass_and_two_passes_never_share_a_session(rig: Rig) -> None:
    apply_schedules(rig.ctx, [FIRST, SECOND])
    rig.clock.advance(hours=1)  # 07:00
    assert rig.start()["ok"]  # a chat is open
    rig.clock.advance(hours=1, minutes=45)  # 08:45: both passes are due
    _ran, wake = run_tick(rig.app)
    assert wake == WAKE_EXIT_STATUS  # the chat does not hold the wake signal
    assert [p["schedule"] for p in due_passes(rig.ctx)] == ["first"]  # one at a time

    day = repo.recent_runs(rig.ctx.db, 1)[0].day
    one = rig.start(purpose="scheduled", scheduled_day=day)["data"]["session"]
    run_tick(rig.app)
    assert run_states(rig) == {"first": "running", "second": "due"}
    assert due_passes(rig.ctx) == []  # the first is being worked
    _ran, wake = run_tick(rig.app)
    assert wake == 0  # a pass in progress does hold the signal

    first = next(r for r in repo.recent_runs(rig.ctx.db, 5) if r.schedule == "first")
    assert rig.call("keep", kind="artifact", owner=first.handle, reason="r", text="done")["ok"]
    rig.clock.advance(minutes=1)
    _ran, wake = run_tick(rig.app)
    assert run_states(rig)["first"] == "finished"
    assert wake == WAKE_EXIT_STATUS  # the finished session no longer holds it
    assert [p["schedule"] for p in due_passes(rig.ctx)] == ["second"]

    two = rig.start(purpose="scheduled", scheduled_day=day)["data"]["session"]
    run_tick(rig.app)
    assert two != one
    second = next(r for r in repo.recent_runs(rig.ctx.db, 5) if r.schedule == "second")
    assert second.state is RunState.RUNNING
    assert second.session == two


def test_a_pass_not_started_within_six_hours_is_missed_not_run_late(rig: Rig) -> None:
    apply_schedules(rig.ctx, [FIRST])
    rig.clock.advance(hours=2)  # 08:00: due
    run_tick(rig.app)
    assert run_states(rig) == {"first": "due"}
    rig.clock.advance(hours=6, minutes=5)  # 14:05, still the same day
    run_tick(rig.app)
    assert run_states(rig) == {"first": "missed"}
    assert due_passes(rig.ctx) == []
