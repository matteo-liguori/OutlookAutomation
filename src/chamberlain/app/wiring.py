"""Assembling the app from the tracks.

Each track exposes, in its own `handlers.py`:

    HANDLERS: dict[ToolName, Handler]
    DOCTOR_CHECKS: list[tuple[DoctorCheck, DoctorCheckFn]]

and attention additionally exposes `HOOKS: Hooks | None`. Tracks never edit
this file; they fill those names. Wiring real `Services` and connectors is
the integration step after wave 1 (docs/OWNERSHIP.md).
"""

from importlib import import_module
from pathlib import Path

from chamberlain.app.context import Context, DoctorCheckFn, Handler, Hooks, SourceRouter
from chamberlain.app.core import App
from chamberlain.app.doctor import make_doctor
from chamberlain.app.doubles import unwired_services
from chamberlain.app.services import Services
from chamberlain.ports.clock import Clock, SystemClock
from chamberlain.schema.base import ToolName
from chamberlain.schema.registry import BY_NAME
from chamberlain.schema.tools import DoctorCheck
from chamberlain.store.migrate import open_store

TRACK_MODULES: tuple[str, ...] = (
    "chamberlain.attention.handlers",
    "chamberlain.memory.handlers",
    "chamberlain.writes.handlers",
)


def load_tracks() -> tuple[dict[ToolName, Handler], Hooks | None]:
    """Collect handlers, doctor checks and hooks from the track modules."""
    handlers: dict[ToolName, Handler] = {}
    checks: list[tuple[DoctorCheck, DoctorCheckFn]] = []
    hooks: Hooks | None = None
    for module_name in TRACK_MODULES:
        module = import_module(module_name)
        track = module_name.split(".")[1]
        track_handlers: dict[ToolName, Handler] = module.HANDLERS
        for name, handler in track_handlers.items():
            if BY_NAME[name.value].owner != track:
                raise RuntimeError(f"{track} registered {name.value}, which it does not own")
            handlers[name] = handler
        checks.extend(module.DOCTOR_CHECKS)
        hooks = getattr(module, "HOOKS", None) or hooks
    handlers[ToolName.DOCTOR] = make_doctor(checks)
    return handlers, hooks


def build_app(
    data_root: Path,
    *,
    clock: Clock | None = None,
    sources: SourceRouter | None = None,
    services: Services | None = None,
) -> App:
    """Open the store under `data_root` and assemble the app (S-18)."""
    the_clock = clock or SystemClock()
    db = open_store(data_root, the_clock)
    context = Context(
        db=db,
        clock=the_clock,
        data_root=data_root,
        sources=sources or SourceRouter(),
        services=services or unwired_services(),
    )
    handlers, hooks = load_tracks()
    return App(context, handlers, hooks)
