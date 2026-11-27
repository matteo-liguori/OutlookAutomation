"""Assemble the app, its handlers and tick steps over one store (S-18, A-6)."""

import logging
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from chamberlain.app.context import (
    Context,
    Handler,
    Services,
    SourceRouter,
    StartStep,
    SyncBudget,
    TickSteps,
)
from chamberlain.app.core import App
from chamberlain.app.doctor import make_doctor
from chamberlain.attention import handlers as attention_handlers
from chamberlain.attention import tick as attention_tick
from chamberlain.cleanup.rules import CleanupConfig
from chamberlain.cleanup.run import daily_cleanup
from chamberlain.memory import handlers as memory_handlers
from chamberlain.memory.artifacts import Budgets
from chamberlain.memory.sweeper import Compactor, sweep
from chamberlain.ports.clock import Clock, SystemClock
from chamberlain.review import maintenance as review_maintenance
from chamberlain.review.scan import ReviewSettings
from chamberlain.review.scan import tick_step as review_tick_step
from chamberlain.runs import maintenance as run_maintenance
from chamberlain.runs import plan as run_plan
from chamberlain.runs import updates as run_updates
from chamberlain.runs.reports import report_keep
from chamberlain.runs.updates import TagLister
from chamberlain.schema.base import ToolName
from chamberlain.schema.safelog import log_failure
from chamberlain.store.migrate import open_store
from chamberlain.surface.viewer import presence as viewer_presence
from chamberlain.writes import maintenance as write_maintenance
from chamberlain.writes.handlers import DOCTOR_CHECKS as WRITE_DOCTOR_CHECKS
from chamberlain.writes.handlers import make_handlers
from chamberlain.writes.models import WritePolicy

COMPACTORS: tuple[tuple[str, Compactor], ...] = (
    *write_maintenance.COMPACTORS,
    *run_maintenance.COMPACTORS,
    *review_maintenance.COMPACTORS,
)
"""Other tracks' lifetime rules for their own tables, run inside the sweeper's records step.

Attention trims its own events and sessions inside its tick, so it supplies none. The runs
track trims its run rows and notes."""


def app_handlers() -> dict[ToolName, Handler]:
    return {
        **attention_handlers.HANDLERS,
        **memory_handlers.HANDLERS,
        ToolName.KEEP: report_keep(memory_handlers.HANDLERS[ToolName.KEEP]),
        **make_handlers(),
        ToolName.DOCTOR: make_doctor(
            [
                *attention_handlers.DOCTOR_CHECKS,
                *memory_handlers.DOCTOR_CHECKS,
                *WRITE_DOCTOR_CHECKS,
            ]
        ),
    }


log = logging.getLogger("chamberlain.app")
LIVE_TEST_AGENT = "live-test"
"""The agent name `chamberlain live-test` starts its sessions with."""


def tick_steps(
    *,
    update_lister: TagLister | None = None,
    review: ReviewSettings | None = None,
    cleanup: CleanupConfig | None = None,
    open_viewer: bool = False,
) -> TickSteps:
    """What the tick runs on every call (`always`), when due (`extra`) and what `start`
    runs before offering work (`on_start`)."""

    def sweep_everything(ctx: Context) -> None:
        sweep(ctx, ctx.clock.now(), compactors=COMPACTORS)

    def maintain_writes(ctx: Context) -> None:
        write_maintenance.maintain(ctx)

    always: list[Callable[[Context], None]] = [
        write_maintenance.recover_operations,
        run_plan.plan_runs,
    ]
    extra: list[Callable[[Context], None]] = [sweep_everything, maintain_writes]
    if update_lister is not None:
        extra.append(run_updates.tick_step(update_lister))
    if review is not None:
        extra.append(review_tick_step(review))
    on_start: list[StartStep] = []
    if cleanup is not None and cleanup.enabled and cleanup.rules:
        rules = cleanup

        def clean_up_mail(ctx: Context, _agent: str) -> list[str]:
            report = daily_cleanup(ctx, rules)
            return [] if report is None else report.summary()

        on_start.append(clean_up_mail)
    if open_viewer:

        def show_the_viewer(ctx: Context, agent: str) -> list[str]:
            if agent == LIVE_TEST_AGENT:
                return []  # a test run must not open browser tabs
            try:
                viewer_presence.open_viewer(ctx.data_root)
            except Exception:
                log_failure(log, "opening the viewer at session start failed")
            return []

        on_start.append(show_the_viewer)

        def show_held(ctx: Context, agent: str, grant: str) -> bool:
            if agent == LIVE_TEST_AGENT:
                return False  # a test run must not open browser tabs
            try:
                viewer_presence.open_viewer(ctx.data_root, grant=grant)
            except Exception:
                log_failure(log, "opening the viewer on a held reply failed")
                return False
            return True

        return TickSteps(
            always=tuple(always), extra=tuple(extra), on_start=tuple(on_start), show_held=show_held
        )
    return TickSteps(always=tuple(always), extra=tuple(extra), on_start=tuple(on_start))


def build_app(
    data_root: Path,
    *,
    clock: Clock | None = None,
    sources: SourceRouter | None = None,
    policy: WritePolicy | None = None,
    budgets: Budgets | None = None,
    update_lister: TagLister | None = None,
    review: ReviewSettings | None = None,
    cleanup: CleanupConfig | None = None,
    open_viewer: bool = False,
) -> App:
    """Open the store under `data_root` and assemble the app (S-18).

    `sources` is the configured connector router. `update_lister` turns the daily tag
    check on (`runs.updates`); without it no tick ever touches the network.
    """
    the_clock = clock or SystemClock()
    db = open_store(data_root, the_clock)
    configured_policy = policy if policy is not None else WritePolicy()
    context = Context(
        db=db,
        clock=the_clock,
        data_root=data_root,
        sources=sources or SourceRouter(),
        services=Services.bind(db, the_clock, data_root, policy=configured_policy, budgets=budgets),
        write_policy=configured_policy,
        tick_steps=tick_steps(
            update_lister=update_lister, review=review, cleanup=cleanup, open_viewer=open_viewer
        ),
    )
    return App(context, app_handlers(), attention_handlers.HOOKS)


def run_tick(app: App, *, force: bool = True) -> tuple[bool, int]:
    """The operator's tick: `(detectors ran, wake status)`.

    The wake status is 10 when events wait for a session and none is active, so a
    scheduler can start one (07-attention.md, delivery 3); 0 otherwise.
    """
    ran = attention_tick.tick(replace(app.context, sync_budget=SyncBudget()), force=force)
    return ran, attention_tick.wake_status(app.context)
