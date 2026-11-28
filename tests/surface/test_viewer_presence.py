"""One viewer per data root, reused or started detached, and opened when a session starts."""

import io
import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from chamberlain.app.context import Context
from chamberlain.app.wiring import LIVE_TEST_AGENT, tick_steps
from chamberlain.surface.cli import main
from chamberlain.surface.viewer import presence


def running_viewer(root: Path, port: int = 51234, token: str = "tok-123") -> None:
    (root / "run").mkdir(parents=True, exist_ok=True)
    (root / "run" / "viewer.token").write_text(token, encoding="utf-8")
    presence.record(root, port)


def test_a_running_viewer_is_found_with_its_tokened_address(tmp_path: Path) -> None:
    assert presence.running_url(tmp_path, answers=lambda port: True) is None, "nothing recorded"
    running_viewer(tmp_path)
    url = presence.running_url(tmp_path, answers=lambda port: port == 51234)
    assert url == "http://127.0.0.1:51234/#token=tok-123"
    assert presence.running_url(tmp_path, answers=lambda port: False) is None, "port is dead"
    presence.forget(tmp_path)
    assert presence.running_url(tmp_path, answers=lambda port: True) is None


def test_open_reuses_a_running_viewer_or_starts_one_detached(tmp_path: Path) -> None:
    opened: list[str] = []
    spawned: list[Sequence[str]] = []
    started = presence.open_viewer(
        tmp_path, opener=opened.append, spawn=spawned.append, answers=lambda port: True
    )
    assert started == "started"
    assert opened == []
    [args] = spawned
    assert list(args[1:]) == [
        "-m",
        "chamberlain.surface.cli",
        "--data",
        str(tmp_path),
        "viewer",
        "--browser",
    ]

    running_viewer(tmp_path)
    reused = presence.open_viewer(
        tmp_path, opener=opened.append, spawn=spawned.append, answers=lambda port: True
    )
    assert reused == "reused"
    assert opened == ["http://127.0.0.1:51234/#token=tok-123"]
    assert len(spawned) == 1, "a running viewer is never started twice"


def test_a_session_start_opens_the_viewer_except_for_the_live_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Path] = []
    monkeypatch.setattr(presence, "open_viewer", lambda root: calls.append(root) or "reused")

    class Ctx:
        data_root = tmp_path

    ctx = Ctx()
    assert tick_steps().on_start == (), "off unless the profile turns it on"
    [step] = tick_steps(open_viewer=True).on_start
    assert step(ctx, "codex") == []  # type: ignore[arg-type]
    assert step(ctx, LIVE_TEST_AGENT) == []  # type: ignore[arg-type]
    assert calls == [tmp_path], "the live test never opens browser tabs"


def test_a_session_start_survives_a_viewer_that_cannot_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(root: Path) -> str:
        raise OSError("no browser")

    monkeypatch.setattr(presence, "open_viewer", broken)

    class Ctx:
        data_root = tmp_path

    [step] = tick_steps(open_viewer=True).on_start
    assert step(Ctx(), "codex") == []  # type: ignore[arg-type]


def test_the_viewer_command_reuses_a_running_viewer_and_prints_no_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    running_viewer(root)
    monkeypatch.setattr(presence, "_answers", lambda port: True)
    opened: list[str] = []
    monkeypatch.setattr(presence, "installed_browser", lambda: None)
    monkeypatch.setattr("webbrowser.open", opened.append)
    monkeypatch.delenv("CLAUDECODE", raising=False)
    out = io.StringIO()
    assert main(["--data", str(root), "viewer"], out=out) == 0
    assert opened == ["http://127.0.0.1:51234/#token=tok-123"]
    assert "already running" in out.getvalue()
    assert "tok-123" not in out.getvalue()


_ = Context  # the step's real argument type


def test_a_held_reply_opens_the_running_viewer_on_its_grant(tmp_path: Path) -> None:
    opened: list[str] = []
    running_viewer(tmp_path)
    presence.open_viewer(
        tmp_path,
        opener=opened.append,
        spawn=lambda a: None,
        answers=lambda p: True,
        grant="grt_ab12",
    )
    assert opened == ["http://127.0.0.1:51234/#token=tok-123&grant=grt_ab12"]


def test_show_held_skips_the_live_test_and_is_off_unless_the_profile_opens_the_viewer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[Path, str | None]] = []

    def fake_open(root: Path, *, grant: str | None = None) -> str:
        calls.append((root, grant))
        return "reused"

    monkeypatch.setattr(presence, "open_viewer", fake_open)

    class Ctx:
        data_root = tmp_path

    assert tick_steps().show_held is None
    show = tick_steps(open_viewer=True).show_held
    assert show is not None
    assert show(Ctx(), "codex", "grt_1") is True  # type: ignore[arg-type]
    assert show(Ctx(), LIVE_TEST_AGENT, "grt_2") is False  # type: ignore[arg-type]
    assert calls == [(tmp_path, "grt_1")]


# --- one app window, reused ---------------------------------------------------------------

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
WIN_ENV = {"ProgramFiles(x86)": r"C:\Program Files (x86)", "ProgramFiles": r"C:\Program Files"}


def windows_with(*present: str) -> list[str] | None:
    def exists(path: str) -> bool:
        return path.replace("/", "\\") in present

    return presence.find_app_browser(platform="win32", env=WIN_ENV, exists=exists)


def test_windows_prefers_chrome_then_edge_in_app_mode_and_else_nothing(tmp_path: Path) -> None:
    assert windows_with(EDGE, CHROME) == [CHROME]
    assert windows_with(EDGE) == [EDGE]
    assert windows_with() is None
    command = presence.app_command([CHROME], "http://127.0.0.1:1/#token=t", tmp_path)
    assert command == [
        CHROME,
        "--app=http://127.0.0.1:1/#token=t",
        f"--user-data-dir={tmp_path / 'run' / 'viewer-browser'}",
        "--no-first-run",
        "--no-default-browser-check",
    ]


def test_mac_and_linux_pick_a_chromium_browser() -> None:
    def mac(path: str) -> bool:
        return path == "/Applications/Google Chrome.app"

    assert presence.find_app_browser(platform="darwin", env={}, exists=mac) == [
        "open", "-na", "Google Chrome", "--args"
    ]  # fmt: skip
    assert presence.find_app_browser(platform="darwin", env={}, exists=lambda p: False) is None
    found = presence.find_app_browser(
        platform="linux", env={}, which=lambda n: "/usr/bin/" + n if n == "google-chrome" else None
    )
    assert found == ["/usr/bin/google-chrome"]


def test_the_window_opener_launches_the_app_or_falls_back_to_the_browser(tmp_path: Path) -> None:
    launched: list[Sequence[str]] = []
    fallback: list[str] = []
    app = presence.window_opener(tmp_path, spawn=launched.append, find=lambda: [EDGE])
    app("http://127.0.0.1:1/#token=t")
    assert launched[0][0] == EDGE
    assert launched[0][1] == "--app=http://127.0.0.1:1/#token=t"
    plain = presence.window_opener(
        tmp_path, spawn=launched.append, find=lambda: None, fallback=fallback.append
    )
    plain("http://x/")
    assert fallback == ["http://x/"]
    assert len(launched) == 1


def test_no_new_window_while_the_page_is_alive_and_one_when_it_is_not(tmp_path: Path) -> None:
    running_viewer(tmp_path)
    opened: list[str] = []
    clock = [1000.0]

    def open_it(grant: str | None = None) -> str:
        return presence.open_viewer(
            tmp_path,
            opener=opened.append,
            spawn=lambda a: None,
            answers=lambda p: True,
            grant=grant,
            now=lambda: clock[0],
        )

    assert open_it() == "reused", "no page seen yet: a window opens"
    assert len(opened) == 1
    presence.mark_page_seen(tmp_path, now=lambda: clock[0])
    clock[0] += 44
    assert open_it() == "already open"
    assert len(opened) == 1
    clock[0] += 2
    assert open_it() == "reused", "the page went quiet: open again"
    assert len(opened) == 2


def test_a_held_grant_is_queued_for_the_live_page_and_delivered_once(tmp_path: Path) -> None:
    running_viewer(tmp_path)
    opened: list[str] = []
    presence.mark_page_seen(tmp_path, now=lambda: 1000.0)
    done = presence.open_viewer(
        tmp_path,
        opener=opened.append,
        answers=lambda p: True,
        grant="grt_ab12",
        now=lambda: 1010.0,
    )
    assert done == "already open"
    assert opened == []
    assert presence.heartbeat(tmp_path, now=lambda: 1015.0) == {"grant": "grt_ab12"}
    assert presence.heartbeat(tmp_path, now=lambda: 1030.0) == {"grant": None}, "delivered once"
    presence.queue_grant(tmp_path, "grt_ab12", now=lambda: 1000.0)
    assert presence.take_grant(tmp_path, now=lambda: 5000.0) is None, "a stale request is dropped"
    presence.queue_grant(tmp_path, "../evil", now=lambda: 1000.0)
    assert presence.take_grant(tmp_path, now=lambda: 1001.0) is None, "only a grant handle"


def test_the_page_seen_time_is_in_a_file_and_a_new_viewer_clears_it(tmp_path: Path) -> None:
    presence.mark_page_seen(tmp_path, now=lambda: 77.0)
    seen = json.loads((tmp_path / "run" / "viewer-page.json").read_text("utf-8"))
    assert seen == {"seen_at": 77.0}
    presence.record(tmp_path, 4000)
    assert not presence.page_alive(tmp_path, now=lambda: 78.0)


def test_the_token_is_never_in_what_open_viewer_returns(tmp_path: Path) -> None:
    running_viewer(tmp_path, token="secret-tok")
    spoken = presence.open_viewer(
        tmp_path, opener=lambda u: None, answers=lambda p: True, grant="grt_1"
    )
    presence.mark_page_seen(tmp_path)
    live = presence.open_viewer(tmp_path, opener=lambda u: None, answers=lambda p: True)
    assert "secret-tok" not in spoken + live + json.dumps(presence.heartbeat(tmp_path))
