"""Fixtures for the integration scenarios: the real app over the fake provider."""

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

MAIL = "mail:test"
CALENDAR = "calendar:test"
CONTACTS = "contacts:test"


@dataclass
class Stack:
    """The wired app, the fake provider behind it and the clock they share."""

    app: App
    fake: FakeProvider
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
