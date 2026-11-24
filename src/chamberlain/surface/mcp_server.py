"""MCP server stub: lists the tools from the schema over stdio JSON-RPC.

Contract: docs/contracts/02-tools.md (T-15, T-16). No business logic here.

`main` serves the configured app (`chamberlain.connections.open_app`).

This is a deliberately small, dependency-free loop (newline-delimited
JSON-RPC 2.0: `initialize`, `ping`, `tools/list`, `tools/call`). Whether to
replace it with the official MCP SDK is a wave-2 decision; the tool
definitions below are what either would serve.
"""

import argparse
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO

from chamberlain import __version__
from chamberlain.connections import open_app
from chamberlain.schema.base import Model
from chamberlain.schema.envelope import Reply, error_reply
from chamberlain.schema.errors import ChamberlainError
from chamberlain.schema.registry import STANDING_PROMPT, TOOLS

DATA_ENV = "CHAMBERLAIN_DATA"
PROTOCOL_VERSION = "2025-06-18"
ToolCaller = Callable[[str, Mapping[str, Any]], Reply[Model]]

_METHOD_NOT_FOUND = -32601
_PARSE_ERROR = -32700


def list_tools() -> list[dict[str, Any]]:
    """One MCP tool definition per registry entry (T-15)."""
    return [
        {
            "name": spec.name.value,
            "description": spec.description,
            "inputSchema": spec.input_schema(),
        }
        for spec in TOOLS
    ]


def handle(message: Mapping[str, Any], call_tool: ToolCaller | None) -> dict[str, Any] | None:
    """Answer one JSON-RPC message. Notifications get no answer."""
    if "id" not in message:
        return None
    method = message.get("method")
    result: dict[str, Any]
    if method == "initialize":
        result = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "chamberlain", "version": __version__},
            "instructions": STANDING_PROMPT,
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": list_tools()}
    elif method == "tools/call" and call_tool is not None:
        params: Mapping[str, Any] = message.get("params") or {}
        reply = call_tool(str(params.get("name", "")), params.get("arguments") or {})
        envelope = reply.model_dump(mode="json", exclude_none=True)
        result = {
            "content": [{"type": "text", "text": json.dumps(envelope, ensure_ascii=False)}],
            "structuredContent": envelope,
            "isError": not reply.ok,
        }
    else:
        error = {"code": _METHOD_NOT_FOUND, "message": "Method not found"}
        return {"jsonrpc": "2.0", "id": message["id"], "error": error}
    return {"jsonrpc": "2.0", "id": message["id"], "result": result}


def serve(stdin: TextIO, stdout: TextIO, call_tool: ToolCaller | None = None) -> None:
    """Read messages line by line until the input ends."""
    for line in stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except ValueError:
            error = {"code": _PARSE_ERROR, "message": "Parse error"}
            answer: dict[str, Any] | None = {"jsonrpc": "2.0", "id": None, "error": error}
        else:
            answer = handle(message, call_tool)
        if answer is not None:
            stdout.write(json.dumps(answer, ensure_ascii=False) + "\n")
            stdout.flush()


def configured_caller(data_root: Path) -> ToolCaller:
    """Call tools on the app that `config/connections.yaml` describes, one app per call.

    A broken profile is a `config.invalid` reply to the call, never a crashed server.
    """

    def call_tool(name: str, arguments: Mapping[str, Any]) -> Reply[Model]:
        try:
            app = open_app(data_root)
        except ChamberlainError as error:
            return error_reply(error)
        try:
            return app.call(name, arguments)
        finally:
            app.close()

    return call_tool


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="chamberlain-mcp", description="Chamberlain MCP server.")
    parser.add_argument("--data", help=f"data root (default: ${DATA_ENV})")
    args = parser.parse_args(argv)
    data = args.data or os.environ.get(DATA_ENV)
    if not data:
        sys.stderr.write(f"No data root: set {DATA_ENV} or pass --data.\n")
        return 2
    serve(sys.stdin, sys.stdout, configured_caller(Path(data)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
