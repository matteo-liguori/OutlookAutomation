"""Archive v1 after the switch: one folder, a ROLLBACK.md and a scheduled deletion.

`<parent>/_archive/<date>-pre-v2-switch` receives the v1 checkout, the v1 data folder, the
importer's snapshot and anything named with `--also`. Folders in use (the agent's own working
folder, for one) are moved later by a one-time task at the next logon or 03:00. Deletion after N
days is a one-time task too (Windows); on Mac the command is printed. Movers, schedulers, the
clock and the in-use check are injected; nothing moves unless `apply` is true.
"""

import base64
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final, Protocol

from chamberlain.ports.clock import Clock, SystemClock

CLEANUP_TASK: Final = "Chamberlain archive cleanup"
MOVE_TASK: Final = "Chamberlain archive move"
DEFAULT_KEEP_DAYS: Final = 30


class Scheduler(Protocol):
    def delete_after(self, folder: Path, when: datetime) -> str:
        """Register a one-time deletion of `folder`; return what was registered or the command."""
        ...

    def move_later(self, moves: Sequence[tuple[Path, Path]], script: Path, now: datetime) -> str:
        """Register a one-time move of each (source, target) at next logon or 03:00."""
        ...


Mover = Callable[[Path, Path], None]


def _move(source: Path, target: Path) -> None:
    shutil.move(str(source), str(target))


def path_in_use(path: Path) -> bool:
    """The working folder of this process (or one inside the path) cannot be moved."""
    try:
        cwd = Path.cwd().resolve()
    except OSError:
        return False
    resolved = path.resolve()
    return cwd == resolved or resolved in cwd.parents


@dataclass
class ArchiveResult:
    applied: bool
    archive: Path
    deletes_on: str
    moved: list[str] = field(default_factory=lambda: list[str]())
    scheduled_moves: list[str] = field(default_factory=lambda: list[str]())
    skipped: list[str] = field(default_factory=lambda: list[str]())
    deletion: str = ""
    rollback: str = ""

    def document(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "archive": str(self.archive),
            "deletes_on": self.deletes_on,
            "moves" if not self.applied else "moved": self.moved,
            "scheduled_moves": self.scheduled_moves,
            "skipped": self.skipped,
            "deletion": self.deletion,
            "rollback": self.rollback,
        }


def archive_folder(parent: Path, now: datetime) -> Path:
    return parent / "_archive" / f"{now:%Y-%m-%d}-pre-v2-switch"


def archive_v1(
    *,
    old_root: Path,
    old_checkout: Path | None,
    import_backup: Path | None,
    also: Sequence[Path] = (),
    extra_copies: Sequence[Path] = (),
    keep_days: int = DEFAULT_KEEP_DAYS,
    apply: bool,
    scheduler: Scheduler,
    mover: Mover = _move,
    in_use: Callable[[Path], bool] = path_in_use,
    clock: Clock | None = None,
) -> ArchiveResult:
    """Move the v1 folders into one archive. Dry run unless `apply`; safe to repeat."""
    moment = (clock or SystemClock()).now().astimezone()
    anchor = (old_checkout or old_root).resolve().parent
    archive = archive_folder(anchor, moment)
    deletes = moment + timedelta(days=keep_days)
    result = ArchiveResult(apply, archive, f"{deletes:%Y-%m-%d}")
    items: list[tuple[Path, str]] = []
    if old_checkout is not None:
        items.append((old_checkout, "v1 checkout"))
    items.append((old_root, "v1 data folder"))
    if import_backup is not None:
        items.append((import_backup, "importer snapshot of v1"))
    items.extend((path, "named with --also") for path in also)
    now_moves: list[tuple[Path, Path, str]] = []
    later: list[tuple[Path, Path, str]] = []
    names: set[str] = set()
    for source, why in items:
        target_name = source.name
        if source == import_backup:
            target_name = f"{old_root.name}.v1-backup"
        while target_name in names:
            target_name += "-2"
        names.add(target_name)
        target = archive / target_name
        if not source.exists():
            result.skipped.append(f"{source}: not found (already moved?)")
        elif target.exists():
            result.skipped.append(f"{source}: {target} already exists")
        elif in_use(source):
            later.append((source, target, why))
        else:
            now_moves.append((source, target, why))
    result.moved = [f"{s} -> {t}" for s, t, _ in now_moves]
    result.scheduled_moves = [f"{s} -> {t}" for s, t, _ in later]
    result.rollback = str(archive / "ROLLBACK.md")
    if not apply:
        result.deletion = f"would delete {archive} on {result.deletes_on} (one-time scheduled task)"
        return result
    archive.mkdir(parents=True, exist_ok=True)
    pending = list(later)
    done: list[tuple[Path, Path, str]] = []
    result.moved = []
    for source, target, why in now_moves:
        try:
            mover(source, target)
        except OSError:
            pending.append((source, target, why))
            continue
        done.append((source, target, why))
        result.moved.append(f"{source} -> {target}")
    for extra in extra_copies:
        if extra.is_dir() and not (archive / extra.name).exists():
            shutil.copytree(extra, archive / extra.name)
    result.scheduled_moves = [f"{s} -> {t}" for s, t, _ in pending]
    if pending:
        script = archive / "finish-archive-move.cmd"
        result.scheduled_moves.append(
            scheduler.move_later([(s, t) for s, t, _ in pending], script, moment)
        )
    (archive / "ROLLBACK.md").write_text(
        rollback_text(archive, [*done, *pending], result.deletes_on, bool(pending)),
        encoding="utf-8",
        newline="\n",
    )
    result.deletion = scheduler.delete_after(archive, deletes)
    return result


def rollback_text(
    archive: Path,
    items: Sequence[tuple[Path, Path, str]],
    deletes_on: str,
    has_pending: bool,
) -> str:
    rows = "\n".join(f"| `{t.name}` | `{s}` ({why}) |" for s, t, why in items)
    pending = (
        "\nSome folders were in use and move later (next logon or 03:00) through the task "
        f'"{MOVE_TASK}". Until then they are still at their old place.\n'
        if has_pending
        else ""
    )
    return f"""# Rolling back to v1 (until {deletes_on})

Everything from before the switch to Chamberlain v2 is in this folder. It is deleted
automatically on **{deletes_on}** by the scheduled task "{CLEANUP_TASK}" (Windows). To keep it
longer, disable or delete that task (Mac: cancel the reminder you were given).
{pending}
## What is here

| Item | Was at |
| --- | --- |
{rows}

Also kept when they exist: `v1-retired\\` (v1's Thunderbird add-on file and native-host
registration, saved when they were retired) and `app-automation-backups\\` (the agent-app
automations before they were paused).

## Roll back to v1

1. Disable v2: turn off its scheduled tick (`chamberlain schedule install` made it; Task
   Scheduler or launchd) and pause its agent-app automations.
2. Close Thunderbird. Move each item above back to its old place.
3. Copy the v1 add-on file from `v1-retired` back to the Thunderbird profile's `extensions`
   folder and import `v1-native-host.reg` (double-click it; Mac: copy the saved manifest back).
   Start Thunderbird.
4. In the agent app, resume the v1 automations (set `status = "ACTIVE"` in the saved copies'
   place, or resume them in the app's Automations page).

v1's data was never changed by the migration, so it resumes from where it stopped. Anything v2
sent in between is in your Sent folders and in v2's audit log (`chamberlain doctor --check audit`).
"""


def _ps_encoded(script: str) -> str:
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


class WindowsScheduler:
    """One-time tasks through PowerShell's ScheduledTasks module."""

    def __init__(self, run: Callable[[list[str]], int] | None = None) -> None:
        self._run = run or _run

    def _register(self, name: str, command: str, triggers: str) -> None:
        action = (
            f"New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "
            f"{_quote('-NoProfile -WindowStyle Hidden -EncodedCommand ' + _ps_encoded(command))}"
        )
        script = (
            f"Register-ScheduledTask -TaskName {_quote(name)} -Force "
            f"-Action ({action}) -Trigger @({triggers}) "
            "-Settings (New-ScheduledTaskSettingsSet -StartWhenAvailable) | Out-Null"
        )
        code = self._run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script])
        if code != 0:
            raise OSError(f"The scheduled task {name!r} could not be registered.")

    def delete_after(self, folder: Path, when: datetime) -> str:
        command = (
            f"Remove-Item -LiteralPath {_quote(str(folder))} -Recurse -Force "
            f"-ErrorAction SilentlyContinue; "
            f"Unregister-ScheduledTask -TaskName {_quote(CLEANUP_TASK)} -Confirm:$false"
        )
        at = when.replace(hour=3, minute=0, second=0, microsecond=0, tzinfo=None)
        self._register(
            CLEANUP_TASK, command, f"New-ScheduledTaskTrigger -Once -At {_quote(at.isoformat())}"
        )
        return f'task "{CLEANUP_TASK}" deletes {folder} on {at:%Y-%m-%d} at 03:00'

    def move_later(self, moves: Sequence[tuple[Path, Path]], script: Path, now: datetime) -> str:
        lines = ["@echo off"]
        for source, target in moves:
            lines.append(f'if exist "{source}" move "{source}" "{target}"')
        lines.append(f'schtasks /Delete /TN "{MOVE_TASK}" /F')
        script.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8", newline="")
        command = f"& cmd.exe /c {_quote(str(script))}"
        tomorrow = now.replace(hour=3, minute=0, second=0, microsecond=0, tzinfo=None)
        if tomorrow <= now.replace(tzinfo=None):
            tomorrow += timedelta(days=1)
        self._register(
            MOVE_TASK,
            command,
            "(New-ScheduledTaskTrigger -AtLogOn), "
            f"(New-ScheduledTaskTrigger -Once -At {_quote(tomorrow.isoformat())})",
        )
        return f'task "{MOVE_TASK}" moves them at the next logon or 03:00 ({script})'


class ManualScheduler:
    """Mac and Linux: nothing is registered; the exact commands are returned to run or note."""

    def delete_after(self, folder: Path, when: datetime) -> str:
        return (
            f"Not scheduled on this platform. On {when:%Y-%m-%d} or later, delete it with: "
            f'rm -rf "{folder}" (set a calendar reminder).'
        )

    def move_later(self, moves: Sequence[tuple[Path, Path]], script: Path, now: datetime) -> str:
        commands = "; ".join(f'mv "{s}" "{t}"' for s, t in moves)
        return f"Not scheduled on this platform. After closing the agent session, run: {commands}"


def system_scheduler() -> Scheduler:
    return WindowsScheduler() if sys.platform == "win32" else ManualScheduler()


def _run(args: list[str]) -> int:
    return subprocess.run(args, capture_output=True, check=False).returncode


__all__ = [
    "ArchiveResult",
    "ManualScheduler",
    "Scheduler",
    "WindowsScheduler",
    "archive_folder",
    "archive_v1",
    "path_in_use",
    "system_scheduler",
]
