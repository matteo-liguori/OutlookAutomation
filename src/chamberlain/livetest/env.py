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
from datetime import datetime
from pathlib import Path
from typing import Protocol

from chamberlain.app.context import Context
from chamberlain.domain.calendar import CalendarQuery
from chamberlain.domain.mail import Folder, FolderRole, MailQuery
from chamberlain.ports.clock import Clock
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

    def events(self, source: str, text: str, start: datetime, end: datetime) -> list[CalendarCopy]:
        """Every event (cancelled too) whose title contains `text`, in the window."""
        ...


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
        self._bridge_ok = bridge_dir is None or bridge_dir == data_root / "bridge"

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

    def copies(self, source: str, text: str) -> list[MailCopy]:
        port = self._ctx.sources.mail_port(source)
        roles = {f.id: f.role for f in port.folders(source)}
        found: list[MailCopy] = []
        cursor: str | None = None
        for _ in range(10):
            page = port.search(source, MailQuery(text=text, limit=200), cursor)
            for item in page.items:
                if text in item.subject:
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
            cursor = page.cursor
            if cursor is None:
                break
        return found

    def in_reply_to(self, source: str, copy: MailCopy) -> str:
        return self._ctx.sources.mail_port(source).read(source, copy.locator).in_reply_to

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
            )
            for item in page.items
            if text in item.fields.title
        ]


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
