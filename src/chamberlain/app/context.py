"""What a handler is given, and what it gives back.

Contract: docs/contracts/01-domain.md (D-12: state arrives through Context)
and docs/contracts/02-tools.md (T-16).
"""

import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, Self

from chamberlain.domain.testmark import carries_marker
from chamberlain.memory.artifacts import ArtifactService, Budgets
from chamberlain.memory.evidence import EvidenceService
from chamberlain.memory.refs import RefService
from chamberlain.ports.calendar import CalendarPort
from chamberlain.ports.clock import Clock
from chamberlain.ports.contacts import ContactsPort
from chamberlain.ports.mail import MailPort
from chamberlain.schema.base import Model
from chamberlain.schema.envelope import Coverage, NextStep, Reply
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.schema.registry import ToolSpec
from chamberlain.schema.tools import CheckResult, ToolRequest
from chamberlain.writes.audit import AuditChain
from chamberlain.writes.control import Control
from chamberlain.writes.models import WritePolicy


@dataclass(frozen=True)
class SourceRouter:
    """Source ID to port. The only way the core reaches a connector (P-1)."""

    mail: Mapping[str, MailPort] = field(default_factory=dict[str, MailPort])
    calendar: Mapping[str, CalendarPort] = field(default_factory=dict[str, CalendarPort])
    contacts: Mapping[str, ContactsPort] = field(default_factory=dict[str, ContactsPort])
    information_only: frozenset[str] = frozenset()
    """Calendars the owner keeps for information (`commitments: false`): no clash checks."""

    test_only: frozenset[str] = frozenset()
    """Sources only the live test uses (`test_only: true`): daily operation skips them."""
    hide_test_marked: bool = False
    """Daily operation: calendar events carrying the live-test marker are left out of views
    and the calendar collect. The live test's own app leaves this false."""

    def daily_mail(self) -> list[str]:
        """Mailboxes daily operation works on, sorted: every mail source but test-only ones."""
        return sorted(s for s in self.mail if s not in self.test_only)

    def is_hidden_event_title(self, title: str) -> bool:
        """True when daily operation leaves out a calendar event with this title."""
        return self.hide_test_marked and carries_marker(title)

    def daily_calendars(self) -> list[str]:
        return sorted(s for s in self.calendar if s not in self.test_only)

    def daily_contacts(self) -> list[str]:
        return sorted(s for s in self.contacts if s not in self.test_only)

    def daily_source_ids(self) -> list[str]:
        return [s for s in self.source_ids() if s not in self.test_only]

    def mail_port(self, source: str) -> MailPort:
        return _route(self.mail, source)

    def calendar_port(self, source: str) -> CalendarPort:
        return _route(self.calendar, source)

    def contacts_port(self, source: str) -> ContactsPort:
        return _route(self.contacts, source)

    def port(self, source: str) -> MailPort | CalendarPort | ContactsPort:
        """The port that owns `source`, by its id's role: `mail:`, `calendar:`, else contacts."""
        pick = {"mail": self.mail_port, "calendar": self.calendar_port}
        return pick.get(source.partition(":")[0], self.contacts_port)(source)

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
    on_start: tuple["StartStep", ...] = ()
    """When an agent calls `start`, before its work is offered: the owner's mail clean-up.
    Each returns lines for the agent's report; a failing step never fails `start`."""


StartStep = Callable[["Context", str], list[str]]
"""`(context, the starting agent's name)` -> report lines."""


@dataclass
class SyncBudget:
    """The one overall time limit for all the folder syncing inside a single tool call.

    The clock starts the first time something syncs; sources then take what is left (A-25).
    """

    started: datetime | None = None


@dataclass(frozen=True)
class Services:
    audit: AuditChain
    artifacts: ArtifactService
    evidence: EvidenceService
    refs: RefService
    writes: Control

    @classmethod
    def bind(
        cls,
        db: sqlite3.Connection,
        clock: Clock,
        data_root: Path,
        *,
        policy: WritePolicy,
        budgets: Budgets | None = None,
    ) -> Self:
        """Bind all services to the same store and clock (S-3)."""
        artifacts = ArtifactService(db, clock, data_root, budgets)
        audit = AuditChain(db, clock)
        return cls(
            audit=audit,
            artifacts=artifacts,
            evidence=EvidenceService(db, clock),
            refs=RefService(db, clock),
            writes=Control(db, clock, audit, policy, artifacts),
        )


@dataclass(frozen=True)
class Context:
    db: sqlite3.Connection
    clock: Clock
    data_root: Path
    sources: SourceRouter
    services: Services
    write_policy: WritePolicy = field(default_factory=WritePolicy)
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
