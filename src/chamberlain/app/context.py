"""What a handler is given, and what it gives back.

Contract: docs/contracts/01-domain.md (D-12: state arrives through Context)
and docs/contracts/02-tools.md (T-16). Shared file (docs/OWNERSHIP.md).
"""

import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from chamberlain.app.services import Services
from chamberlain.ports.calendar import CalendarPort
from chamberlain.ports.clock import Clock
from chamberlain.ports.contacts import ContactsPort
from chamberlain.ports.mail import MailPort
from chamberlain.schema.base import Model
from chamberlain.schema.envelope import Coverage, NextStep, Reply
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.schema.registry import ToolSpec
from chamberlain.schema.tools import CheckResult, ToolRequest


@dataclass(frozen=True)
class SourceRouter:
    """Source ID to port. The only way the core reaches a connector (P-1)."""

    mail: Mapping[str, MailPort] = field(default_factory=dict[str, MailPort])
    calendar: Mapping[str, CalendarPort] = field(default_factory=dict[str, CalendarPort])
    contacts: Mapping[str, ContactsPort] = field(default_factory=dict[str, ContactsPort])
    demo: frozenset[str] = frozenset()
    """Source IDs backed by synthetic demo data. Replies are marked while any exist."""
    information_only: frozenset[str] = frozenset()
    """Calendars the owner keeps for information (`commitments: false`): no clash checks."""

    def mail_port(self, source: str) -> MailPort:
        return _route(self.mail, source)

    def calendar_port(self, source: str) -> CalendarPort:
        return _route(self.calendar, source)

    def contacts_port(self, source: str) -> ContactsPort:
        return _route(self.contacts, source)

    def source_ids(self) -> list[str]:
        return sorted({*self.mail, *self.calendar, *self.contacts})


def _route[PortT](ports: Mapping[str, PortT], source: str) -> PortT:
    port = ports.get(source)
    if port is None:
        raise ChamberlainError(ErrorCode.SOURCE_NOT_CONFIGURED, "That source is not set up.")
    return port


TickStep = Callable[["Context"], None]
"""A step another track contributes to the tick. It does its own transaction and may raise."""


@dataclass(frozen=True)
class TickSteps:
    """What the tick runs besides attention's own work. Built with the app (`app/wiring.py`)."""

    always: tuple[TickStep, ...] = ()
    """Every call, even a skipped tick, before detection: crashed-operation recovery (W-9)."""
    extra: tuple[TickStep, ...] = ()
    """After the inbox is reconciled, at most once per interval: the sweeper (M-21)."""
    background: tuple[TickStep, ...] = ()
    """Only in the operating system scheduler's tick (`chamberlain tick`), never inside a
    tool call, because they may take minutes: the owner's mail clean-up."""


@dataclass
class SyncBudget:
    """The one overall time limit for all the folder syncing inside a single tool call.

    The clock starts the first time something syncs; sources then take what is left (A-25).
    """

    started: datetime | None = None


@dataclass(frozen=True)
class Context:
    db: sqlite3.Connection
    clock: Clock
    data_root: Path
    sources: SourceRouter
    services: Services
    session: str | None = None
    """Set by the app from the request, after validation (T-3)."""
    tick_steps: TickSteps = TickSteps()
    """The tick's extra steps belong to the context, so two apps never share them (A-6)."""
    sync_budget: SyncBudget | None = None
    """Set per tool call and per operator tick. None: each source keeps its own limit."""


@dataclass(frozen=True)
class Result:
    """What a handler returns. The app wraps it in the envelope."""

    data: Model
    coverage: Coverage | None = None
    next: tuple[NextStep, ...] = ()


Handler = Callable[[Context, Any], Result]
"""`handler(ctx, request)` where request is the tool's validated request model.

A handler raises `ChamberlainError` for anything the agent can meet (E-14).
"""

DoctorCheckFn = Callable[[Context], list[CheckResult]]


class Hooks(Protocol):
    """Implemented by attention (07-attention.md, A-6, A-19)."""

    def guard(self, ctx: Context, spec: ToolSpec, request: ToolRequest) -> None:
        """Raise `ChamberlainError` to stop any tool before its handler (R10)."""
        ...

    def tick(self, ctx: Context) -> None:
        """Run before every handler. Must not raise (A-7)."""
        ...

    def decorate(
        self, ctx: Context, spec: ToolSpec, request: ToolRequest, reply: Reply[Model]
    ) -> Reply[Model]:
        """Add `next` (if empty), `guidance` and `notices`. Nothing else (A-19)."""
        ...
