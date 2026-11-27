"""One viewer per data root, reused or started detached, and opened when a session starts."""

import io
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
