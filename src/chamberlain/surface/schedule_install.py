"""`chamberlain schedule install|uninstall|prompts`: the operating system tick and the agent text.

The tick runs `chamberlain.surface.cli --data ROOT tick --quiet` every few minutes while the
person is logged on: a hidden Windows Task Scheduler task (overlapping runs ignored) or a macOS
LaunchAgent. Nothing changes without `--apply`; whatever is replaced is first saved under
`<data root>/backups/`. The runner and every path are injected, so tests touch no real system.
"""

import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final, Protocol
from xml.sax.saxutils import escape

from chamberlain.connections import load_connections
from chamberlain.dataroot import checkout_root
from chamberlain.ports.clock import SystemClock
from chamberlain.schema.errors import ChamberlainError, ErrorCode

TASK_NAME: Final = "Chamberlain tick"
LAUNCHD_LABEL: Final = "uk.chamberlain.tick"
DEFAULT_EVERY_MINUTES: Final = 5
_CLOCK: Final = SystemClock()
MAX_EVERY_MINUTES: Final = 60
_DAY_NAMES: Final = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


@dataclass(frozen=True)
class Completed:
    returncode: int
    stdout: str = ""


class Runner(Protocol):
    def __call__(self, args: Sequence[str]) -> Completed: ...


def system_runner(args: Sequence[str]) -> Completed:
    try:
        done = subprocess.run(list(args), capture_output=True, text=True, check=False)
    except OSError as error:
        return Completed(127, str(error))
    return Completed(done.returncode, done.stdout)


def tick_python(python: Path | None = None, *, system: str | None = None) -> Path:
    """The interpreter that runs the tick: `pythonw.exe` on Windows (no console), else python."""
    exe = python or Path(sys.executable)
    if (system or sys.platform) == "win32":
        windowless = exe.with_name("pythonw.exe")
        if windowless.is_file():
            return windowless
    return exe


def tick_arguments(root: Path) -> list[str]:
    return ["-m", "chamberlain.surface.cli", "--data", str(root), "tick", "--quiet"]


def _quote(argument: str) -> str:
    return f'"{argument}"' if (" " in argument or not argument) else argument


def task_xml(python: Path, root: Path, every_minutes: int, *, start: datetime) -> str:
    """A Task Scheduler 1.2 definition: hidden, logged-on only, one run at a time."""
    arguments = " ".join(_quote(a) for a in tick_arguments(root))
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Chamberlain tick for {escape(str(root))}</Description>
  </RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <StartBoundary>{start.astimezone().strftime("%Y-%m-%dT%H:%M:%S")}</StartBoundary>
      <Enabled>true</Enabled>
      <Repetition>
        <Interval>PT{every_minutes}M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Hidden>true</Hidden>
    <ExecutionTimeLimit>PT30M</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(str(python))}</Command>
      <Arguments>{escape(arguments)}</Arguments>
    </Exec>
  </Actions>
</Task>
"""


def launchd_plist(python: Path, root: Path, every_minutes: int) -> str:
    items = "\n".join(
        f"    <string>{escape(a)}</string>" for a in [str(python), *tick_arguments(root)]
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LAUNCHD_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
{items}
  </array>
  <key>StartInterval</key><integer>{every_minutes * 60}</integer>
  <key>RunAtLoad</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>StandardErrorPath</key><string>{escape(str(root / "logs" / "tick.err"))}</string>
</dict>
</plist>
"""


@dataclass(frozen=True)
class Target:
    """Where and how the tick is installed; every field is injectable."""

    system: str
    python: Path
    root: Path
    work_dir: Path
    """Where the task definition is written for `schtasks` (removed afterwards)."""
    plist_path: Path
    now: Callable[[], datetime] = _CLOCK.now
    uid: int = 0


def default_target(root: Path) -> Target:
    return Target(
        system=sys.platform,
        python=tick_python(),
        root=root,
        work_dir=root / "run",
        plist_path=Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist",
        uid=int(getattr(os, "getuid", lambda: 0)()),
    )


def _check_minutes(every: int) -> None:
    if not 1 <= every <= MAX_EVERY_MINUTES:
        raise ChamberlainError(
            ErrorCode.REQUEST_INVALID,
            f"--every-minutes is 1 to {MAX_EVERY_MINUTES}.",
            field="every_minutes",
        )


def _backup(root: Path, name: str, text: str, now: datetime) -> Path:
    folder = root / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}-{now.strftime('%Y%m%dT%H%M%S')}"
    path.write_text(text, encoding="utf-8")
    return path


def install(
    target: Target, *, every_minutes: int, apply: bool, runner: Runner = system_runner
) -> list[str]:
    """Show the plan, or (with `apply`) register the tick and check that it is there."""
    _check_minutes(every_minutes)
    command = f"{target.python} {' '.join(_quote(a) for a in tick_arguments(target.root))}"
    if target.system == "win32":
        existing = runner(["schtasks", "/Query", "/TN", TASK_NAME, "/XML"])
        lines = [
            f'{"Registering" if apply else "Would register"} the hidden task "{TASK_NAME}": '
            f"every {every_minutes} minutes while you are logged on, one run at a time.",
            f"  runs: {command}",
        ]
        if existing.returncode == 0:
            lines.append("  it replaces the task of that name that exists now (saved first).")
        if not apply:
            return [*lines, "Add --apply to do it."]
        if existing.returncode == 0:
            saved = _backup(target.root, "tick-task.xml", existing.stdout, target.now())
            lines.append(f"  saved the old task to {saved}")
        target.work_dir.mkdir(parents=True, exist_ok=True)
        definition = target.work_dir / "tick-task.xml"
        definition.write_text(
            task_xml(target.python, target.root, every_minutes, start=target.now()),
            encoding="utf-16",
        )
        try:
            made = runner(["schtasks", "/Create", "/TN", TASK_NAME, "/XML", str(definition), "/F"])
        finally:
            definition.unlink(missing_ok=True)
        if made.returncode != 0 or runner(["schtasks", "/Query", "/TN", TASK_NAME]).returncode:
            raise ChamberlainError(
                ErrorCode.CONFIG_INVALID,
                f'Task Scheduler did not accept the task "{TASK_NAME}".',
                detail=made.stdout[:300],
            )
        return [*lines, f'Verified: "{TASK_NAME}" is registered.']
    if target.system == "darwin":
        lines = [
            f"{'Writing' if apply else 'Would write'} the LaunchAgent {target.plist_path}: "
            f"every {every_minutes} minutes while you are logged in.",
            f"  runs: {command}",
        ]
        if not apply:
            return [*lines, "Add --apply to do it."]
        if target.plist_path.is_file():
            old = target.plist_path.read_text(encoding="utf-8")
            lines.append(
                f"  saved the old file to {_backup(target.root, 'tick.plist', old, target.now())}"
            )
        (target.root / "logs").mkdir(parents=True, exist_ok=True)
        target.plist_path.parent.mkdir(parents=True, exist_ok=True)
        target.plist_path.write_text(
            launchd_plist(target.python, target.root, every_minutes), encoding="utf-8"
        )
        domain = f"gui/{target.uid}"
        runner(["launchctl", "bootout", domain, str(target.plist_path)])  # absent is fine
        loaded = runner(["launchctl", "bootstrap", domain, str(target.plist_path)])
        if (
            loaded.returncode != 0
            or runner(["launchctl", "print", f"{domain}/{LAUNCHD_LABEL}"]).returncode
        ):
            raise ChamberlainError(
                ErrorCode.CONFIG_INVALID,
                "launchd did not load the LaunchAgent.",
                detail=loaded.stdout[:300],
            )
        return [*lines, f"Verified: {LAUNCHD_LABEL} is loaded."]
    return [
        f"Run `{command}` every {every_minutes} minutes from your scheduler "
        "(cron or a systemd timer). It prints nothing; exit status 10 means the agent should wake."
    ]


def uninstall(target: Target, *, apply: bool, runner: Runner = system_runner) -> list[str]:
    if target.system == "win32":
        if runner(["schtasks", "/Query", "/TN", TASK_NAME]).returncode != 0:
            return [f'No task "{TASK_NAME}" is registered.']
        if not apply:
            return [f'Would delete the task "{TASK_NAME}".', "Add --apply to do it."]
        runner(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])
        if runner(["schtasks", "/Query", "/TN", TASK_NAME]).returncode == 0:
            raise ChamberlainError(
                ErrorCode.CONFIG_INVALID, f'The task "{TASK_NAME}" is still there.'
            )
        return [f'Deleted the task "{TASK_NAME}".']
    if target.system == "darwin":
        if not target.plist_path.is_file():
            return [f"No LaunchAgent at {target.plist_path}."]
        if not apply:
            return [f"Would unload and delete {target.plist_path}.", "Add --apply to do it."]
        runner(["launchctl", "bootout", f"gui/{target.uid}", str(target.plist_path)])
        target.plist_path.unlink()
        return [f"Unloaded and deleted {target.plist_path}."]
    return ["Remove the tick from your scheduler."]


def automation_prompts(root: Path, *, checkout: Path | None = None) -> list[str]:
    """For each enabled schedule: the agent-app automation text (name, time, folder, prompt)."""
    profile = load_connections(root)
    folder = checkout or checkout_root() or Path.cwd()
    blocks: list[str] = []
    for schedule in profile.schedules:
        if not schedule.enabled:
            continue
        zone = schedule.zone or profile.timezone
        days = sorted(set(schedule.weekdays))
        repeat = "daily" if len(days) == 7 else "on " + ", ".join(_DAY_NAMES[d - 1] for d in days)
        blocks.append(
            "\n".join(
                [
                    f"Name: Chamberlain {schedule.name}",
                    f"Time: {repeat} at {schedule.at} ({zone})",
                    f"Working folder: {folder}",
                    "Prompt:",
                    f"Do the scheduled {schedule.name} pass ({schedule.kind}) with the chamberlain",
                    "tools.",
                    'Call `start` with purpose "scheduled" and scheduled_day set to today\'s date',
                    f"in {zone} (YYYY-MM-DD). Then follow `next` until the pass is done, and keep",
                    "the report under the run when `report.due` arrives. Stop when done.",
                ]
            )
        )
    if not blocks:
        return ["No enabled schedules in the profile (config/connections.yaml)."]
    intro = (
        "Create one automation per block below in your agent app. The agent needs the "
        "chamberlain MCP server (`chamberlain setup agent`)."
    )
    return [intro, *blocks]
