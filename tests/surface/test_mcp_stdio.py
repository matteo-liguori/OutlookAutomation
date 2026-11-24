"""MCP over stdio, end to end: list the tools and call one (docs/contracts/02-tools.md, T-15)."""

import io
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

from chamberlain.app.context import SourceRouter
from chamberlain.app.wiring import build_app
from chamberlain.connectors.fake import FakeProvider
from chamberlain.ports.clock import ManualClock
from chamberlain.schema.registry import TOOLS
from chamberlain.surface import mcp_server

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


def test_the_server_over_a_real_stdio_process(data_root: Path) -> None:
    profile = data_root / "config" / "connections.yaml"
    profile.parent.mkdir(parents=True)
    profile.write_text(yaml.safe_dump({"version": 1, "sources": {}}), encoding="utf-8")
    done = subprocess.run(
        [sys.executable, "-m", "chamberlain.surface.mcp_server", "--data", str(data_root)],
        input=_messages(),
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    _check([json.loads(line) for line in done.stdout.splitlines()])


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
