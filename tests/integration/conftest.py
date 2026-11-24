"""Fixtures for the integration scenarios: the real app over the fake provider."""

import importlib
import os
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from chamberlain.app.context import SourceRouter
from chamberlain.app.core import App
from chamberlain.app.wiring import build_app
from chamberlain.connectors.fake import FakeMail, FakeProvider
from chamberlain.domain.operations import Prepared, ReconcileResult, ReconcileStatus
from chamberlain.ports.clock import ManualClock
from chamberlain.store.migrate import stamp
from chamberlain.writes import repo

MAIL = "mail:test"
CALENDAR = "calendar:test"
CONTACTS = "contacts:test"


@dataclass
class Stack:
    """The wired app, the fake provider behind it and the clock they share."""

    app: App
    fake: Any
    """The fake provider, or the Thunderbird rig (`seed`, `inject`, `copies`)."""
    clock: ManualClock
    root: Path
    session: str | None = None

    def call(self, tool: str, **request: Any) -> dict[str, Any]:
        """One tool call; returns the envelope as plain JSON."""
        if self.session is not None and "session" not in request:
            request["session"] = self.session
        reply = self.app.call(tool, request)
        return reply.model_dump(mode="json", exclude_none=True)

    def ok(self, tool: str, **request: Any) -> dict[str, Any]:
        """A call that must succeed; returns its `data`."""
        reply = self.call(tool, **request)
        assert reply["ok"], reply.get("error")
        data: dict[str, Any] = reply["data"]
        return data

    def know(self, *addresses: str) -> None:
        """Make addresses known recipients, as a human-confirmed grant would (G-15)."""
        db = self.app.context.db
        stored = stamp(self.clock)
        db.execute(
            "INSERT OR IGNORE INTO grants (handle, kind, state, source_id, title, requirement, "
            "content_sha256, proposed_at) VALUES ('grant_knownseed00', 'one_off', 'spent', ?, "
            "'seed', 'human_confirmation', ?, ?)",
            (MAIL, "0" * 64, stored),
        )
        for address in addresses:
            repo.add_known_recipient(db, MAIL, address, "grant_knownseed00", stored)

    def start(self, **request: Any) -> dict[str, Any]:
        data = self.ok("start", agent="test", **request)
        self.session = data["session"]
        return data


class UnknownReconciles:
    """A mail port that cannot answer its first `count` reconciles (a lost line, a restart)."""

    def __init__(self, inner: FakeMail, count: int) -> None:
        self._inner = inner
        self.remaining = count

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def reconcile(self, prepared: Prepared, key: str) -> ReconcileResult:
        if self.remaining > 0:
            self.remaining -= 1
            return ReconcileResult(status=ReconcileStatus.UNKNOWN, detail="no answer")
        return self._inner.reconcile(prepared, key)


def build_stack(root: Path, mail_wrapper: Callable[[FakeMail], Any] | None = None) -> Stack:
    clock = ManualClock()
    fake = FakeProvider(clock)
    fake.mail.add_source(MAIL)
    fake.calendar.add_source(CALENDAR)
    fake.contacts.add_source(CONTACTS)
    mail: Any = fake.mail if mail_wrapper is None else mail_wrapper(fake.mail)
    sources = SourceRouter(
        mail={MAIL: mail},
        calendar={CALENDAR: fake.calendar},
        contacts={CONTACTS: fake.contacts},
    )
    return Stack(build_app(root, clock=clock, sources=sources), fake, clock, root)


@pytest.fixture
def stack(tmp_path: Path) -> Iterator[Stack]:
    built = build_stack(tmp_path / "data")
    try:
        yield built
    finally:
        built.app.close()


@pytest.fixture
def thunderbird_stack(tmp_path: Path) -> Iterator[Stack]:
    """The same app over the Thunderbird connector, the native host and the add-on under Node."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "thunderbird"))
    thunderbird_rig: Any = importlib.import_module("thunderbird_rig")
    if not thunderbird_rig.node_available():
        if os.environ.get("CHAMBERLAIN_ALLOW_NO_NODE") == "1":
            pytest.skip("node is not installed (CHAMBERLAIN_ALLOW_NO_NODE=1)")
        pytest.fail("Node.js is required for the Thunderbird scenario (install Node 22 or newer)")
    bridge = tmp_path / "bridge"
    bridge.mkdir()
    with thunderbird_rig.running(bridge) as rig:
        sources = SourceRouter(mail={MAIL: rig.mail}, contacts={CONTACTS: rig.contacts})
        app = build_app(tmp_path / "data", clock=rig.clock, sources=sources)
        try:
            yield Stack(app, rig, rig.clock, tmp_path / "data")
        finally:
            app.close()
