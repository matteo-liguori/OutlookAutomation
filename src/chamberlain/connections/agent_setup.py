"""`chamberlain setup agent codex|claude`: connect an agent app to the MCP server.

Codex: a `[mcp_servers.chamberlain]` block in `~/.codex/config.toml`. Claude Code: a project
`.mcp.json` (with `--project`) or the `claude mcp add` command to run. The server command is this
checkout's Python and `--data ROOT`. Nothing is written without `--apply`; the file is saved
first and only this block is replaced. After writing, the server is started and asked to
`initialize`, so a wrong path shows now rather than in the agent.
"""

import json
import re
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from chamberlain.ports.clock import SystemClock
from chamberlain.schema.errors import ChamberlainError, ErrorCode

SERVER_NAME: Final = "chamberlain"
MODULE: Final = "chamberlain.surface.mcp_server"
PROBE_SECONDS: Final = 20.0
_CLOCK: Final = SystemClock()

Probe = Callable[[Sequence[str]], tuple[str, str]]
"""Start the command, send `initialize`, return the server's (name, version); raise on failure."""


def server_command(root: Path, python: Path | None = None) -> list[str]:
    exe = (python or Path(sys.executable)).as_posix()
    return [exe, "-m", MODULE, "--data", root.as_posix()]


def toml_block(command: Sequence[str]) -> str:
    args = ", ".join(json.dumps(a) for a in command[1:])
    return f"[mcp_servers.{SERVER_NAME}]\ncommand = {json.dumps(command[0])}\nargs = [{args}]\n"


_HEADER = re.compile(r"^\s*\[\[?([^\]]+)\]\]?\s*(#.*)?$")


def replace_block(text: str, block: str) -> str:
    """`text` with the chamberlain table (and its sub-tables) replaced, or the block appended."""
    own = f"mcp_servers.{SERVER_NAME}"
    lines = text.splitlines(keepends=True)
    kept: list[str] = []
    skipping = False
    placed = False
    for line in lines:
        header = _HEADER.match(line)
        if header:
            name = header.group(1).strip()
            skipping = name == own or name.startswith(own + ".")
            if skipping and not placed:
                kept.append(block)
                placed = True
        if not skipping:
            kept.append(line)
    out = "".join(kept)
    if not placed:
        if out and not out.endswith("\n"):
            out += "\n"
        out += ("\n" if out else "") + block
    return out


def _backup(path: Path, now: datetime) -> Path:
    saved = path.with_name(f"{path.name}.bak-{now.strftime('%Y%m%dT%H%M%S')}")
    shutil.copy2(path, saved)
    return saved


def probe_server(command: Sequence[str]) -> tuple[str, str]:
    """Really start the server and read its name and version from `initialize`."""
    process = subprocess.Popen(
        list(command),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
    )
    stdin, stdout = process.stdin, process.stdout
    assert stdin is not None
    assert stdout is not None
    answer: list[str] = []
    try:
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "chamberlain-setup", "version": "1"},
            },
        }
        stdin.write(json.dumps(request) + "\n")
        stdin.flush()
        reader = threading.Thread(target=lambda: answer.append(stdout.readline()), daemon=True)
        reader.start()
        reader.join(PROBE_SECONDS)
    finally:
        process.kill()
        process.wait()
    return parse_initialize(answer[0] if answer else "")


def parse_initialize(line: str) -> tuple[str, str]:
    try:
        info: dict[str, Any] = json.loads(line)["result"]["serverInfo"]
        return str(info["name"]), str(info["version"])
    except (ValueError, KeyError, TypeError):
        raise ChamberlainError(
            ErrorCode.CONFIG_INVALID,
            "The server did not answer `initialize`.",
            fix="Run the command by hand to see its error.",
        ) from None


def codex_config_path() -> Path:
    return Path.home() / ".codex" / "config.toml"


def setup_codex(
    root: Path,
    *,
    apply: bool,
    config: Path | None = None,
    python: Path | None = None,
    probe: Probe = probe_server,
    now: Callable[[], datetime] = _CLOCK.now,
) -> list[str]:
    path = config or codex_config_path()
    command = server_command(root, python)
    block = toml_block(command)
    old = path.read_text(encoding="utf-8") if path.is_file() else ""
    new = replace_block(old, block)
    lines = [f"Codex config: {path}", block.rstrip()]
    if new == old:
        lines.append("Already set; nothing to change.")
    elif not apply:
        return [*lines, "Add --apply to write it (the file is saved first)."]
    else:
        if path.is_file():
            lines.append(f"Saved the old file as {_backup(path, now())}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new, encoding="utf-8")
        lines.append("Written. Restart the Codex app once so it loads the server.")
    if apply:
        lines.append(_verify(command, probe))
    return lines


def setup_claude(
    root: Path,
    *,
    apply: bool,
    project: Path | None = None,
    python: Path | None = None,
    probe: Probe = probe_server,
    now: Callable[[], datetime] = _CLOCK.now,
) -> list[str]:
    command = server_command(root, python)
    entry = {"command": command[0], "args": command[1:]}
    if project is None:
        quoted = " ".join(json.dumps(c) for c in command)
        lines = [
            "Run this once in a terminal (add `--scope user` for every project):",
            f"  claude mcp add {SERVER_NAME} -- {quoted}",
            "Or pass --project FOLDER to write FOLDER/.mcp.json.",
        ]
        if apply:
            lines.append(_verify(command, probe))
        return lines
    path = project / ".mcp.json"
    document: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            raise ChamberlainError(
                ErrorCode.CONFIG_INVALID, f"{path} is not valid JSON; it was left alone."
            ) from None
        if not isinstance(loaded, dict):
            raise ChamberlainError(ErrorCode.CONFIG_INVALID, f"{path} is not a JSON object.")
        document = loaded  # pyright: ignore[reportUnknownVariableType]
    servers: dict[str, Any] = dict(document.get("mcpServers") or {})
    lines = [f"Claude Code project file: {path}", json.dumps({SERVER_NAME: entry}, indent=2)]
    if servers.get(SERVER_NAME) == entry:
        lines.append("Already set; nothing to change.")
    elif not apply:
        return [*lines, "Add --apply to write it (the file is saved first)."]
    else:
        servers[SERVER_NAME] = entry
        document["mcpServers"] = servers
        if path.is_file():
            lines.append(f"Saved the old file as {_backup(path, now())}")
        project.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        lines.append("Written. Claude Code asks you to approve the project server on first use.")
    if apply:
        lines.append(_verify(command, probe))
    return lines


def _verify(command: Sequence[str], probe: Probe) -> str:
    name, version = probe(command)
    return f"Verified: the server answered as {name} {version}."
