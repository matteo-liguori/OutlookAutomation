"""Planning runs: which passes are due now, and what became of the ones in flight.

Contract: docs/contracts/07-attention.md (`schedule.pass_due`, A-10, R10). `plan_runs` is a
tick step that runs on every call, before detection, so a pass that has come due is an
event in the same call. Attention raises the event from the `runs` rows; nothing here
writes `events`.

A run is `due` from its time, `running` once a scheduled session has started, then
`finished` (a report exists), `incomplete` (its session ended without one) or `missed`
(its day ended first, or the tick first noticed it too late). The wake adapter starts the
scheduled session with `scheduled_day` set to the run's `day`; a session from another
local day is stopped by the guard in attention (R10), so a late wake never works a stale
day.
"""

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from chamberlain.app.context import Context
from chamberlain.domain.handles import HandleKind, new_handle
from chamberlain.domain.timefmt import parse, ts
from chamberlain.limits import SESSION_IDLE_HOURS
from chamberlain.runs import compose, repo
from chamberlain.runs.models import RunRow, RunState
from chamberlain.runs.read_state import mark_reported
from chamberlain.runs.reports import store_report
from chamberlain.runs.schedule import due_instant
from chamberlain.runs.tuning import MAX_LATE
from chamberlain.schema.safelog import log_failure
from chamberlain.store.db import transaction
from chamberlain.store.migrate import stamp

log = logging.getLogger("chamberlain.runs")

INCOMPLETE_REPORT = (
    "This pass ended without a report from the assistant. Nothing was lost: check the "
    "activity log for what it did before it stopped, and treat the pass as not covered."
)


def local_day(ctx: Context, zone: str) -> str:
    """Today's date in `zone`."""
    return ctx.clock.now().astimezone(ZoneInfo(zone)).date().isoformat()


def _create_due(ctx: Context, now: datetime) -> None:
    for schedule in repo.schedules(ctx.db):
        if not schedule.enabled:
            continue
        zone = ZoneInfo(schedule.zone)
        local = now.astimezone(zone)
        if local.isoweekday() not in schedule.weekdays:
            continue
        day = local.date().isoformat()
        due = due_instant(schedule.at_local, day, zone)
        if now < due or repo.run_for_day(ctx.db, schedule.name, day) is not None:
            continue
        if schedule.created_at and due < parse(schedule.created_at):
            continue  # its time today passed before the schedule existed: not a missed pass
        late = now - due > MAX_LATE
        repo.insert_run(
            ctx.db,
            handle=new_handle(HandleKind.RUN),
            schedule=schedule.name,
            day=day,
            state=(RunState.MISSED if late else RunState.DUE).value,
            due_at=ts(due),
            finished_at=stamp(ctx.clock) if late else None,
        )


def _close_old_days(ctx: Context) -> None:
    zones = {schedule.name: schedule.zone for schedule in repo.schedules(ctx.db)}
    open_runs = repo.runs_in_states(ctx.db, (RunState.DUE.value, RunState.RUNNING.value))
    for run in open_runs:
        zone = zones.get(run.schedule)
        if zone is None or run.day >= local_day(ctx, zone):
            continue
        state = RunState.MISSED if run.state is RunState.DUE else RunState.INCOMPLETE
        repo.set_run_state(ctx.db, run.handle, state.value, finished_at=stamp(ctx.clock))


def _day_end(day: str, zone: ZoneInfo) -> str:
    """The instant the local day `day` ends, as a stored timestamp."""
    return ts(datetime.fromisoformat(day).replace(tzinfo=zone) + timedelta(days=1))


def _attach_sessions(ctx: Context) -> None:
    zones = {schedule.name: schedule.zone for schedule in repo.schedules(ctx.db)}
    for run in repo.runs_in_states(ctx.db, (RunState.DUE.value,)):
        zone = zones.get(run.schedule)
        if zone is None:
            continue
        session = repo.scheduled_session_between(
            ctx.db, run.due_at, _day_end(run.day, ZoneInfo(zone))
        )
        if session is not None:
            repo.set_run_state(
                ctx.db,
                run.handle,
                RunState.RUNNING.value,
                started_at=stamp(ctx.clock),
                session=session,
            )


def _miss_late(ctx: Context, now: datetime) -> None:
    """A pass still waiting for its session more than `MAX_LATE` after its time is missed.

    The wake signal now starts due passes promptly (a chat no longer holds it back), so a
    pass that was not started within six hours is not run late, as docs/scheduling.md says.
    """
    for run in repo.runs_in_states(ctx.db, (RunState.DUE.value,)):
        if now - parse(run.due_at) > MAX_LATE:
            repo.set_run_state(
                ctx.db, run.handle, RunState.MISSED.value, finished_at=stamp(ctx.clock)
            )


def _settle_running(ctx: Context, now: datetime) -> tuple[list[RunRow], list[RunRow]]:
    """Finish runs that have a report; end running ones whose session is gone.

    Returns `(finished, ended)`: the runs that just got a report, and those that ended without.
    """
    idle_edge = ts(now - timedelta(hours=SESSION_IDLE_HOURS))
    finished: list[RunRow] = []
    ended: list[RunRow] = []
    for run in repo.runs_in_states(ctx.db, (RunState.DUE.value, RunState.RUNNING.value)):
        if repo.report_handles(ctx.db, run.handle):
            repo.set_run_state(
                ctx.db, run.handle, RunState.FINISHED.value, finished_at=stamp(ctx.clock)
            )
            finished.append(run)
        elif run.state is RunState.RUNNING and (
            run.session is None or repo.session_has_ended(ctx.db, run.session, idle_edge)
        ):
            repo.set_run_state(
                ctx.db, run.handle, RunState.INCOMPLETE.value, finished_at=stamp(ctx.clock)
            )
            ended.append(run)
    return finished, ended


def _keep_outlines(ctx: Context) -> None:
    """Every pass being worked gets the outline of what its report should hold."""
    for run in repo.runs_in_states(ctx.db, (RunState.RUNNING.value,)):
        compose.ensure_outline(ctx, run)


def plan_runs(ctx: Context) -> None:
    """The tick step (an `always` tick step). It may raise; the tick logs and goes on."""
    now = ctx.clock.now()
    with transaction(ctx.db):
        _create_due(ctx, now)
        _attach_sessions(ctx)
        _miss_late(ctx, now)
        _close_old_days(ctx)
        finished, ended = _settle_running(ctx, now)
    for run in ended:
        try:
            store_report(ctx, run.handle, INCOMPLETE_REPORT)
        except Exception:
            log_failure(log, "storing the report of %s failed", run.handle)
    for run in finished:
        mark_reported(ctx, run)
    for run in [*finished, *ended]:
        try:
            compose.settle_snapshot(ctx, run)
        except Exception:
            log_failure(log, "composing the report of %s failed", run.handle)
    try:
        _keep_outlines(ctx)
    except Exception:
        log_failure(log, "keeping a report outline failed")


def due_passes(ctx: Context) -> list[dict[str, str]]:
    """What a wake adapter needs to start a scheduled session: handle, schedule, kind, day.

    One pass at a time: the oldest due pass, and none while another is being worked. Two
    passes never share a session; the next one is listed once the first has its report.
    """
    kinds = {schedule.name: schedule.kind.value for schedule in repo.schedules(ctx.db)}
    if repo.runs_in_states(ctx.db, (RunState.RUNNING.value,)):
        return []
    runs = repo.runs_in_states(ctx.db, (RunState.DUE.value,))[:1]
    return [
        {
            "run": run.handle,
            "schedule": run.schedule,
            "kind": kinds.get(run.schedule, ""),
            "day": run.day,
        }
        for run in runs
    ]
