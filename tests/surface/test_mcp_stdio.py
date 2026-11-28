"""MCP over stdio, end to end: list the tools and call one (docs/contracts/02-tools.md, T-15)."""

import io
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

import chamberlain
from chamberlain.schema.registry import TOOLS
from chamberlain.surface import mcp_server

NEWLINE = chr(10)
NAMES = [spec.name.value for spec in TOOLS]


def _structured(answer: dict[str, Any]) -> dict[str, Any]:
    assert answer["result"]["isError"] is False
    reply: dict[str, Any] = answer["result"]["structuredContent"]
    assert reply["ok"] is True
    return reply


def test_the_server_over_a_real_stdio_process_on_a_minimal_profile(data_root: Path) -> None:
    profile = data_root / "config" / "connections.yaml"
    profile.parent.mkdir(parents=True)
    profile.write_text("version: 1\nsources: {}\n", encoding="utf-8")
    server = subprocess.Popen(
        [sys.executable, "-m", "chamberlain.surface.mcp_server", "--data", str(data_root)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    stdin, stdout = server.stdin, server.stdout
    assert stdin is not None
    assert stdout is not None
    ids = iter(range(1, 100))

    def ask(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        number = next(ids)
        message = {"jsonrpc": "2.0", "id": number, "method": method, "params": params or {}}
        stdin.write(json.dumps(message) + NEWLINE)
        stdin.flush()
        answer: dict[str, Any] = json.loads(stdout.readline())
        assert answer["id"] == number
        return answer

    def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return _structured(ask("tools/call", {"name": name, "arguments": arguments}))

    try:
        ask("initialize")
        listed = ask("tools/list")
        assert [t["name"] for t in listed["result"]["tools"]] == NAMES
        call("doctor", {"check": "schema"})
        unknown = ask("tools/call", {"name": "nope"})
        assert unknown["result"]["isError"] is True
    finally:
        stdin.close()
        server.wait(timeout=30)
        stdout.close()
    assert server.returncode == 0


def test_unicode_stdio_with_a_legacy_windows_pipe_encoding(data_root: Path) -> None:
    messages = [
        {"jsonrpc": "2.0", "id": "mail-📨", "method": "initialize"},
        {"jsonrpc": "2.0", "id": 2, "method": "ping"},
    ]
    wire = "".join(json.dumps(message, ensure_ascii=False) + NEWLINE for message in messages)
    server = subprocess.run(
        [sys.executable, "-m", "chamberlain.surface.mcp_server", "--data", str(data_root)],
        input=wire,
        capture_output=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "cp1252"},
        timeout=30,
        check=True,
    )
    first, second = map(json.loads, server.stdout.splitlines())
    assert first["id"] == messages[0]["id"]
    assert second == {"jsonrpc": "2.0", "id": 2, "result": {}}


# --- self-reload after an update ---------------------------------------------------------------

PACKAGE = Path(chamberlain.__file__).resolve().parent


def test_the_fingerprint_changes_when_a_package_file_is_touched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "chamberlain"
    package.mkdir()
    (package / "__init__.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(chamberlain, "__file__", str(package / "__init__.py"))
    before = mcp_server.code_fingerprint()
    assert mcp_server.code_fingerprint() == before
    (package / "__init__.py").write_text("x = 22\n", encoding="utf-8")
    after = mcp_server.code_fingerprint()
    assert after != before
    (package / "new.py").write_text("", encoding="utf-8")
    assert mcp_server.code_fingerprint() != after


def test_the_reloader_checks_the_disk_at_most_once_per_interval() -> None:
    now = [0.0]
    prints = ["a"]
    reloader = mcp_server.Reloader(lambda: prints[0], interval=10, clock=lambda: now[0])
    prints[0] = "b"
    now[0] = 9
    assert reloader.changed() is False
    now[0] = 10
    assert reloader.changed() is True
    reloader.note_failure()
    assert reloader.may_retry() is False
    prints[0] = "c"
    assert reloader.may_retry() is True


def test_a_failed_handover_answers_with_the_stale_code_error_and_serves_on() -> None:
    prints = ["a"]
    reloader = mcp_server.Reloader(lambda: prints[0], interval=0)
    prints[0] = "b"
    call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "x"}}
    stdin = io.StringIO(json.dumps(call) + NEWLINE + json.dumps({**call, "id": 2}) + NEWLINE)
    stdout = io.StringIO()
    tried: list[bytes] = []

    def fail(line: bytes, initialize: Mapping[str, Any]) -> int | None:
        tried.append(line)
        return None

    mcp_server.serve(stdin, stdout, None, reloader=reloader, handover=fail)
    answers = [json.loads(text) for text in stdout.getvalue().splitlines()]
    assert [a["id"] for a in answers] == [1, 2]
    error = answers[0]["result"]["structuredContent"]["error"]
    assert answers[0]["result"]["isError"] is True
    assert "updated since this connection started" in error["message"]
    assert "Restart the chamberlain MCP server" in error["fix"]
    assert len(tried) == 1  # the same changed code is not retried


def _run_server(package_parent: Path, data_root: Path) -> subprocess.Popen[bytes]:
    env = {**os.environ, "PYTHONPATH": str(package_parent), mcp_server.CHECK_SECONDS_ENV: "0"}
    return subprocess.Popen(
        [sys.executable, "-m", "chamberlain.surface.mcp_server", "--data", str(data_root)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        env=env,
    )


def test_a_server_whose_code_changed_hands_over_in_place(data_root: Path, tmp_path: Path) -> None:
    profile = data_root / "config" / "connections.yaml"
    profile.parent.mkdir(parents=True)
    profile.write_text("version: 1\nsources: {}\n", encoding="utf-8")
    copy = tmp_path / "src"
    shutil.copytree(PACKAGE, copy / "chamberlain", ignore=shutil.ignore_patterns("__pycache__"))
    server = _run_server(copy, data_root)
    stdin, stdout = server.stdin, server.stdout
    assert stdin is not None
    assert stdout is not None
    ids = iter(range(1, 100))

    def ask(method: str, params: dict[str, Any] | None = None, batch: int = 1) -> dict[str, Any]:
        number = next(ids)
        message = {"jsonrpc": "2.0", "id": number, "method": method, "params": params or {}}
        stdin.write((json.dumps(message) + NEWLINE).encode())
        stdin.flush()
        answer: dict[str, Any] = json.loads(stdout.readline())
        assert answer["id"] == number
        return answer

    try:
        ask("initialize")
        stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        assert [t["name"] for t in ask("tools/list")["result"]["tools"]] == NAMES
        first = ask("tools/call", {"name": "doctor", "arguments": {"check": "schema"}})
        assert first["result"]["isError"] is False
        assert "_v" not in first["result"]
        # Update the checkout: the new code answers every tools/call with a marker.
        target = copy / "chamberlain" / "surface" / "mcp_server.py"
        text = target.read_text(encoding="utf-8")
        marked = text.replace('"isError": not reply.ok,', '"isError": not reply.ok, "_v": "new",')
        assert marked != text
        target.write_text(marked, encoding="utf-8")
        # Two requests written back to back: nothing may be lost to read-ahead.
        for number in (101, 102):
            message = {"jsonrpc": "2.0", "id": number, "method": "tools/call"}
            params = {"name": "doctor", "arguments": {"check": "schema"}}
            stdin.write((json.dumps({**message, "params": params}) + NEWLINE).encode())
        stdin.flush()
        answers = [json.loads(stdout.readline()) for _ in range(2)]
        assert [a["id"] for a in answers] == [101, 102]
        for answer in answers:
            assert answer["result"]["isError"] is False
            assert answer["result"]["_v"] == "new"
            assert answer["result"]["structuredContent"]["ok"] is True
    finally:
        stdin.close()
        server.wait(timeout=60)
        stdout.close()
    assert server.returncode == 0


def test_a_server_whose_new_code_cannot_load_answers_with_the_stale_code_error(
    data_root: Path, tmp_path: Path
) -> None:
    profile = data_root / "config" / "connections.yaml"
    profile.parent.mkdir(parents=True)
    profile.write_text("version: 1\nsources: {}\n", encoding="utf-8")
    copy = tmp_path / "src"
    shutil.copytree(PACKAGE, copy / "chamberlain", ignore=shutil.ignore_patterns("__pycache__"))
    server = _run_server(copy, data_root)
    stdin, stdout = server.stdin, server.stdout
    assert stdin is not None
    assert stdout is not None
    try:
        stdin.write(b'{"jsonrpc":"2.0","id":0,"method":"ping"}\n')
        stdin.flush()
        assert json.loads(stdout.readline())["id"] == 0  # started, fingerprint taken
        target = copy / "chamberlain" / "surface" / "mcp_server.py"
        target.write_text(target.read_text(encoding="utf-8") + "\nraise SystemExit(7)\n")
        message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "doctor"}}
        stdin.write((json.dumps(message) + NEWLINE).encode())
        stdin.flush()
        answer = json.loads(stdout.readline())
        assert answer["result"]["isError"] is True
        assert (
            "could not reload itself" in answer["result"]["structuredContent"]["error"]["message"]
        )
    finally:
        stdin.close()
        server.wait(timeout=60)
        stdout.close()
