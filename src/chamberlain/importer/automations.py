"""Agent-app automations that still run v1: find them, pause them (or say which to pause).

v1's passes ran as agent-app automations, not as operating-system tasks. Codex keeps each in
`~/.codex/automations/<id>/automation.toml`; pausing is one line, `status = "PAUSED"`, written
after a backup. Claude desktop scheduled tasks are read when their files are found. All paths are
injectable; nothing is written unless `apply` is true.
"""

import re
import shutil
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast

PAUSED = "PAUSED"
_V1_TEXT = re.compile(r"-m\s+monitor\b|monitor\.ps1", re.IGNORECASE)
_STATUS_LINE = re.compile(r'^(status\s*=\s*)"[^"\n]*"', re.MULTILINE)


@dataclass(frozen=True)
class Automation:
    app: str
    """`codex` or `claude`."""
    name: str
    status: str
    schedule: str
    path: str
    pausable: bool
    """True when this harness can pause it by editing its file."""


def guess_checkout(old_root: Path) -> Path | None:
    """v1's source checkout from its data folder: `<name>-data` sits next to `<name>`."""
    name = old_root.name
    if name.endswith("-data"):
        candidate = old_root.with_name(name[: -len("-data")])
        if (candidate / "monitor").is_dir():
            return candidate
    return None


def _norm(value: str) -> str:
    return value.replace("\\", "/").rstrip("/").casefold()


def _under(path: str, checkout: Path) -> bool:
    root = _norm(str(checkout))
    candidate = _norm(path)
    return candidate == root or candidate.startswith(root + "/")


def _codex(codex_home: Path, checkout: Path) -> list[Automation]:
    found: list[Automation] = []
    for toml in sorted((codex_home / "automations").glob("*/automation.toml")):
        try:
            data: dict[str, Any] = tomllib.loads(toml.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            continue
        cwds = cast(list[Any], data["cwds"]) if isinstance(data.get("cwds"), list) else []
        if not any(isinstance(c, str) and _under(c, checkout) for c in cwds):
            continue
        schedule = str(data.get("rrule") or data.get("schedule") or "")
        found.append(
            Automation(
                "codex",
                str(data.get("name") or toml.parent.name),
                str(data.get("status") or ""),
                schedule.replace("\n", " "),
                str(toml),
                True,
            )
        )
    return found


def _claude(claude_home: Path, checkout: Path) -> list[Automation]:
    """Claude scheduled tasks, when kept as `<home>/scheduled-tasks/<id>/SKILL.md`."""
    found: list[Automation] = []
    for task in sorted((claude_home / "scheduled-tasks").glob("*/SKILL.md")):
        try:
            text = task.read_text(encoding="utf-8")
        except OSError:
            continue
        if _norm(str(checkout)) in _norm(text) or _V1_TEXT.search(text):
            found.append(Automation("claude", task.parent.name, "unknown", "", str(task), False))
    return found


def detect_app_automations(
    checkout: Path | None, *, codex_home: Path, claude_home: Path
) -> dict[str, Any]:
    """Automations that run v1, with name, status and schedule; plus what could not be checked."""
    if checkout is None:
        return {
            "checked": False,
            "note": "The v1 checkout is unknown: pass --old-checkout PATH so agent-app "
            "automations that run it can be found.",
            "automations": [],
        }
    items = [*_codex(codex_home, checkout), *_claude(claude_home, checkout)]
    notes: list[str] = []
    if not (claude_home / "scheduled-tasks").is_dir():
        notes.append(
            "Claude scheduled tasks are not stored in files this harness can read; open the "
            "Claude app's Scheduled page and pause any task that runs v1 by hand."
        )
    if not (codex_home / "automations").is_dir():
        notes.append("No Codex automations folder was found.")
    return {
        "checked": True,
        "checkout": str(checkout),
        "automations": [asdict(item) for item in items],
        "notes": notes,
    }


def pause_app_automations(
    checkout: Path | None,
    *,
    codex_home: Path,
    claude_home: Path,
    backup_dir: Path,
    apply: bool,
) -> dict[str, Any]:
    """Pause every Codex automation that runs v1 (backup first); list the rest by name.

    Dry run unless `apply`. Idempotent: an already paused automation is left alone.
    """
    detected = detect_app_automations(checkout, codex_home=codex_home, claude_home=claude_home)
    paused: list[str] = []
    already: list[str] = []
    to_pause_by_hand: list[str] = []
    for row in detected["automations"]:
        if row["app"] != "codex":
            to_pause_by_hand.append(f"{row['app']}: {row['name']}")
            continue
        if row["status"].upper() == PAUSED:
            already.append(row["name"])
            continue
        paused.append(row["name"])
        if apply:
            _pause_codex(Path(row["path"]), backup_dir)
    return {
        "applied": apply,
        "would_pause" if not apply else "paused": paused,
        "already_paused": already,
        "pause_by_hand": to_pause_by_hand,
        "notes": detected.get("notes", []),
        "after": "Tell the Codex app to reload (restart it or reopen Automations) so it "
        "sees the paused status."
        if paused
        else "",
    }


def _pause_codex(toml: Path, backup_dir: Path) -> None:
    backup_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(toml, backup_dir / f"codex-{toml.parent.name}.automation.toml")
    text = toml.read_bytes().decode("utf-8")
    if _STATUS_LINE.search(text):
        changed = _STATUS_LINE.sub(f'\\1"{PAUSED}"', text, count=1)
    else:
        changed = f'status = "{PAUSED}"\n' + text
    pending = toml.with_name("automation.toml.tmp")
    pending.write_bytes(changed.encode("utf-8"))
    pending.replace(toml)
