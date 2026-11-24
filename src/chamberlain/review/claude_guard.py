"""The Claude file guard: deny rules so Claude Code's file tools cannot read or edit the data root.

Contract: docs/contracts/06-memory.md (M-40), docs/review.md. An accident guard, not a security
boundary: it stops Claude Code's own Read, Grep, Glob, Edit and Write tools. It does not stop a
shell command, and Codex has nothing like it. Run it as
`python -m chamberlain.review.claude_guard add|remove|check --data <data root>`.

The rules go in the user's `settings.json` by default (they then hold wherever a scheduled run
starts), or in the checkout's `.claude/settings.local.json` with `--where project`. Never in the
project's `settings.json`: that file is committed, and it would carry a private path into Git.
Other keys and rules in the file are kept. Adding twice changes nothing.
"""

# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false
# pyright: reportUnknownVariableType=false
import argparse
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any

from chamberlain.review.homes import agent_homes
from chamberlain.review.scan import checkout_root
from chamberlain.schema.errors import ChamberlainError, ErrorCode

RECORD = Path("config") / "claude-guard.json"
"""Where, inside the data root, the guard notes which settings file it changed."""


@dataclass(frozen=True)
class GuardState:
    present: bool
    where: str
    """`user`, `project`, or `none`."""


def pattern(data_root: Path) -> str:
    """The data root as Claude Code's absolute-path pattern (`//c/Users/me/data/**`)."""
    resolved = data_root.resolve()
    text = str(resolved)
    if os.name == "nt" or (len(text) > 1 and text[1] == ":"):
        win = PureWindowsPath(text)
        drive = win.drive.rstrip(":").lower()
        rest = "/".join(win.parts[1:])
        return f"//{drive}/{rest}/**"
    return f"/{text}/**"


def rules_for(data_root: Path) -> list[str]:
    """Read also stops Grep, Glob, Edit and Write; Edit covers notebooks."""
    shape = pattern(data_root)
    return [f"Read({shape})", f"Edit({shape})"]


def _settings_path(where: str, project_dir: Path | None) -> Path:
    if where == "user":
        return agent_homes().claude / "settings.json"
    base = project_dir or checkout_root()
    if base is None:
        raise ChamberlainError(
            ErrorCode.CONFIG_INVALID,
            "There is no project folder to write to.",
            fix="Pass --project-dir, or use --where user.",
        )
    return base / ".claude" / "settings.local.json"


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        loaded: object = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as error:
        raise ChamberlainError(
            ErrorCode.CONFIG_INVALID,
            f"{path.name} is not valid JSON, so it was left alone.",
            fix="Repair the file, then run this again.",
        ) from error
    if not isinstance(loaded, dict):
        raise ChamberlainError(ErrorCode.CONFIG_INVALID, f"{path.name} is not a JSON object.")
    return loaded


def _deny(settings: dict[str, Any], *, create: bool) -> list[str] | None:
    permissions = settings.get("permissions")
    if not isinstance(permissions, dict):
        if not create:
            return None
        permissions = {}
        settings["permissions"] = permissions
    deny = permissions.get("deny")
    if not isinstance(deny, list):
        if not create:
            return None
        deny = []
        permissions["deny"] = deny
    return deny


def _write(path: Path, settings: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8", newline="\n")
    os.replace(temp, path)


def _record_path(data_root: Path) -> Path:
    return data_root / RECORD


def _recorded(data_root: Path) -> tuple[Path, str] | None:
    try:
        raw: object = json.loads(_record_path(data_root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(raw, dict) and isinstance(raw.get("settings"), str):
        return Path(raw["settings"]), str(raw.get("where", "user"))
    return None


def add(data_root: Path, *, where: str = "user", project_dir: Path | None = None) -> Path:
    """Add the deny rules (once) and note where they went. Returns the settings file."""
    path = _settings_path(where, project_dir)
    settings = _load(path)
    deny = _deny(settings, create=True)
    assert deny is not None
    for rule in rules_for(data_root):
        if rule not in deny:
            deny.append(rule)
    _write(path, settings)
    record = _record_path(data_root)
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(
        json.dumps({"settings": str(path), "where": where}, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return path


def remove(data_root: Path) -> bool:
    """Take out exactly the rules `add` put in. Returns True when something was removed."""
    recorded = _recorded(data_root)
    if recorded is None:
        return False
    path, _ = recorded
    changed = False
    if path.exists():
        settings = _load(path)
        deny = _deny(settings, create=False)
        mine = rules_for(data_root)
        if deny is not None and any(rule in deny for rule in mine):
            deny[:] = [rule for rule in deny if rule not in mine]
            _write(path, settings)
            changed = True
    _record_path(data_root).unlink(missing_ok=True)
    return changed


def check(data_root: Path) -> GuardState:
    """Whether the file recorded by `add` still holds every rule. Reads nothing else."""
    recorded = _recorded(data_root)
    if recorded is None:
        return GuardState(False, "none")
    path, where = recorded
    try:
        deny = _deny(_load(path), create=False) if path.exists() else None
    except ChamberlainError:
        return GuardState(False, where)
    present = deny is not None and all(rule in deny for rule in rules_for(data_root))
    return GuardState(present, where)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m chamberlain.review.claude_guard", description=(__doc__ or "").split("\n")[0]
    )
    parser.add_argument("action", choices=["add", "remove", "check"])
    parser.add_argument("--data", help="the data root (default: $CHAMBERLAIN_DATA)")
    parser.add_argument("--where", choices=["user", "project"], default="user")
    parser.add_argument("--project-dir", help="with --where project: the project folder")
    args = parser.parse_args(argv)
    data = args.data or os.environ.get("CHAMBERLAIN_DATA")
    if not data:
        print("Give the data root with --data or CHAMBERLAIN_DATA.", file=sys.stderr)
        return 2
    root = Path(data)
    try:
        if args.action == "add":
            path = add(
                root,
                where=args.where,
                project_dir=Path(args.project_dir) if args.project_dir else None,
            )
            print(f"Added the rules to {path}.")
        elif args.action == "remove":
            print("Removed the rules." if remove(root) else "No rules of ours were found.")
        else:
            state = check(root)
            print("The guard is in place." if state.present else "The guard is not in place.")
            return 0 if state.present else 1
    except ChamberlainError as error:
        print(error.message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
