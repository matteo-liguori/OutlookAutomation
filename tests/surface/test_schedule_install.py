"""`schedule install|uninstall|prompts` against a fake runner: nothing touches the system."""

import io
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from chamberlain.schema.errors import ChamberlainError
from chamberlain.surface.cli import main
from chamberlain.surface.schedule_install import (
    TASK_NAME,
    Completed,
    Target,
    automation_prompts,
    install,
    uninstall,
)

NOW = datetime(2026, 1, 5, 17, 0, tzinfo=UTC)


class FakeRunner:
    def __init__(self, *, existing: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.exists = existing
        self.xml_seen = ""

    def __call__(self, args: Sequence[str]) -> Completed:
        self.calls.append(list(args))
        if args[:2] == ["schtasks", "/Create"]:
            self.xml_seen = Path(args[args.index("/XML") + 1]).read_text(encoding="utf-16")
            self.exists = True
        if args[:2] == ["schtasks", "/Delete"]:
            self.exists = False
        if args[:2] == ["schtasks", "/Query"]:
            return Completed(0 if self.exists else 1, "<old-task/>")
        if args[:2] == ["launchctl", "bootstrap"]:
            self.exists = True
        if args[:2] == ["launchctl", "print"]:
            return Completed(0 if self.exists else 1)
        return Completed(0)


def target(tmp_path: Path, system: str) -> Target:
    return Target(
        system=system,
        python=Path("C:/venv/Scripts/pythonw.exe"),
        root=tmp_path / "data",
        work_dir=tmp_path / "data" / "run",
        plist_path=tmp_path / "LaunchAgents" / "uk.chamberlain.tick.plist",
        now=lambda: NOW,
        uid=501,
    )


def test_windows_dry_run_shows_the_plan_and_changes_nothing(tmp_path: Path) -> None:
    runner = FakeRunner()
    lines = install(target(tmp_path, "win32"), every_minutes=5, apply=False, runner=runner)
    assert any("Would register" in line and TASK_NAME in line for line in lines)
    assert "--apply" in lines[-1]
    assert all(call[:2] == ["schtasks", "/Query"] for call in runner.calls)
    assert not (tmp_path / "data").exists()


def test_windows_apply_registers_a_hidden_non_overlapping_task_and_verifies(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()
    lines = install(target(tmp_path, "win32"), every_minutes=5, apply=True, runner=runner)
    assert lines[-1].startswith("Verified")
    xml = runner.xml_seen
    assert "<Hidden>true</Hidden>" in xml
    assert "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>" in xml
    assert "<LogonType>InteractiveToken</LogonType>" in xml
    assert "<Interval>PT5M</Interval>" in xml
    assert "pythonw.exe" in xml
    assert "-m chamberlain.surface.cli --data" in xml
    assert "tick --quiet" in xml
    assert not (tmp_path / "data" / "run" / "tick-task.xml").exists()


def test_windows_apply_saves_the_task_it_replaces(tmp_path: Path) -> None:
    runner = FakeRunner(existing=True)
    install(target(tmp_path, "win32"), every_minutes=10, apply=True, runner=runner)
    saved = list((tmp_path / "data" / "backups").iterdir())
    assert [p.read_text(encoding="utf-8") for p in saved] == ["<old-task/>"]


def test_the_interval_is_checked(tmp_path: Path) -> None:
    with pytest.raises(ChamberlainError):
        install(target(tmp_path, "win32"), every_minutes=0, apply=False, runner=FakeRunner())


def test_windows_uninstall_deletes_only_with_apply(tmp_path: Path) -> None:
    runner = FakeRunner(existing=True)
    assert "Would delete" in uninstall(target(tmp_path, "win32"), apply=False, runner=runner)[0]
    assert runner.exists
    assert "Deleted" in uninstall(target(tmp_path, "win32"), apply=True, runner=runner)[0]
    assert not runner.exists
    assert "No task" in uninstall(target(tmp_path, "win32"), apply=True, runner=runner)[0]


def test_mac_writes_a_launch_agent_and_loads_it(tmp_path: Path) -> None:
    runner = FakeRunner()
    t = target(tmp_path, "darwin")
    dry = install(t, every_minutes=5, apply=False, runner=runner)
    assert not t.plist_path.exists()
    assert runner.calls == []
    assert "--apply" in dry[-1]
    install(t, every_minutes=5, apply=True, runner=runner)
    plist = t.plist_path.read_text(encoding="utf-8")
    assert "<integer>300</integer>" in plist
    assert "<string>--quiet</string>" in plist
    assert ["launchctl", "bootstrap", "gui/501", str(t.plist_path)] in runner.calls
    install(t, every_minutes=5, apply=True, runner=runner)  # again: the old file is saved
    assert len(list((tmp_path / "data" / "backups").iterdir())) == 1
    assert "Unloaded" in uninstall(t, apply=True, runner=runner)[0]
    assert not t.plist_path.exists()


PROFILE = """version: 1
timezone: Europe/London
sources: {}
schedules:
  - {name: morning, kind: mail, at: "09:00"}
  - {name: weekday-check, kind: calendar, at: "14:00", weekdays: [1, 2, 3, 4, 5]}
  - {name: later, kind: mail, at: "20:00", enabled: false}
"""


def test_prompts_list_each_enabled_schedule_with_its_folder_and_start_call(
    data_root: Path,
) -> None:
    (data_root / "config").mkdir(parents=True)
    (data_root / "config" / "connections.yaml").write_text(PROFILE, encoding="utf-8")
    text = "\n".join(automation_prompts(data_root, checkout=Path("C:/dev/chamberlain")))
    assert "Name: Chamberlain morning" in text
    assert "Time: daily at 09:00 (Europe/London)" in text
    assert "Time: on Mon, Tue, Wed, Thu, Fri at 14:00 (Europe/London)" in text
    assert "Working folder: " in text
    assert 'purpose "scheduled"' in text
    assert "report.due" in text
    assert "Chamberlain later" not in text


def test_a_quiet_tick_prints_nothing_and_keeps_the_exit_code(data_root: Path) -> None:
    (data_root / "config").mkdir(parents=True)
    (data_root / "config" / "connections.yaml").write_text(
        "version: 1\nsources: {}\n", encoding="utf-8"
    )
    out = io.StringIO()
    status = main(["--data", str(data_root), "tick", "--quiet"], out)
    assert out.getvalue() == ""
    assert status == 0


TICK_PROFILE = """version: 1
timezone: UTC
tick:
  every_minutes: 60
schedules:
- {name: morning, kind: mail, at: '09:00', presync_minutes: 5}
- {name: evening, kind: mail, at: '19:00', presync_minutes: 10, weekdays: [1, 2, 3, 4, 5]}
- {name: quiet, kind: mail, at: '12:00', presync_minutes: 0}
"""


def with_profile(tmp_path: Path, system: str) -> Target:
    t = target(tmp_path, system)
    (t.root / "config").mkdir(parents=True)
    (t.root / "config" / "connections.yaml").write_text(TICK_PROFILE, encoding="utf-8")
    return t


def local_clock(hour: int, minute: int) -> str:
    moment = datetime(2026, 1, 5, hour, minute, tzinfo=UTC).astimezone()
    return f"{moment.hour:02d}:{moment.minute:02d}"


def test_the_profile_sets_an_hourly_tick_and_one_run_before_each_pass(tmp_path: Path) -> None:
    runner = FakeRunner()
    lines = install(with_profile(tmp_path, "win32"), apply=True, runner=runner)
    assert "every 60 minutes" in lines[0]
    before = local_clock(8, 55)
    assert f"{before} (morning)" in lines[1]
    assert f"{local_clock(18, 50)} (evening)" in lines[1]
    assert "quiet" not in lines[1], "a pass without a pre-pass sync gets no extra run"
    xml = runner.xml_seen
    assert "<Interval>PT60M</Interval>" in xml
    assert xml.count("<CalendarTrigger>") == 2
    assert f"T{before}:00</StartBoundary>" in xml
    assert "<Saturday />" in xml.split("<CalendarTrigger>")[1], "every day for the morning pass"
    assert "<Saturday />" not in xml.split("<CalendarTrigger>")[2], "weekdays only for evening"


def test_the_mac_agent_runs_hourly_and_at_each_pre_pass_time(tmp_path: Path) -> None:
    t = with_profile(tmp_path, "darwin")
    install(t, apply=True, runner=FakeRunner())
    plist = t.plist_path.read_text(encoding="utf-8")
    assert "<key>StartInterval</key><integer>3600</integer>" in plist
    assert "<key>StartCalendarInterval</key>" in plist
    assert plist.count("<key>Hour</key>") == 1 + 5, "one daily entry, five weekday entries"


def test_without_a_profile_the_tick_defaults_to_hourly(tmp_path: Path) -> None:
    lines = install(target(tmp_path, "win32"), apply=False, runner=FakeRunner())
    assert "every 60 minutes" in lines[0]
    assert "no scheduled passes" in lines[1]
