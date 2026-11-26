"""The runner: open a session, check authority, run each scenario, clean up, score.

Contract: docs/live-test.md (How a run goes). The runner never confirms a grant. If the
standing grants are missing or unconfirmed the run stops before any scenario, with the exact
next step.
"""

import queue
import secrets
import threading
import time
from collections.abc import Callable, Sequence
from contextlib import nullcontext
from dataclasses import dataclass, field, replace

from chamberlain.app.core import App
from chamberlain.domain.timefmt import ts
from chamberlain.livetest.authority import ensure_card, require_grants
from chamberlain.livetest.catalogue import build_plan
from chamberlain.livetest.config import LiveTestConfig
from chamberlain.livetest.env import Environment
from chamberlain.livetest.lab import (
    JSON,
    Lab,
    RunStopped,
    ScenarioFailure,
    ScenarioSkip,
)
from chamberlain.livetest.markers import MARKER, new_run_id
from chamberlain.livetest.model import (
    RunReport,
    ScenarioResult,
    Status,
    Timing,
    compute_score,
)
from chamberlain.livetest.registry import Planned, Scope
from chamberlain.livetest.scenarios_harness import doctor_states
from chamberlain.ports.health import passive_health_checks
from chamberlain.schema.errors import ChamberlainError

DEFAULT_WORKERS = 8
"""Scenarios a live run runs side by side (`--workers`)."""
NOT_RUN_YET = "not run yet"
"""The reason on a scenario a report lists before it has run (or after the run was stopped)."""
APPROVAL_CONFIG = "live_test.approve_calendar_edits in connections.yaml"
APPROVAL_OPTION = "the --approve-calendar-edits option of this run"
RUN_ACTION_PREFIX = "Live test run "


@dataclass(frozen=True)
class RunOptions:
    only: tuple[str, ...] = ()
    skip: tuple[str, ...] = ()
    keep: bool = False
    dry_run: bool = False
    approve_calendar_edits: bool = False
    """The one-run option; the config setting works without it."""
    run_id: str | None = None
    retry_from: str | None = None
    """`--retry`: a run id (or `latest`) whose failed and never-run scenarios run again."""
    retry_ids: tuple[str, ...] = ()
    """Only these exact scenario ids (`--retry`: what failed or never ran in an earlier run)."""
    progress: Callable[[RunReport], None] | None = field(default=None, compare=False)
    """Called with the report so far after every scenario, so a stopped run keeps its results."""
    workers: int = 1
    """Scenarios run side by side: their sends go out together and their waits overlap."""
    open_app: Callable[[], App] | None = field(default=None, compare=False)
    """Opens another app on the same data root, one per extra worker (needed for workers > 1)."""


def approval(config: LiveTestConfig, options: RunOptions) -> tuple[bool, str]:
    """`(approved, where the owner said so)`."""
    if config.approve_calendar_edits:
        return True, APPROVAL_CONFIG
    if options.approve_calendar_edits:
        return True, APPROVAL_OPTION
    return False, ""


def make_run_id(env: Environment, options: RunOptions) -> str:
    return options.run_id or new_run_id(env.clock.now(), secrets.token_hex(2))


def open_lab(
    app: App,
    env: Environment,
    config: LiveTestConfig,
    run_id: str,
    options: RunOptions,
    *,
    sweeping: bool = False,
) -> Lab:
    """Start a session and find the test card. Writes nothing but the session row."""
    approved, source = approval(config, options)
    lab = Lab(
        app=app,
        env=env,
        config=config,
        run_id=run_id,
        approve_edits=approved,
        approval_source=source,
        sweeping=sweeping,
    )
    started = lab.ok("start", agent="live-test")
    lab.session = str(started["session"])
    lab.card = ensure_card(lab)
    return lab


def _fork_lab(main: Lab, app: App) -> Lab:
    """A lab for one worker: its own app and session, the run's card, action and write lock."""
    lab = replace(main, app=app, session="", run_handles=set[str](), accepted_uncertain=set[str]())
    lab.begin()
    lab.session = str(lab.ok("start", agent="live-test")["session"])
    return lab


def _run_side_by_side(
    main: Lab,
    items: Sequence[Planned],
    options: RunOptions,
    finished: Callable[[ScenarioResult], None],
) -> None:
    """Run `items` on `options.workers` labs at once; every lab's handles join the main lab.

    Each worker thread opens its own app (a store connection belongs to its thread) and
    takes scenarios from one queue; results come back to this thread in the order they end.
    """
    count = min(options.workers, len(items))
    if count <= 1 or options.open_app is None:
        for item in items:
            finished(_run_one(main, item, options.keep))
        return
    opener = options.open_app
    todo: queue.SimpleQueue[Planned] = queue.SimpleQueue()
    for item in items:
        todo.put(item)
    results: queue.SimpleQueue[ScenarioResult | BaseException | None] = queue.SimpleQueue()
    merge = threading.Lock()

    def worker() -> None:
        try:
            app = opener()
            try:
                lab = _fork_lab(main, app)
                while True:
                    try:
                        item = todo.get_nowait()
                    except queue.Empty:
                        break
                    results.put(_run_one(lab, item, options.keep))
                with merge:
                    main.run_handles |= lab.run_handles
                    main.accepted_uncertain |= lab.accepted_uncertain
            finally:
                app.close()
        except BaseException as error:  # reported by the main thread
            results.put(error)
        finally:
            results.put(None)

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(count)]
    for thread in threads:
        thread.start()
    running = count
    while running:
        got = results.get()
        if got is None:
            running -= 1
        elif isinstance(got, BaseException):
            raise got
        else:
            finished(got)
    for thread in threads:
        thread.join()


def _used_mail_sources(plan: Sequence[Planned], config: LiveTestConfig) -> list[str]:
    used: dict[str, None] = {}
    for item in plan:
        if item.skip:
            continue
        if item.spec.scope in (Scope.MAILBOX, Scope.PAIR):
            used[item.target.source] = None
            if item.target.other:
                used[item.target.other] = None
        if item.spec.id == "calendar.invite_flow":
            invitee = config.calendars[item.target.source].invitee_mail
            if invitee is not None:
                used[invitee] = None
    return list(used)


def _start_action(lab: Lab) -> str:
    """One action per run on the test card; stale open ones from crashed runs are finished."""
    card = lab.ok("open", handle=lab.card)["view"]
    for action in card.get("actions", []):
        if action["state"] == "open" and str(action["title"]).startswith(RUN_ACTION_PREFIX):
            lab.ok(
                "card",
                op="finish_action",
                action=action["handle"],
                outcome="Left open by a run that did not finish.",
            )
    made = lab.ok(
        "card",
        op="add_action",
        card=lab.card,
        action_fields={"title": f"{RUN_ACTION_PREFIX}{lab.run_id}"},
    )
    return str(made["card"]["actions"][-1]["handle"])


def _run_one(lab: Lab, item: Planned, keep: bool) -> ScenarioResult:
    lab.begin()
    started = time.monotonic()
    status = Status.PASS
    reason = ""
    try:
        item.spec.run(lab, item.target)
    except ScenarioSkip as skipped:
        status, reason = Status.SKIPPED, str(skipped)
    except ScenarioFailure as failure:
        status, reason = Status.FAIL, str(failure)
    except ChamberlainError as error:
        status, reason = Status.FAIL, f"harness error {error.code}: {error.message[:100]}"
    except Exception as error:
        status, reason = Status.FAIL, f"unexpected {type(error).__name__}: {str(error)[:100]}"
    cleanup = "kept" if keep else "clean"
    if not keep:
        problems = lab.cleanup()
        if problems:
            cleanup = "; ".join(problems)[:300]
    return ScenarioResult(
        id=item.id,
        group=item.spec.group,
        status=status,
        reason=reason,
        seconds=round(time.monotonic() - started, 2),
        handles=tuple(sorted(lab.handles)),
        timings=tuple(Timing(tool=t.tool, ms=t.ms) for t in lab.timings),
        probe_checks=tuple(lab.probe_checks),
        cleanup=cleanup,
        manual=item.spec.manual,
    )


def _pending(item: Planned) -> ScenarioResult:
    if item.skip:
        return _skipped(item, False)
    return ScenarioResult(
        id=item.id,
        group=item.spec.group,
        status=Status.SKIPPED,
        reason=NOT_RUN_YET,
        manual=item.spec.manual,
    )


def _skipped(item: Planned, dry: bool) -> ScenarioResult:
    reason = item.skip or ("planned (dry run)" if dry else "")
    return ScenarioResult(
        id=item.id,
        group=item.spec.group,
        status=Status.SKIPPED,
        reason=reason,
        manual=item.spec.manual,
    )


def leftover_items(lab: Lab, plan: Sequence[Planned]) -> list[str]:
    """Handles of this run's marked items that are not in the trash or cancelled."""
    found: list[str] = []
    marker = lab.marker
    sources = {
        p.target.source for p in plan if p.spec.scope is not Scope.CALENDAR and p.target.source
    }
    sources |= {p.target.other for p in plan if p.target.other}
    for source in sorted(sources):
        for copy in lab.copies(source, marker):
            if copy.folder_role.value != "trash":
                found.append(lab.handle_of(copy))
    start, end = lab.calendar_window()
    for calendar in sorted({p.target.source for p in plan if p.spec.scope is Scope.CALENDAR}):
        for event in lab.env.probe.events(calendar, marker, start, end):
            if event.status != "cancelled":
                found.append(lab.event_handle(event))
    return sorted(set(found))


def _second_chance(lab: Lab, plan: Sequence[Planned]) -> None:
    """One more try at removing whatever the scenarios could not."""
    for item in plan:
        if item.skip:
            continue
        try:
            if item.spec.scope in (Scope.MAILBOX, Scope.PAIR):
                for source in (item.target.source, item.target.other):
                    if source:
                        lab.remove_mail(source, lab.marker)
            elif item.spec.scope is Scope.CALENDAR and lab.approve_edits:
                lab.remove_events(item.target.source, lab.marker)
        except (ChamberlainError, ScenarioFailure, ScenarioSkip):
            continue


def execute(app: App, env: Environment, config: LiveTestConfig, options: RunOptions) -> RunReport:
    """Plan, check authority, run, clean up and score one run."""
    sources = app.context.sources
    with passive_health_checks() if options.dry_run else nullcontext():
        plan = build_plan(config, sources, only=options.only, skip=options.skip)
    if options.retry_ids:
        plan = [p for p in plan if p.id in options.retry_ids]
    run_id = make_run_id(env, options)
    started_at = ts(env.clock.now())
    names = tuple(sorted({s for p in plan for s in (p.target.source, p.target.other) if s}))
    if options.dry_run:
        results = tuple(_skipped(p, True) for p in plan)
        return _report(run_id, started_at, env, "dry-run", options, names, results, [], {}, {})
    lab = open_lab(app, env, config, run_id, options)
    runnable = [p for p in plan if not p.skip]
    try:
        require_grants(lab, _used_mail_sources(plan, config))
    except RunStopped as stop:
        results = tuple(_skipped(p, False) for p in plan)
        report = _report(run_id, started_at, env, env.mode, options, names, results, [], {}, {})
        return report.model_copy(update={"stopped": str(stop)})
    lab.action = _start_action(lab)
    lab.baseline = doctor_states(lab)
    done: list[ScenarioResult] = []

    def tell() -> None:
        if options.progress is None:
            return
        by_id = {r.id: r for r in done}
        sofar = tuple(by_id.get(p.id) or _pending(p) for p in plan)
        partial = _report(
            run_id, started_at, env, env.mode, options, names, sofar, [], lab.baseline, {}
        )
        options.progress(partial.model_copy(update={"finished": False}))

    tell()

    def finished(result: ScenarioResult) -> None:
        done.append(result)
        tell()

    together = [p for p in plan if not (p.skip or p.spec.last or p.spec.alone)]
    _run_side_by_side(lab, together, options, finished)
    for item in plan:
        if item.spec.alone and not (item.skip or item.spec.last):
            finished(_run_one(lab, item, options.keep))
    leftovers: list[str] = []
    if not options.keep and runnable:
        _second_chance(lab, plan)
        leftovers = leftover_items(lab, plan)
    for item in plan:
        if item.spec.last and not item.skip:
            done.append(_run_one(lab, item, options.keep))
            tell()
    by_id = {r.id: r for r in done}
    ordered = tuple(by_id.get(p.id) or _skipped(p, False) for p in plan)
    after = doctor_states(lab)
    outcome = _outcome_line(ordered, len(leftovers))
    lab.ok("card", op="finish_action", action=lab.action, outcome=outcome)
    return _report(
        run_id, started_at, env, env.mode, options, names, ordered, leftovers, lab.baseline, after
    )


def _outcome_line(results: Sequence[ScenarioResult], leftovers: int) -> str:
    failed = sum(1 for r in results if r.status is Status.FAIL)
    passed = sum(1 for r in results if r.status is Status.PASS)
    return f"{passed} passed, {failed} failed, {leftovers} left behind."


def _report(
    run_id: str,
    started_at: str,
    env: Environment,
    mode: str,
    options: RunOptions,
    names: tuple[str, ...],
    results: tuple[ScenarioResult, ...],
    leftovers: list[str],
    before: JSON,
    after: JSON,
) -> RunReport:
    return RunReport(
        run_id=run_id,
        started_at=started_at,
        finished_at=ts(env.clock.now()),
        mode=mode,
        dry_run=options.dry_run,
        kept=options.keep,
        sources=names,
        results=results,
        score=compute_score(results, len(leftovers)),
        leftovers=tuple(leftovers),
        doctor_before=tuple(f"{k}:{v}" for k, v in sorted(before.items())),
        doctor_after=tuple(f"{k}:{v}" for k, v in sorted(after.items())),
    )


def retry_ids(report: RunReport) -> tuple[str, ...]:
    """The scenarios of a run worth running again: those that failed or never ran."""
    return tuple(r.id for r in report.results if r.status is Status.FAIL or r.reason == NOT_RUN_YET)


__all__ = [
    "DEFAULT_WORKERS",
    "MARKER",
    "NOT_RUN_YET",
    "RunOptions",
    "approval",
    "execute",
    "open_lab",
    "retry_ids",
]
