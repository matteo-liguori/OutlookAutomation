"""What differs between a real installation and the fake world.

Contract: docs/live-test.md. The runner takes an `Environment` and never asks which kind it
is: the waiter sleeps (or moves a manual clock), the fault injector arms a lost response
(a file the native host reads, or a fake switch), and the probe is a read-only view of the
ports for the few checks the tools cannot make. The report names every check that used it.
"""

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, cast

from chamberlain.app.context import Context
from chamberlain.domain.calendar import CalendarEvent, CalendarQuery
from chamberlain.domain.mail import Folder, FolderRole, MailQuery
from chamberlain.domain.mail import strip_reply_prefix as strip_reply_prefix
from chamberlain.ports.clock import Clock
from chamberlain.ports.page import pages
from chamberlain.ports.sync import FolderSync
from chamberlain.schema.errors import ChamberlainError

FAULT_FILE = "livetest-fault.json"
FAULT_DROP_RESPONSE = "drop_response_after_dispatch"


@dataclass(frozen=True)
class MailCopy:
    """One physical copy of a message, as the port lists it."""

    source: str
    locator: str
    folder_id: str
    folder_role: FolderRole
    message_id: str
    subject: str
    unread: bool


@dataclass(frozen=True)
class CalendarCopy:
    source: str
    locator: str
    title: str
    status: str
    start: datetime
    series: str | None = None
    """The locator of the series this is one date of, when it is one."""


class Waiter(Protocol):
    def sleep(self, seconds: float) -> None: ...


class FaultInjector(Protocol):
    def why_unavailable(self) -> str | None:
        """None when a lost response can be armed here, else the reason it cannot."""
        ...

    def arm_lost_response(self, run_id: str) -> None: ...

    def fired(self, run_id: str) -> bool:
        """True once the fault was used (the file is consumed, or the fake switch is spent)."""
        ...

    def disarm(self) -> None: ...


class Probe(Protocol):
    def folders(self, source: str) -> list[Folder]: ...

    def copies(self, source: str, text: str) -> list[MailCopy]:
        """Every copy whose subject contains `text`, in every folder."""
        ...

    def in_reply_to(self, source: str, copy: MailCopy) -> str: ...

    def sync_inbox(self, source: str) -> None:
        """Ask the client to fetch new mail into the inbox now (read-only; never raises)."""
        ...

    def events(self, source: str, text: str, start: datetime, end: datetime) -> list[CalendarCopy]:
        """Every event (cancelled too) whose title contains `text`, in the window."""
        ...

    def read_event(self, source: str, locator: str) -> CalendarEvent: ...

    def answers(self, source: str, locator: str) -> dict[str, str]:
        """`{address: answer}` the calendar holds for an event's guests (empty if unknown)."""
        ...


INBOX_SYNC_SECONDS = 20
"""How long an arrival check waits for the inbox to sync before searching it."""
MARKED_MAIL_DAYS = 30
"""How far back the probe looks for marked mail; runs and sweeps are recent."""


class RealWaiter:
    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class ClockWaiter:
    """Moves a manual clock instead of waiting (the fake world)."""

    def __init__(self, advance: Callable[[float], None]) -> None:
        self._advance = advance

    def sleep(self, seconds: float) -> None:
        self._advance(seconds)


class FileFaultInjector:
    """Arms the native host's lost-response hook by writing `<data root>/run/livetest-fault.json`.

    The host fires it once, only for a `send` whose subject carries this run's marker, and
    consumes the file. Without the default layout (`<data root>/bridge`) the host cannot find
    the file, so the scenario is skipped rather than guessed.
    """

    def __init__(self, data_root: Path, bridge_dir: Path | None) -> None:
        self._path = data_root / "run" / FAULT_FILE
        default = (data_root / "bridge").resolve()
        given = None if bridge_dir is None else bridge_dir
        if given is not None and not given.is_absolute():
            given = data_root / given  # a relative path in the profile is read from the data root
        self._bridge_ok = given is None or given.resolve() == default

    def why_unavailable(self) -> str | None:
        if not self._bridge_ok:
            return "the bridge directory is not <data root>/bridge, so the host cannot see the file"
        return None

    def arm_lost_response(self, run_id: str) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        pending = self._path.with_suffix(".tmp")
        body = {"run_id": run_id, "fault": FAULT_DROP_RESPONSE, "cmd": "send"}
        pending.write_text(json.dumps(body), encoding="utf-8")
        os.replace(pending, self._path)

    def fired(self, run_id: str) -> bool:
        return not self._path.exists()

    def disarm(self) -> None:
        self._path.unlink(missing_ok=True)


class PortProbe:
    """Read-only: lists and reads through the ports, never changes anything (P-3)."""

    def __init__(self, ctx: Context) -> None:
        self._ctx = ctx

    def folders(self, source: str) -> list[Folder]:
        return self._ctx.sources.mail_port(source).folders(source)

    def sync_inbox(self, source: str) -> None:
        # A folder counts as fresh for minutes after a sync, so a reply that lands meanwhile
        # stays invisible until the client checks the server by itself (found live,
        # 2026-11-25: arrivals were missed though the mail had come).
        port = self._ctx.sources.mail_port(source)
        if not isinstance(port, FolderSync):
            return
        try:
            inbox = [f.id for f in port.folders(source) if f.role is FolderRole.INBOX]
            if inbox:
                port.sync_folders(source, inbox, timedelta(seconds=INBOX_SYNC_SECONDS))
        except ChamberlainError:
            return

    def copies(self, source: str, text: str) -> list[MailCopy]:
        port = self._ctx.sources.mail_port(source)
        roles = {f.id: f.role for f in port.folders(source)}
        found: list[MailCopy] = []
        since = self._ctx.clock.now() - timedelta(days=MARKED_MAIL_DAYS)
        query = MailQuery(text=text, since=since, limit=200)
        for page in pages(lambda cursor: port.search(source, query, cursor), limit=10):
            for item in page.items:
                if strip_reply_prefix(text) in item.subject:
                    found.append(
                        MailCopy(
                            source=source,
                            locator=item.locator,
                            folder_id=item.folder_id,
                            folder_role=roles.get(item.folder_id, FolderRole.OTHER),
                            message_id=item.internet_message_id,
                            subject=item.subject,
                            unread=item.unread,
                        )
                    )
        return found

    def in_reply_to(self, source: str, copy: MailCopy) -> str:
        return self._ctx.sources.mail_port(source).read(source, copy.locator).in_reply_to

    def answers(self, source: str, locator: str) -> dict[str, str]:
        return _answers_of(self.read_event(source, locator))

    def read_event(self, source: str, locator: str) -> CalendarEvent:
        return self._ctx.sources.calendar_port(source).read(source, locator)

    def events(self, source: str, text: str, start: datetime, end: datetime) -> list[CalendarCopy]:
        port = self._ctx.sources.calendar_port(source)
        try:
            page = port.events(
                source,
                CalendarQuery(start=start, end=end, text=text, include_cancelled=True, limit=250),
            )
        except ChamberlainError:
            return []
        return [
            CalendarCopy(
                source=source,
                locator=item.locator,
                title=item.fields.title,
                status=item.status.value,
                start=item.fields.start,
                series=item.series_locator if item.is_occurrence else None,
            )
            for item in page.items
            if text in item.fields.title
        ]


def _answers_of(event: object) -> dict[str, str]:
    """Per-guest answers when the calendar port reports them (an older port does not)."""
    found: dict[str, str] = {}
    for item in cast("tuple[Any, ...]", getattr(event, "participants", ())):
        response = getattr(item, "response", None)
        found[str(getattr(item, "address", ""))] = str(getattr(response, "value", response))
    return found


@dataclass
class Environment:
    """Everything the runner needs that differs between live and the fake."""

    mode: str
    clock: Clock
    waiter: Waiter
    faults: FaultInjector
    probe: Probe
    dispatches: Callable[[], int | None] = field(default=lambda: None)
    """How many writes reached the provider so far, when the environment can count them."""
