"""The real server and `chamberlain viewer`: loopback only, the token goes to the browser
alone, and a request over a socket meets the same gate (05-grants.md G-17, G-18).
"""

import http.client
import io
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from chamberlain.surface import cli
from chamberlain.surface.viewer import server
from chamberlain.surface.viewer.server import Viewer, launch


@contextmanager
def serving(root: Path, opened: list[str]) -> Iterator[tuple[Viewer, io.StringIO]]:
    out = io.StringIO()
    viewer = launch(root, opener=lambda url: opened.append(url) or True, out=out)
    thread = threading.Thread(target=viewer.serve_forever)
    thread.start()
    try:
        yield viewer, out
    finally:
        viewer.shutdown()
        thread.join(10)
        viewer.close()


def request(
    viewer: Viewer,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
) -> tuple[int, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", viewer.port, timeout=10)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def test_g17_the_server_binds_loopback_only_and_serves_the_gate(tmp_path: Path) -> None:
    opened: list[str] = []
    with serving(tmp_path / "data", opened) as (viewer, out):
        assert viewer._server.server_address[0] == "127.0.0.1"
        assert viewer.origin == f"http://127.0.0.1:{viewer.port}"
        token = (tmp_path / "data" / "run" / "viewer.token").read_text(encoding="utf-8")
        host = {"Host": f"127.0.0.1:{viewer.port}"}
        status, page = request(viewer, "GET", "/")
        assert status == 200
        assert b"Chamberlain" in page
        assert request(viewer, "GET", "/api/overview")[0] == 403
        assert (
            request(viewer, "GET", "/api/overview", headers={"X-Chamberlain-Token": "no"})[0] == 403
        )
        good = {**host, "X-Chamberlain-Token": token}
        status, body = request(viewer, "GET", "/api/overview", headers=good)
        assert status == 200
        assert json.loads(body)["needs_you"]["grants_to_confirm"] == 0
        post = {**good, "Content-Type": "application/json"}
        assert request(viewer, "POST", "/api/notes", headers=post, body=b'{"text": "hi"}')[0] == 403
        with_origin = {**post, "Origin": viewer.origin}
        sent = request(viewer, "POST", "/api/notes", headers=with_origin, body=b'{"text": "hi"}')
        assert sent[0] == 200
        wrong_host = {**good, "Host": "evil.example"}
        assert request(viewer, "GET", "/api/overview", headers=wrong_host)[0] == 403
    assert not (tmp_path / "data" / "run" / "viewer.token").exists()
    assert token not in out.getvalue()
    assert viewer.url_with_token.endswith("#token=" + token)
    assert opened == [viewer.url_with_token]
    assert "#" not in viewer.url
    assert token not in viewer.url


def test_g18_the_page_asks_for_the_token_when_the_browser_could_not_be_opened(
    tmp_path: Path,
) -> None:
    out = io.StringIO()
    viewer = launch(tmp_path / "data", opener=lambda url: False, out=out)
    try:
        text = out.getvalue()
        assert "Open that address yourself" in text
        assert "viewer.token" in text
        token = (tmp_path / "data" / "run" / "viewer.token").read_text(encoding="utf-8")
        assert token not in text
    finally:
        viewer.close()


def test_g17_each_launch_has_its_own_random_token_and_port(tmp_path: Path) -> None:
    first = launch(tmp_path / "one", open_browser=False)
    second = launch(tmp_path / "two", open_browser=False)
    try:
        tokens = [
            (tmp_path / name / "run" / "viewer.token").read_text(encoding="utf-8")
            for name in ("one", "two")
        ]
        assert tokens[0] != tokens[1]
        assert len(tokens[0]) >= 40
        assert first.port != second.port
    finally:
        first.close()
        second.close()


def test_g17_the_viewer_cannot_be_told_to_listen_elsewhere() -> None:
    assert server.BIND_ADDRESS == "127.0.0.1"
    options = {a.dest for a in cli._parser()._actions}
    assert "host" not in options
    viewer_parser = cli._parser()._subparsers._group_actions[0].choices["viewer"]  # type: ignore[union-attr]
    assert {a.dest for a in viewer_parser._actions} == {"help", "port", "no_open"}


def test_the_viewer_command_prints_no_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[str] = []
    served: list[Viewer] = []
    monkeypatch.setattr(server.webbrowser, "open", lambda url: opened.append(url) or True)
    monkeypatch.setattr(Viewer, "serve_forever", lambda self: served.append(self))
    out = io.StringIO()
    code = cli.main(["--data", str(tmp_path / "data"), "viewer"], out)
    assert code == 0
    assert len(served) == 1
    assert "Viewer: http://127.0.0.1:" in out.getvalue()
    assert "token=" not in out.getvalue()
    assert not (tmp_path / "data" / "run" / "viewer.token").exists()  # retired on exit


def test_the_viewer_command_needs_a_data_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(cli.DATA_ENV, raising=False)
    out = io.StringIO()
    assert cli.main(["viewer"], out) == cli.EXIT_FAILED
    assert json.loads(out.getvalue())["error"]["code"] == "config.invalid"


def test_tick_prints_the_passes_a_wake_adapter_should_start(tmp_path: Path) -> None:
    out = io.StringIO()
    cli.main(["--data", str(tmp_path / "data"), "tick"], out)
    assert json.loads(out.getvalue()) == {"ran": True, "wake": False, "due": []}


def test_the_viewer_runs_on_the_configured_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It gets the profile's policy and schedules through `open_app`, like every surface."""
    from chamberlain.connections import open_app

    opened = threading.Event()

    def spy(data_root: Path, **kwargs: object) -> object:
        app = open_app(data_root, **kwargs)  # type: ignore[arg-type]
        opened.set()
        return app

    monkeypatch.setattr(server, "open_app", spy)
    root = tmp_path / "data"
    (root / "config").mkdir(parents=True)
    (root / "config" / "connections.yaml").write_text(
        "version: 1\n"
        "timezone: Europe/London\n"
        "write_policy: {sends_per_hour: 3}\n"
        "schedules:\n"
        "  - {name: morning, kind: mail, at: '07:30', weekdays: [1, 2, 3]}\n",
        encoding="utf-8",
    )
    with serving(root, []) as (viewer, _):
        assert viewer._policy is not None
        assert viewer._policy.zone == "Europe/London"
        assert viewer._policy.sends_per_hour == 3
        assert opened.wait(10)
    app = open_app(root)
    try:
        rows = app.context.db.execute("SELECT name, at_local, zone, weekdays FROM schedules")
        assert [tuple(row) for row in rows] == [("morning", "07:30", "Europe/London", "1,2,3")]
    finally:
        app.close()


def test_a_malformed_profile_stops_the_viewer_with_an_error_not_a_traceback(
    tmp_path: Path,
) -> None:
    root = tmp_path / "data"
    (root / "config").mkdir(parents=True)
    (root / "config" / "connections.yaml").write_text("version: 2\n", encoding="utf-8")
    out = io.StringIO()
    code = cli.main(["--data", str(root), "viewer", "--no-open"], out)
    assert code == cli.EXIT_FAILED
    assert "config.invalid" in out.getvalue()
