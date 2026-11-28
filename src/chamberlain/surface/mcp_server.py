"""MCP server stub: lists the tools from the schema over stdio JSON-RPC.

Contract: docs/contracts/02-tools.md (T-15, T-16). No business logic here.

`main` serves the configured app (`chamberlain.connections.open_app`).

This is a deliberately small, dependency-free loop (newline-delimited
JSON-RPC 2.0: `initialize`, `ping`, `tools/list`, `tools/call`). The official
MCP SDK was considered and not adopted; see the "MCP server" note in
docs/contracts/02-tools.md.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from io import TextIOWrapper
from pathlib import Path
from typing import Any, BinaryIO, TextIO

import chamberlain
from chamberlain import __version__
from chamberlain.dataroot import refuse_inside_checkout
from chamberlain.schema.envelope import error_reply
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.schema.registry import STANDING_PROMPT, TOOLS
from chamberlain.surface.runner import DATA_ENV, ToolCaller, configured_caller

PROTOCOL_VERSION = "2025-06-18"


_METHOD_NOT_FOUND = -32601
_PARSE_ERROR = -32700

# Self-reload (docs/agents.md, "Staying current"). The old process hands the pending request
# and the saved `initialize` params to its replacement in this variable, never on a command line.
HANDOVER_ENV = "CHAMBERLAIN_MCP_HANDOVER"
CHECK_SECONDS_ENV = "CHAMBERLAIN_MCP_CHECK_SECONDS"  # for tests
CHECK_SECONDS = 10.0
_STALE_MESSAGE = (
    "Chamberlain was updated since this connection started; the MCP server could not reload itself."
)
_STALE_FIX = "Restart the chamberlain MCP server (or the Codex app)."


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


def code_fingerprint() -> str:
    """A cheap hash of the package: each `.py` path, size and mtime, plus the version."""
    root = Path(chamberlain.__file__).resolve().parent
    digest = hashlib.sha256(__version__.encode())
    for path in sorted(root.rglob("*.py")):
        try:
            info = path.stat()
        except OSError:
            continue
        name = path.relative_to(root).as_posix()
        digest.update(f"{name}|{info.st_size}|{info.st_mtime_ns}\n".encode())
    return digest.hexdigest()


class Reloader:
    """Notices that the code on disk is no longer the code this process runs.

    It is asked only between requests, and looks at the disk at most once per `interval`.
    """

    def __init__(
        self,
        fingerprint: Callable[[], str] = code_fingerprint,
        interval: float = CHECK_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fingerprint = fingerprint
        self._interval = interval
        self._clock = clock
        self._started = fingerprint()
        self._checked = clock()
        self._failed_on: str | None = None

    def changed(self) -> bool:
        """True when the code on disk differs from the code loaded at start."""
        now = self._clock()
        if now - self._checked < self._interval:
            return False
        self._checked = now
        return self._fingerprint() != self._started

    def may_retry(self) -> bool:
        """False when a handover already failed on exactly this code on disk."""
        return self._fingerprint() != self._failed_on

    def note_failure(self) -> None:
        self._failed_on = self._fingerprint()


Handover = Callable[[bytes, Mapping[str, Any]], int | None]


def _stale_answer(message: Mapping[str, Any]) -> dict[str, Any]:
    error = ChamberlainError(ErrorCode.INTERNAL_UNEXPECTED, _STALE_MESSAGE, fix=_STALE_FIX)
    envelope = error_reply(error).model_dump(mode="json", exclude_none=True)
    result = {
        "content": [{"type": "text", "text": json.dumps(envelope, ensure_ascii=False)}],
        "structuredContent": envelope,
        "isError": True,
    }
    return {"jsonrpc": "2.0", "id": message["id"], "result": result}


def serve(
    stdin: BinaryIO | TextIO,
    stdout: TextIO,
    call_tool: ToolCaller | None = None,
    *,
    reloader: Reloader | None = None,
    handover: Handover | None = None,
    first: str | None = None,
    initialize: Mapping[str, Any] | None = None,
) -> int | None:
    """Read messages line by line until the input ends.

    `first` is a request line to answer before reading `stdin` (a handover's pending call).
    Before a `tools/call`, never during one, a `reloader` that sees changed code asks
    `handover` to continue in a fresh process. It gets the raw request line and the saved
    `initialize` params. Its exit code ends this loop: the new process answered the call and
    owns the streams. If it returns None, the call gets a stale-code error instead.
    """
    saved: Mapping[str, Any] = initialize or {}
    pending = first
    while True:
        if pending is not None:
            line, pending = pending, None
        else:
            raw = stdin.readline()
            if not raw:
                return None
            line = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        if not line.strip():
            continue
        try:
            message: dict[str, Any] = json.loads(line)
        except ValueError:
            error = {"code": _PARSE_ERROR, "message": "Parse error"}
            answer: dict[str, Any] | None = {"jsonrpc": "2.0", "id": None, "error": error}
        else:
            method = message.get("method")
            if method == "initialize":
                saved = message.get("params") or {}
            if (
                reloader is not None
                and method == "tools/call"
                and "id" in message
                and reloader.changed()
            ):
                code = None
                if handover is not None and reloader.may_retry():
                    code = handover(line.encode("utf-8"), saved)
                if code is not None:
                    return code
                reloader.note_failure()
                answer = _stale_answer(message)
            else:
                answer = handle(message, call_tool)
        if answer is not None:
            stdout.write(json.dumps(answer, ensure_ascii=False) + "\n")
            stdout.flush()


def os_handover(data: str) -> Handover:
    """Hand the connection to a fresh process on this process's own stdin and stdout.

    The new process inherits both handles, is told the client has initialised, and answers
    the pending request itself. The caller gets its exit code, or None (nothing was handed
    over) when the new code cannot even be imported.
    """

    def hand_over(line: bytes, initialize: Mapping[str, Any]) -> int | None:
        try:
            probe = subprocess.run(
                [sys.executable, "-c", "import chamberlain.surface.mcp_server"],
                capture_output=True,
                timeout=60,
                check=False,
            )
            if probe.returncode != 0:
                return None
            state = {
                "line": line.decode("utf-8"),
                "initialize": dict(initialize),
                "protocol": PROTOCOL_VERSION,
            }
            child = subprocess.Popen(
                [sys.executable, "-m", "chamberlain.surface.mcp_server", "--data", data],
                stdin=sys.stdin.fileno(),
                stdout=sys.stdout.fileno(),
                env={**os.environ, HANDOVER_ENV: json.dumps(state)},
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return child.wait()

    return hand_over


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="chamberlain-mcp", description="Chamberlain MCP server.")
    parser.add_argument("--data", help=f"data root (default: ${DATA_ENV})")
    args = parser.parse_args(argv)
    data = args.data or os.environ.get(DATA_ENV)
    if not data:
        sys.stderr.write(f"No data root: set {DATA_ENV} or pass --data.\n")
        return 2
    try:
        refuse_inside_checkout(data)
    except ChamberlainError as error:
        sys.stderr.write(f"{error.message} {error.fix}\n")
        return 2
    # MCP uses UTF-8 even when Windows gives redirected stdio a legacy code page.
    for stream in (sys.stdin, sys.stdout):
        if isinstance(stream, TextIOWrapper):
            stream.reconfigure(encoding="utf-8")
    state = _take_handover()
    code = serve(
        _raw_stdin(),
        sys.stdout,
        configured_caller(Path(data)),
        reloader=Reloader(interval=float(os.environ.get(CHECK_SECONDS_ENV) or CHECK_SECONDS)),
        handover=os_handover(data),
        first=state["line"] if state else None,
        initialize=state.get("initialize") if state else None,
    )
    return code or 0


def _take_handover() -> dict[str, Any] | None:
    """The state an older process passed on, read once and removed from the environment."""
    text = os.environ.pop(HANDOVER_ENV, None)
    if not text:
        return None
    try:
        state: dict[str, Any] = json.loads(text)
    except ValueError:
        return None
    return state if isinstance(state.get("line"), str) else None


def _raw_stdin() -> BinaryIO:
    """Unbuffered binary stdin: a line read takes exactly one request and nothing after it.

    No read-ahead is what lets a replacement process inherit the stream without losing input.
    """
    return os.fdopen(sys.stdin.fileno(), "rb", buffering=0, closefd=False)  # type: ignore[return-value]


if __name__ == "__main__":
    raise SystemExit(main())
