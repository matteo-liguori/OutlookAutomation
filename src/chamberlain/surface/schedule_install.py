"""`chamberlain schedule install|uninstall|prompts`: the operating system tick and the agent text.

The tick runs `chamberlain.surface.cli --data ROOT tick --quiet` every `tick.every_minutes`
(profile, default 60) while the person is logged on, plus once `presync_minutes` before each
scheduled pass, so the agent starts on freshly synced mail: a hidden Windows Task Scheduler task
(overlapping runs ignored) or a macOS LaunchAgent. Nothing changes without `--apply`; whatever
is replaced is first saved under `<data root>/backups/`. The runner and every path are injected,
so tests touch no real system.
"""

import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final, Protocol
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

from chamberlain.connections import load_connections
from chamberlain.dataroot import checkout_root
from chamberlain.ports.clock import SystemClock
from chamberlain.schema.errors import ChamberlainError, ErrorCode

TASK_NAME: Final = "Chamberlain tick"
LAUNCHD_LABEL: Final = "uk.chamberlain.tick"
DEFAULT_EVERY_MINUTES: Final = 60
_CLOCK: Final = SystemClock()
MAX_EVERY_MINUTES: Final = 1440
_DAY_NAMES: Final = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_XML_DAYS: Final = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


@dataclass(frozen=True)
class PrePass:
    """One extra tick before a pass, in this machine's local time."""

    name: str
    hour: int
    minute: int
    weekdays: tuple[int, ...]
    """ISO weekdays (1 Monday .. 7 Sunday) on this machine's clock."""

    @property
    def clock(self) -> str:
        return f"{self.hour:02d}:{self.minute:02d}"


def pre_pass_times(root: Path, now: datetime) -> list[PrePass]:
    """The machine-local time `presync_minutes` before each enabled pass (none without a
    readable profile)."""
    try:
        profile = load_connections(root)
    except (ChamberlainError, OSError):
        return []
    found: list[PrePass] = []
    for schedule in profile.schedules:
        if not schedule.enabled or schedule.presync_minutes <= 0:
            continue
        zone = ZoneInfo(schedule.zone or profile.timezone)
        hour, minute = (int(part) for part in str(schedule.at).split(":")[:2])
        today = now.astimezone(zone).date()
        due = datetime(today.year, today.month, today.day, hour, minute, tzinfo=zone)
        local = (due - timedelta(minutes=schedule.presync_minutes)).astimezone()
        shift = (local.date() - today).days
        days = tuple(sorted({(day - 1 + shift) % 7 + 1 for day in schedule.weekdays}))
        found.append(PrePass(schedule.name, local.hour, local.minute, days))
    return found


def profile_minutes(root: Path) -> int:
    """`tick.every_minutes` from the profile, or the default without one."""
    try:
        return load_connections(root).tick.every_minutes
    except (ChamberlainError, OSError):
        return DEFAULT_EVERY_MINUTES


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


def _calendar_trigger(item: PrePass, start: datetime) -> str:
    days = "".join(f"<{_XML_DAYS[d - 1]} />" for d in item.weekdays)
    return f"""    <CalendarTrigger>
      <StartBoundary>{start.date().isoformat()}T{item.clock}:00</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByWeek>
        <DaysOfWeek>{days}</DaysOfWeek>
        <WeeksInterval>1</WeeksInterval>
      </ScheduleByWeek>
    </CalendarTrigger>
"""


def task_xml(
    python: Path,
    root: Path,
    every_minutes: int,
    *,
    start: datetime,
    pre_pass: Sequence[PrePass] = (),
) -> str:
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
{"".join(_calendar_trigger(item, start) for item in pre_pass)}  </Triggers>
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


def _calendar_dicts(pre_pass: Sequence[PrePass]) -> str:
    entries: list[str] = []
    for item in pre_pass:
        days = item.weekdays if len(item.weekdays) < 7 else (None,)
        for day in days:
            weekday = "" if day is None else f"<key>Weekday</key><integer>{day}</integer>"
            entries.append(
                f"    <dict><key>Hour</key><integer>{item.hour}</integer>"
                f"<key>Minute</key><integer>{item.minute}</integer>{weekday}</dict>"
            )
    if not entries:
        return ""
    body = "\n".join(entries)
    return f"  <key>StartCalendarInterval</key>\n  <array>\n{body}\n  </array>\n"


def launchd_plist(
    python: Path, root: Path, every_minutes: int, pre_pass: Sequence[PrePass] = ()
) -> str:
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
{_calendar_dicts(pre_pass)}  <key>RunAtLoad</key><true/>
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
    target: Target,
    *,
    every_minutes: int | None = None,
    apply: bool,
    runner: Runner = system_runner,
) -> list[str]:
    """Show the plan, or (with `apply`) register the tick and check that it is there.

    Without `every_minutes` the profile's `tick.every_minutes` is used.
    """
    if every_minutes is None:
        every_minutes = profile_minutes(target.root)
    _check_minutes(every_minutes)
    pre_pass = pre_pass_times(target.root, target.now())
    before = (
        "  plus once before each pass: " + ", ".join(f"{p.clock} ({p.name})" for p in pre_pass)
        if pre_pass
        else "  (no scheduled passes in the profile, so no pre-pass runs)"
    )
    command = f"{target.python} {' '.join(_quote(a) for a in tick_arguments(target.root))}"
    if target.system == "win32":
        existing = runner(["schtasks", "/Query", "/TN", TASK_NAME, "/XML"])
        lines = [
            f'{"Registering" if apply else "Would register"} the hidden task "{TASK_NAME}": '
            f"every {every_minutes} minutes while you are logged on, one run at a time.",
            before,
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
            task_xml(
                target.python, target.root, every_minutes, start=target.now(), pre_pass=pre_pass
            ),
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
            before,
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
            launchd_plist(target.python, target.root, every_minutes, pre_pass),
            encoding="utf-8",
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
        "(cron or a systemd timer), and once before each pass. It prints nothing; exit status "
        "10 means the agent should wake.",
        before,
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
                    f"Do the scheduled {schedule.name} pass ({schedule.kind}) with the chamberlain"
                    " tools.",
                    'Call `start` with purpose "scheduled" and scheduled_day set to today\'s date'
                    f" in {zone} (YYYY-MM-DD). Then follow `next` until the pass is done, and keep"
                    " the report under the run when `report.due` arrives. Stop when done.",
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
