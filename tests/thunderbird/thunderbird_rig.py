"""A full-stack rig: connector, client, native host and the real add-on in Node.

The add-on runs under Node against a scripted Thunderbird (`addon/test/harness.mjs`).
Frames travel exactly as in production: client -> loopback TCP -> native host ->
stdio pipes -> add-on. Faults are injected at the add-on or at its frame edge;
`inject` returns False for a fault this rig cannot produce.
"""

import base64
import json
import os
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from functools import cache
from pathlib import Path
from typing import Any, Final

from chamberlain.connectors.calendar_support import ics
from chamberlain.connectors.thunderbird.calendar import (
    OwlBinding,
    OwlCalendarConfig,
    ThunderbirdCalendar,
)
from chamberlain.connectors.thunderbird.client import BridgeClient, RuntimeConnector
from chamberlain.connectors.thunderbird.contacts import ContactsBinding, ThunderbirdContacts
from chamberlain.connectors.thunderbird.identity import (
    decode_locator,
    encode_contact_locator,
    encode_locator,
)
from chamberlain.connectors.thunderbird.mail import ThunderbirdMail
from chamberlain.connectors.thunderbird.mail_reads import MailLimits, ThunderbirdBinding
from chamberlain.connectors.thunderbird.native_host import (
    Deadlines,
    NativeHost,
    write_runtime,
)
from chamberlain.connectors.thunderbird.typegen import write_generated
from chamberlain.domain.calendar import EventFields, EventStatus
from chamberlain.domain.mail import FolderRole
from chamberlain.ports.calendar import CalendarPort
from chamberlain.ports.clock import ManualClock
from chamberlain.ports.contacts import ContactsPort
from chamberlain.ports.mail import MailPort
from support.rigs import Fault

ADDON_DIR: Final = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "chamberlain"
    / "connectors"
    / "thunderbird"
    / "addon"
)
HARNESS: Final = ADDON_DIR / "test" / "harness.mjs"
ACCOUNT: Final = "account1"
CALENDAR_ID: Final = "cal1"
MAILBOX: Final = "owner@example.org"
TOKEN: Final = "t" * 64
DEADLINES: Final = Deadlines(
    addon_read=2.0,
    addon_write=0.6,
    host_read=2.5,
    host_write=1.2,
    client_read=3.0,
    client_write=2.0,
)

_ROLE_IDS: Final = {
    FolderRole.INBOX: ACCOUNT + "://INBOX",
    FolderRole.SENT: ACCOUNT + "://Sent",
    FolderRole.DRAFTS: ACCOUNT + "://Drafts",
    FolderRole.TRASH: ACCOUNT + "://Trash",
    FolderRole.ARCHIVE: ACCOUNT + "://Archive",
}
_ROLE_NAMES: Final = {
    FolderRole.INBOX: "inbox",
    FolderRole.SENT: "sent",
    FolderRole.DRAFTS: "drafts",
    FolderRole.TRASH: "trash",
    FolderRole.ARCHIVE: "archive",
}
_FAULTS: Final = {
    Fault.LOSE_BEFORE_APPLY: "lose_before",
    Fault.LOSE_RESPONSE_AFTER_APPLY: "lose_response",
    Fault.BUSY_BEFORE_APPLY: "busy",
    Fault.DUPLICATE_COPY: "duplicate",
    Fault.SLOW: "slow",
    Fault.STALE: "stale",
}


_CALENDAR_FAULTS: Final = {
    Fault.LOSE_RESPONSE_AFTER_APPLY: "lose_response",
    Fault.BUSY_BEFORE_APPLY: "busy",
    Fault.SLOW: "calendar_slow",
    Fault.STALE: "calendar_offline",
}
# LOSE_BEFORE_APPLY is declined: the client calendar is a cache, so absence is never proven.


def node_available() -> bool:
    return shutil.which("node") is not None


@cache
def generated_types() -> Path:
    """Generate once before the first Node harness in this pytest process."""
    return write_generated()


class Stack:
    """Everything wired together for one test."""

    def __init__(self, workdir: Path) -> None:
        node = shutil.which("node")
        if node is None:
            raise RuntimeError("node is required for the Thunderbird rig")
        types = generated_types()
        self.clock = ManualClock()
        self._control_listener = socket.socket()
        self._control_listener.bind(("127.0.0.1", 0))
        self._control_listener.listen(1)
        port = self._control_listener.getsockname()[1]
        self._proc = subprocess.Popen(
            [
                node,
                "--no-warnings",
                str(HARNESS),
                str(port),
                str(int(DEADLINES.addon_read * 1000)),
                str(int(DEADLINES.addon_write * 1000)),
                "3000",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
            env={**os.environ, "CHAMBERLAIN_ADDON_TYPES": str(types)},
        )
        assert self._proc.stdin is not None
        assert self._proc.stdout is not None
        self._control_listener.settimeout(10)
        self._control, _ = self._control_listener.accept()
        self._control.settimeout(10)
        self._control_buffer = b""
        self._serial = 0
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        self.host = NativeHost(
            self._proc.stdout, self._proc.stdin, listener, token=TOKEN, deadlines=DEADLINES
        )
        self.host.start()
        write_runtime(workdir, port=self.host.port, token=TOKEN, pid=0)
        self.client = BridgeClient(RuntimeConnector(workdir), DEADLINES)
        binding = ThunderbirdBinding(
            account_id=ACCOUNT,
            mailbox=MAILBOX,
            inbox_folder_id=_ROLE_IDS[FolderRole.INBOX],
            sent_folder_id=_ROLE_IDS[FolderRole.SENT],
            draft_folder_id=_ROLE_IDS[FolderRole.DRAFTS],
            trash_folder_id=_ROLE_IDS[FolderRole.TRASH],
        )
        self.mail = ThunderbirdMail(
            self.client,
            {"mail:test": binding},
            self.clock,
            limits=MailLimits(sample_interval_seconds=0),
            sleep=lambda seconds: None,
        )
        self.contacts = ThunderbirdContacts(
            self.client, {"contacts:test": ContactsBinding(("book1",))}, self.clock
        )
        self.calendar = self.calendar_for(CALENDAR_ID)
        self._wait_for_addon()

    def _wait_for_addon(self) -> None:
        end = time.monotonic() + 10
        while not self.host.addon_connected:
            if time.monotonic() > end:
                raise RuntimeError("the add-on never said hello")
            time.sleep(0.01)

    def control(self, op: str, **args: Any) -> Any:
        self._serial += 1
        line = json.dumps({"id": self._serial, "op": op, "args": args}).encode() + b"\n"
        self._control.sendall(line)
        while b"\n" not in self._control_buffer:
            chunk = self._control.recv(65536)
            if not chunk:
                raise RuntimeError("the harness closed its control channel")
            self._control_buffer += chunk
        raw, _, self._control_buffer = self._control_buffer.partition(b"\n")
        reply = json.loads(raw)
        if "error" in reply:
            raise RuntimeError(reply["error"])
        return reply["result"]

    def close(self) -> None:
        assert self._proc.stdin is not None
        assert self._proc.stdout is not None
        self._proc.stdin.close()
        try:
            self._proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait()
        self.host.stop()
        self._proc.stdout.close()
        self._control.close()
        self._control_listener.close()

    # --- the MailRig surface ------------------------------------------------------------

    def folder_id(self, role: FolderRole) -> str:
        return _ROLE_IDS[role]

    def filing_folder_id(self) -> str:
        return ACCOUNT + "://Filed"

    def new_message_id(self) -> str:
        self._serial += 1
        return f"<contract-{self._serial}@fake.example>"

    def seed(
        self,
        *,
        internet_message_id: str,
        folder: FolderRole = FolderRole.INBOX,
        subject: str = "Contract test",
        body: str = "Body text.",
        unread: bool = True,
        date_ms: int | None = None,
        attachments: tuple[tuple[str, bytes], ...] = (),
    ) -> str:
        found = self.control(
            "seed",
            role=_ROLE_NAMES[folder],
            id=internet_message_id.strip("<>"),
            subject=subject,
            body=body,
            unread=unread,
            date_ms=date_ms,
            attachments=[
                {"name": name, "b64": base64.b64encode(data).decode()} for name, data in attachments
            ],
        )
        return encode_locator(ACCOUNT, found["folder_id"], internet_message_id)

    def copies(self, internet_message_id: str, folder_id: str | None = None) -> int:
        return int(self.control("copies", id=internet_message_id, folder_id=folder_id))

    def is_unread(self, locator: str) -> bool:
        where = decode_locator(locator, account_id=ACCOUNT)
        return bool(self.control("unread", folder_id=where.folder_id, id=where.message_id))

    def inject(self, fault: Fault) -> bool:
        name = _FAULTS.get(fault)
        if name is None:
            return False
        return bool(self.control("inject", fault=name))

    def restart(self) -> bool:
        return bool(self.control("restart"))

    def advance(self, seconds: float) -> None:
        self.clock.advance(seconds=seconds)
        self.control("advance")

    # --- the CalendarRig surface --------------------------------------------------------

    def calendar_for(self, calendar_id: str) -> ThunderbirdCalendar:
        return ThunderbirdCalendar(
            OwlCalendarConfig(bindings={"calendar:test": OwlBinding(calendar_id=calendar_id)}),
            self.client,
            self.clock,
        )

    def new_event_uid(self) -> str:
        self._serial += 1
        return f"owl-contract-{self._serial}"

    def seed_event(self, fields: EventFields, uid: str) -> str:
        text = ics.build_new(uid, fields, self.clock.now())
        self.control("calendar_seed", calendar_id=CALENDAR_ID, id=uid, icalendar=text)
        return uid

    def event_count(self, uid: str) -> int:
        text = self.control("calendar_text", calendar_id=CALENDAR_ID, id=uid)
        if text is None:
            return 0
        return int(ics.master_of(str(text)).status is not EventStatus.CANCELLED)

    # --- the ContactsRig surface --------------------------------------------------------

    def seed_contact(self, name: str, *emails: str) -> str:
        self._serial += 1
        contact_id = f"c{self._serial}"
        self.control("contact", book="book1", id=contact_id, name=name, emails=list(emails))
        return encode_contact_locator("book1", contact_id)


class MailRigAdapter:
    """`MailRig` over a `Stack`."""

    def __init__(self, stack: Stack) -> None:
        self._stack = stack
        self.port: MailPort = stack.mail
        self.source = "mail:test"
        self.folder_id = stack.folder_id
        self.filing_folder_id = stack.filing_folder_id
        self.new_message_id = stack.new_message_id
        self.seed = stack.seed
        self.copies = stack.copies
        self.is_unread = stack.is_unread
        self.inject = stack.inject
        self.restart = stack.restart
        self.advance = stack.advance


class CalendarRigAdapter:
    """`CalendarRig` over a `Stack`: the real add-on and a scripted calendar provider."""

    source = "calendar:test"

    def __init__(self, stack: Stack) -> None:
        self._stack = stack
        self.port: CalendarPort = stack.calendar
        self.count = stack.event_count
        self.advance = stack.advance
        self.restart = stack.restart

    def new_uid(self) -> str:
        return self._stack.new_event_uid()

    def seed(self, fields: EventFields) -> str:
        return self._stack.seed_event(fields, self.new_uid())

    def seed_invitation(self, organiser: str, fields: EventFields) -> str | None:
        return None  # the Owl calendar cannot send or answer invitations (W-34)

    def inject(self, fault: Fault) -> bool:
        name = _CALENDAR_FAULTS.get(fault)
        return name is not None and bool(self._stack.control("inject", fault=name))


class ContactsRigAdapter:
    def __init__(self, stack: Stack) -> None:
        self.port: ContactsPort = stack.contacts
        self.source = "contacts:test"
        self.seed = stack.seed_contact


@contextmanager
def running(workdir: Path) -> Iterator[Stack]:
    stack = Stack(workdir)
    try:
        yield stack
    finally:
        stack.close()


__all__ = [
    "CALENDAR_ID",
    "CalendarRigAdapter",
    "ContactsRigAdapter",
    "MailRigAdapter",
    "Stack",
    "node_available",
    "running",
]
