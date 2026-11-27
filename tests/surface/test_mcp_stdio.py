"""MCP over stdio, end to end: list the tools and call one (docs/contracts/02-tools.md, T-15)."""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from chamberlain.schema.registry import TOOLS

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
