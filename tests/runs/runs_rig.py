"""A real wired app over the fake provider and a manual clock, for the runs tests."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from chamberlain.app.context import SourceRouter
from chamberlain.app.core import App
from chamberlain.app.wiring import build_app
from chamberlain.connectors.fake import FakeProvider
from chamberlain.ports.clock import ManualClock

MAIL = "mail:test"


class Rig:
    def __init__(self, root: Path) -> None:
        self.clock = ManualClock(datetime(2026, 1, 5, 6, 0, tzinfo=UTC))
        fake = FakeProvider(self.clock)
        fake.mail.add_source(MAIL)
        self.app: App = build_app(
            root, clock=self.clock, sources=SourceRouter(mail={MAIL: fake.mail})
        )
        self.session: str | None = None

    @property
    def ctx(self) -> Any:
        return self.app.context

    def call(self, tool: str, **request: Any) -> dict[str, Any]:
        if self.session is not None and "session" not in request:
            request["session"] = self.session
        reply = self.app.call(tool, request)
        return reply.model_dump(mode="json", exclude_none=True)

    def start(self, **request: Any) -> dict[str, Any]:
        reply = self.call("start", agent="test", **request)
        if reply["ok"]:
            self.session = reply["data"]["session"]
        return reply

    def rows(self, sql: str, *params: object) -> list[Any]:
        return list(self.ctx.db.execute(sql, params))
