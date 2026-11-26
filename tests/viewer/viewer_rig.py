"""The real wired app behind a `ViewerApp`, called with WSGI environs and no sockets."""

import io
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from chamberlain.app.context import SourceRouter
from chamberlain.app.core import App
from chamberlain.app.wiring import build_app
from chamberlain.connectors.fake import FakeProvider
from chamberlain.domain.intents import MailSend
from chamberlain.ports.clock import ManualClock
from chamberlain.surface.viewer.app import ViewerApp
from chamberlain.writes.confirm import Response, create_token
from chamberlain.writes.core import Engine
from chamberlain.writes.models import Proposal, WritePolicy

MAIL = "mail:test"
CALENDAR = "calendar:test"
ORIGIN = "http://127.0.0.1:5555"
HOST = "127.0.0.1:5555"


class Rig:
    def __init__(self, root: Path) -> None:
        self.clock = ManualClock(datetime(2026, 1, 5, 9, 0, tzinfo=UTC))
        self.fake = FakeProvider(self.clock)
        self.fake.mail.add_source(MAIL)
        self.fake.calendar.add_source(CALENDAR)
        self.policy = WritePolicy()
        self.app: App = build_app(
            root,
            clock=self.clock,
            sources=SourceRouter(
                mail={MAIL: self.fake.mail}, calendar={CALENDAR: self.fake.calendar}
            ),
        )
        self.token = create_token(root)
        self.viewer = ViewerApp(
            lambda: self.app.context, token=self.token, origin=ORIGIN, policy=self.policy
        )

    @property
    def ctx(self) -> Any:
        return self.app.context

    def engine(self) -> Engine:
        return Engine(self.app.context, self.policy)

    def proposal(self, to: str = "stranger@example.org", subject: str = "Hello") -> Proposal:
        made = self.engine().propose_intent(
            MailSend(source=MAIL, to=(to,), subject=subject, body="The exact body text.")
        )
        assert made.grant is not None
        return made

    def call(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        token: str | None = "",
        origin: str | None = ORIGIN,
        host: str | None = HOST,
        remote: str = "127.0.0.1",
        content_type: str | None = "application/json",
        headers: dict[str, str] | None = None,
    ) -> Response:
        """One request through the WSGI app; returns status and decoded JSON body."""
        raw = json.dumps(body).encode() if body is not None else b""
        target, _, query = path.partition("?")
        environ: dict[str, Any] = {
            "REQUEST_METHOD": method,
            "PATH_INFO": target,
            "QUERY_STRING": query,
            "REMOTE_ADDR": remote,
            "CONTENT_LENGTH": str(len(raw)),
            "wsgi.input": io.BytesIO(raw),
        }
        if host is not None:
            environ["HTTP_HOST"] = host
        if token is not None:
            environ["HTTP_X_CHAMBERLAIN_TOKEN"] = self.token if token == "" else token
        if origin is not None:
            environ["HTTP_ORIGIN"] = origin
        if content_type is not None:
            environ["CONTENT_TYPE"] = content_type
        for name, value in (headers or {}).items():
            environ["HTTP_" + name.upper().replace("-", "_")] = value
        seen: dict[str, Any] = {}

        def start_response(status: str, response_headers: list[tuple[str, str]], *_: Any) -> None:
            seen["status"] = int(status.split()[0])
            seen["headers"] = dict(response_headers)

        chunks = b"".join(self.viewer(environ, start_response))
        self.last_headers: dict[str, str] = seen["headers"]
        self.last_bytes = chunks
        try:
            decoded = json.loads(chunks) if chunks else {}
        except ValueError:
            decoded = {}
        return Response(seen["status"], decoded)

    def rows(self, sql: str, *params: object) -> list[Any]:
        return list(self.ctx.db.execute(sql, params))

    def close(self) -> None:
        self.app.close()
