"""Assembling the app from the tracks.

Each track exposes, in its own `handlers.py`:

    HANDLERS: dict[ToolName, Handler]
    DOCTOR_CHECKS: list[tuple[DoctorCheck, DoctorCheckFn]]

and attention additionally exposes `HOOKS: Hooks | None`. Tracks never edit
this file. Here the real `Services` are built (memory first, then writes with
memory's artifact store), the write handlers get the configured policy, and the
tick's steps are installed: crash recovery and run planning on every call (`ALWAYS_STEPS`) and the
sweeper plus write maintenance after the inbox is reconciled (`EXTRA_STEPS`).
"""

import sqlite3
from collections.abc import Callable
from importlib import import_module
from pathlib import Path

from chamberlain.app.context import Context, DoctorCheckFn, Handler, Hooks, SourceRouter
from chamberlain.app.core import App
from chamberlain.app.doctor import make_doctor
from chamberlain.app.services import Services
from chamberlain.attention import tick as attention_tick
from chamberlain.memory import services as memory_services
from chamberlain.memory.artifacts import Budgets
from chamberlain.memory.sweeper import Compactor, sweep
from chamberlain.ports.clock import Clock, SystemClock
from chamberlain.runs import maintenance as run_maintenance
from chamberlain.runs import plan as run_plan
from chamberlain.runs import updates as run_updates
from chamberlain.runs.updates import TagLister
from chamberlain.schema.base import ToolName
from chamberlain.schema.registry import BY_NAME
from chamberlain.schema.tools import DoctorCheck
from chamberlain.store.migrate import open_store
from chamberlain.writes import maintenance as write_maintenance
from chamberlain.writes import services as write_services
from chamberlain.writes.handlers import make_handlers
from chamberlain.writes.models import WritePolicy

TRACK_MODULES: tuple[str, ...] = (
    "chamberlain.attention.handlers",
    "chamberlain.memory.handlers",
    "chamberlain.writes.handlers",
)

WRITES_MODULE = "chamberlain.writes.handlers"
"""Its handlers depend on the configured policy, so they come from `make_handlers`."""

COMPACTORS: tuple[tuple[str, Compactor], ...] = (
    *write_maintenance.COMPACTORS,
    *run_maintenance.COMPACTORS,
)
"""Other tracks' lifetime rules for their own tables, run inside the sweeper's records step.

Attention trims its own events and sessions inside its tick, so it supplies none. The runs
track trims its run rows and notes."""


def load_tracks(
    policy: WritePolicy | None = None,
) -> tuple[dict[ToolName, Handler], Hooks | None]:
    """Collect handlers, doctor checks and hooks from the track modules."""
    handlers: dict[ToolName, Handler] = {}
    checks: list[tuple[DoctorCheck, DoctorCheckFn]] = []
    hooks: Hooks | None = None
    for module_name in TRACK_MODULES:
        module = import_module(module_name)
        track = module_name.split(".")[1]
        track_handlers: dict[ToolName, Handler] = (
            make_handlers(policy) if module_name == WRITES_MODULE else module.HANDLERS
        )
        for name, handler in track_handlers.items():
            if BY_NAME[name.value].owner != track:
                raise RuntimeError(f"{track} registered {name.value}, which it does not own")
            handlers[name] = handler
        checks.extend(module.DOCTOR_CHECKS)
        hooks = getattr(module, "HOOKS", None) or hooks
    handlers[ToolName.DOCTOR] = make_doctor(checks)
    return handlers, hooks


def build_services(
    db: sqlite3.Connection,
    clock: Clock,
    data_root: Path,
    *,
    policy: WritePolicy | None = None,
    budgets: Budgets | None = None,
) -> Services:
    """The real cross-track services over one connection (09-storage.md, S-3)."""
    artifacts, evidence, refs = memory_services.build(db, clock, data_root, budgets)
    audit, control = write_services.build(db, clock, policy=policy, artifacts=artifacts)
    return Services(audit=audit, artifacts=artifacts, evidence=evidence, refs=refs, writes=control)


def tick_steps(
    policy: WritePolicy | None, budgets: Budgets | None, update_lister: TagLister | None = None
) -> tuple[list[Callable[[Context], None]], list[Callable[[Context], None]]]:
    """`(always, extra)`: what the tick runs on every call, and what it runs when due."""

    def sweep_everything(ctx: Context) -> None:
        sweep(ctx, ctx.clock.now(), budgets=budgets, compactors=COMPACTORS)

    def maintain_writes(ctx: Context) -> None:
        write_maintenance.maintain(ctx, policy)

    always: list[Callable[[Context], None]] = [
        write_maintenance.recover_operations,
        run_plan.plan_runs,
    ]
    extra: list[Callable[[Context], None]] = [sweep_everything, maintain_writes]
    if update_lister is not None:
        extra.append(run_updates.tick_step(update_lister))
    return always, extra


def install_tick_steps(
    policy: WritePolicy | None = None,
    budgets: Budgets | None = None,
    update_lister: TagLister | None = None,
) -> None:
    """Fill attention's step lists in place. Idempotent: the last built app's settings win."""
    always, extra = tick_steps(policy, budgets, update_lister)
    attention_tick.ALWAYS_STEPS[:] = always
    attention_tick.EXTRA_STEPS[:] = extra


def build_app(
    data_root: Path,
    *,
    clock: Clock | None = None,
    sources: SourceRouter | None = None,
    services: Services | None = None,
    policy: WritePolicy | None = None,
    budgets: Budgets | None = None,
    update_lister: TagLister | None = None,
) -> App:
    """Open the store under `data_root` and assemble the app (S-18).

    `services` replaces the real ones (tests); `sources` is the connector router, which
    the configuration step fills (wave 2). `update_lister` turns the daily tag
    check on (`runs.updates`); without it no tick ever touches the network.
    """
    the_clock = clock or SystemClock()
    db = open_store(data_root, the_clock)
    context = Context(
        db=db,
        clock=the_clock,
        data_root=data_root,
        sources=sources or SourceRouter(),
        services=services
        or build_services(db, the_clock, data_root, policy=policy, budgets=budgets),
    )
    handlers, hooks = load_tracks(policy)
    install_tick_steps(policy, budgets, update_lister)
    return App(context, handlers, hooks)


def run_tick(app: App, *, force: bool = True) -> tuple[bool, int]:
    """The operator's tick: `(detectors ran, wake status)`.

    The wake status is 10 when events wait for a session and none is active, so a
    scheduler can start one (07-attention.md, delivery 3); 0 otherwise.
    """
    ran = attention_tick.tick(app.context, force=force)
    return ran, attention_tick.wake_status(app.context)
