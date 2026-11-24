"""Reading tool calls out of an agent's own session transcripts, and nothing else.

Contract: docs/contracts/06-memory.md (M-40). Claude Code writes `projects/<project>/*.jsonl`
(an assistant line carries `tool_use` blocks) and Codex `sessions/**/*.jsonl` (a `response_item`
line carries a `function_call`, `custom_tool_call` or `local_shell_call`). Only the tool name and,
for a shell tool, the command line are read, and the command line is reduced at once to a short
summary (`normalise.py`). Tool results, replies, mail text and file contents are never looked at.
"""

# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false
# pyright: reportUnknownVariableType=false
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from chamberlain.review import normalise

CLAUDE = "claude"
CODEX = "codex"

_SHELL_TOOLS = frozenset(
    {"bash", "powershell", "shell", "shell_command", "exec_command", "local_shell", "exec"}
)
_NO_COMMAND = frozenset({"apply_patch"})
_CMD_FIELD = re.compile(r'cmd:\s*"((?:[^"\\]|\\.)*)"')
_TOOL_NAME = re.compile(r"^[A-Za-z0-9_.:\-]{1,80}$")
_CHAMBERLAIN_TOOL = re.compile(r"^(mcp[_.:\-]+)?chamberlain([_.:\-]|$)", re.IGNORECASE)


@dataclass(frozen=True)
class Call:
    agent: str
    tool: str
    patterns: tuple[str, ...]
    """Command summaries; empty when only the tool name is recorded."""
    when: str | None


@dataclass
class Read:
    """What one pass over a region of a file found."""

    calls: list[Call] = field(default_factory=lambda: [])
    cwd: str | None = None
    chamberlain: bool = False
    """A Chamberlain tool was called in the region."""
    consumed: int = 0
    """Bytes used up: only whole lines count."""


def is_chamberlain_tool(name: str) -> bool:
    return bool(_CHAMBERLAIN_TOOL.match(name))


def tool_label(name: object) -> str:
    return name if isinstance(name, str) and _TOOL_NAME.match(name) else "(unknown)"


def stamp(value: object) -> str | None:
    """A transcript time as a UTC stamp, or None when it is not one."""
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _as_text(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        items = [str(item) for item in value]
        return normalise.from_argv(items)
    return None


def _commands(agent: str, name: str, payload: dict[str, Any]) -> list[str]:
    """The command lines a shell tool call carries (never for other tools)."""
    lowered = name.lower()
    if lowered in _NO_COMMAND:
        return []
    found: list[str] = []
    if agent == CLAUDE:
        if lowered in _SHELL_TOOLS:
            text = _as_text((payload.get("input") or {}).get("command"))
            if text:
                found.append(text)
        return found
    action = payload.get("action")
    if isinstance(action, dict):
        text = _as_text(action.get("command"))
        if text:
            found.append(text)
    raw = payload.get("arguments")
    if isinstance(raw, str):
        try:
            args = json.loads(raw)
        except ValueError:
            args = None
        if isinstance(args, dict):
            text = _as_text(args.get("command") or args.get("cmd"))
            if text:
                found.append(text)
    typed = payload.get("input")
    if isinstance(typed, str):
        for match in _CMD_FIELD.finditer(typed):
            try:
                found.append(json.loads(f'"{match.group(1)}"'))
            except ValueError:
                continue
    return found


def _call(agent: str, name: object, payload: dict[str, Any], when: str | None, read: Read) -> None:
    label = tool_label(name)
    if is_chamberlain_tool(label):
        read.chamberlain = True
        return
    patterns: list[str] = []
    for command in _commands(agent, label, payload):
        summary = normalise.summarise(command)
        if summary.chamberlain:
            read.chamberlain = True
        patterns.extend(summary.patterns)
    if label.lower() in _SHELL_TOOLS and not patterns:
        if read.chamberlain:
            return
        patterns = ["(other)"]
    if patterns:
        read.calls.extend(Call(agent, label, (p,), when) for p in patterns)
    else:
        read.calls.append(Call(agent, label, (), when))


def _claude_line(obj: dict[str, Any], read: Read, seen: set[str]) -> None:
    cwd = obj.get("cwd")
    if read.cwd is None and isinstance(cwd, str):
        read.cwd = cwd
    if obj.get("type") != "assistant":
        return
    message = obj.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        ident = block.get("id")
        if isinstance(ident, str):
            if ident in seen:
                continue
            seen.add(ident)
        _call(CLAUDE, block.get("name"), block, stamp(obj.get("timestamp")), read)


def _codex_line(obj: dict[str, Any], read: Read) -> None:
    payload = obj.get("payload")
    if not isinstance(payload, dict):
        return
    kind = obj.get("type")
    if kind in ("session_meta", "turn_context"):
        cwd = payload.get("cwd")
        if read.cwd is None and isinstance(cwd, str):
            read.cwd = cwd
        return
    if kind != "response_item":
        return
    ptype = payload.get("type")
    if ptype not in ("function_call", "custom_tool_call", "local_shell_call"):
        return
    name = "local_shell" if ptype == "local_shell_call" else payload.get("name")
    _call(CODEX, name, payload, stamp(obj.get("timestamp")), read)


_INTERESTING = {
    CLAUDE: (b'"tool_use"', b'"cwd"'),
    CODEX: (
        b"function_call",
        b"custom_tool_call",
        b"local_shell_call",
        b"session_meta",
        b"turn_context",
    ),
}


def read_region(agent: str, data: bytes, *, want_calls: bool = True) -> Read:
    """Parse the whole lines of `data`. A last line without its newline is left for later."""
    read = Read()
    end = data.rfind(b"\n")
    if end < 0:
        return read
    read.consumed = end + 1
    seen: set[str] = set()
    for line in data[:end].split(b"\n"):
        markers = (b'"tool_use"',) if agent == CLAUDE and read.cwd else _INTERESTING[agent]
        if not any(marker in line for marker in markers):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        if not want_calls:
            read.cwd = read.cwd or _cwd_only(agent, obj)
            if read.cwd:
                break
            continue
        if agent == CLAUDE:
            _claude_line(obj, read, seen)
        else:
            _codex_line(obj, read)
    return read


def _cwd_only(agent: str, obj: dict[str, Any]) -> str | None:
    probe = Read()
    if agent == CLAUDE:
        _claude_line(obj, probe, set())
    else:
        _codex_line(obj, probe)
    return probe.cwd
