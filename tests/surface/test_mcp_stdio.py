"""MCP over stdio, end to end: list the tools and call one (docs/contracts/02-tools.md, T-15)."""

import io
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from chamberlain.app.context import SourceRouter
from chamberlain.app.wiring import build_app
from chamberlain.connections.demo import demo_document
from chamberlain.connectors.fake import FakeProvider
from chamberlain.ports.clock import ManualClock
from chamberlain.schema.registry import TOOLS
from chamberlain.surface import mcp_server

NEWLINE = chr(10)
NAMES = [spec.name.value for spec in TOOLS]


def _messages() -> str:
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "doctor", "arguments": {"check": "schema"}},
        },
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "nope"}},
    ]
    return "".join(json.dumps(line) + "\n" for line in lines)


def _check(answers: list[dict[str, Any]]) -> None:
    assert [a["id"] for a in answers] == [1, 2, 3, 4]
    assert [t["name"] for t in answers[1]["result"]["tools"]] == NAMES
    called = answers[2]["result"]
    assert called["isError"] is False
    assert called["structuredContent"]["ok"] is True
    unknown = answers[3]["result"]
    assert unknown["isError"] is True
    assert unknown["structuredContent"]["error"]["code"]


def _structured(answer: dict[str, Any]) -> dict[str, Any]:
    assert answer["result"]["isError"] is False
    reply: dict[str, Any] = answer["result"]["structuredContent"]
    assert reply["ok"] is True
    return reply


def test_the_server_over_a_real_stdio_process_on_a_demo_profile(data_root: Path) -> None:
    profile = data_root / "config" / "connections.yaml"
    profile.parent.mkdir(parents=True)
    profile.write_text(demo_document(), encoding="utf-8")
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
        session = call("start", {})["data"]["session"]
        found = call("find", {"session": session, "scope": "mail", "query": "invoice"})
        assert found["coverage"]["demo"] is True
        hit = found["data"]["hits"][0]
        assert "[DEMO]" in hit["content"]["text"]
        opened = call("open", {"session": session, "handle": hit["handle"], "part": "body"})
        assert "Synthetic demo data" in opened["data"]["content"]["text"]
        unknown = ask("tools/call", {"name": "nope"})
        assert unknown["result"]["isError"] is True
    finally:
        stdin.close()
        server.wait(timeout=30)
        stdout.close()
    assert server.returncode == 0


def test_the_server_loop_over_the_fake_provider(
    data_root: Path, clock: ManualClock, fake: FakeProvider
) -> None:
    sources = SourceRouter(
        mail={"mail:test": fake.mail},
        calendar={"calendar:test": fake.calendar},
        contacts={"contacts:test": fake.contacts},
    )
    app = build_app(data_root, clock=clock, sources=sources)
    out = io.StringIO()
    try:
        mcp_server.serve(io.StringIO(_messages()), out, app.call)
    finally:
        app.close()
    _check([json.loads(line) for line in out.getvalue().splitlines()])
