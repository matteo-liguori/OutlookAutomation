"""Starting the viewer: bind loopback, make the token, open the page.

Contract: docs/contracts/05-grants.md (G-17, G-18). The server is the standard library's
single-threaded WSGI server bound to 127.0.0.1 on a random port; there is no option to bind
anything else. The token is created here, kept in memory and in `run/viewer.token`, and is
handed only to the browser through the URL fragment, which is never sent to the server or
printed. The store is opened in the thread that serves, because SQLite connections stay in
the thread that made them.
"""

import contextlib
import socket
import socketserver
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import TextIO
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from chamberlain.app.core import App
from chamberlain.app.wiring import build_app
from chamberlain.ports.clock import Clock
from chamberlain.surface.viewer.app import ViewerApp
from chamberlain.writes.confirm import create_token
from chamberlain.writes.models import WritePolicy

BIND_ADDRESS = "127.0.0.1"
REQUEST_TIMEOUT_SECONDS = 10
TOKEN_FILE = "viewer.token"

AppFactory = Callable[[], App]
Opener = Callable[[str], object]


class _Server(WSGIServer):
    """Binds without the reverse-DNS lookup `HTTPServer.server_bind` does (slow on Windows)."""

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)
        self.setup_environ()


class _Handler(WSGIRequestHandler):
    timeout = REQUEST_TIMEOUT_SECONDS

    def log_message(self, format: str, *args: object) -> None:
        """No access log: request lines can carry handles, and nothing needs them."""

    def address_string(self) -> str:
        return self.client_address[0]


class Viewer:
    """A bound viewer, not yet serving."""

    def __init__(
        self,
        server: WSGIServer,
        data_root: Path,
        token: str,
        open_app: AppFactory,
        policy: WritePolicy | None,
    ) -> None:
        self._server = server
        self._data_root = data_root
        self._token = token
        self._open_app = open_app
        self._policy = policy
        self.port = int(server.server_address[1])
        self.origin = f"http://{BIND_ADDRESS}:{self.port}"

    @property
    def url(self) -> str:
        """The address to show. It carries no token."""
        return self.origin + "/"

    @property
    def url_with_token(self) -> str:
        """For the browser only: the token travels in the fragment, which is never sent."""
        return f"{self.origin}/#token={self._token}"

    def serve_forever(self) -> None:
        """Serve until `shutdown`. Opens the store in this thread and closes it afterwards."""
        app = self._open_app()
        try:
            self._server.set_app(
                ViewerApp(
                    lambda: app.context,
                    token=self._token,
                    origin=self.origin,
                    policy=self._policy,
                )
            )
            self._server.serve_forever(poll_interval=0.2)
        finally:
            app.close()

    def shutdown(self) -> None:
        self._server.shutdown()

    def close(self) -> None:
        """Release the port and retire the token."""
        self._server.server_close()
        with contextlib.suppress(OSError):
            (self._data_root / "run" / TOKEN_FILE).unlink()


def _bind(port: int) -> WSGIServer:
    server = make_server(BIND_ADDRESS, port, _blank, server_class=_Server, handler_class=_Handler)
    if server.address_family != socket.AF_INET or server.server_address[0] != BIND_ADDRESS:
        server.server_close()
        raise OSError("The viewer must listen on 127.0.0.1 only.")
    return server


def _blank(environ: object, start_response: Callable[..., object]) -> list[bytes]:
    start_response("503 Service Unavailable", [("Content-Length", "0")])
    return []


def launch(
    data_root: Path,
    *,
    port: int = 0,
    open_browser: bool = True,
    opener: Opener | None = None,
    out: TextIO | None = None,
    clock: Clock | None = None,
    policy: WritePolicy | None = None,
    app_factory: AppFactory | None = None,
) -> Viewer:
    """Bind, create the token and open the page; the caller then calls `serve_forever`."""
    server = _bind(port)
    token = create_token(data_root)

    def open_app() -> App:
        return build_app(data_root, clock=clock, policy=policy)

    viewer = Viewer(server, data_root, token, app_factory or open_app, policy)
    if out is not None:
        out.write(f"Viewer: {viewer.url}\n")
    show = opener or webbrowser.open
    opened = open_browser and bool(show(viewer.url_with_token))
    if out is not None:
        if opened:
            out.write("It has been opened in your browser. Press Ctrl+C here to stop it.\n")
        else:
            out.write(
                "Open that address yourself. The page asks for the viewer token: paste "
                f"the contents of {data_root / 'run' / TOKEN_FILE}. Press Ctrl+C to stop.\n"
            )
    return viewer
