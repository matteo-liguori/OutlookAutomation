"""The switch tools: app automations, v1 processes, add-on retirement, iCloud, archive.

Nothing here touches the real Task Scheduler, registry, ~/.codex, Thunderbird or processes:
every machine-facing part is a fake and every path is under tmp_path.
"""

import json
import os
from collections.abc import Sequence
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path

import pytest
import yaml
from old_fixture import MemorySecrets

from chamberlain.connections.load import load_connections
from chamberlain.connectors.caldav.setup import Discovered
from chamberlain.importer import migrate
from chamberlain.importer.archive import (
    CLEANUP_TASK,
    ManualScheduler,
    WindowsScheduler,
    archive_v1,
)
from chamberlain.importer.automations import (
    detect_app_automations,
    guess_checkout,
    pause_app_automations,
)
from chamberlain.importer.retire import (
    V1_ADDON_FILE,
    ManifestFileHost,
    RunningProcess,
    refuse_while_running,
    retire_v1_addon,
    v1_processes,
)
from chamberlain.ports.clock import ManualClock
from chamberlain.schema.errors import ChamberlainError
from chamberlain.surface import cli, migration


@pytest.fixture(autouse=True)
def isolate_legacy_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "empty-local"))


# --- item 1: agent-app automations --------------------------------------------------------


def _codex(home: Path, name: str, cwd: str, status: str = "ACTIVE") -> Path:
    folder = home / "automations" / name
    folder.mkdir(parents=True)
    path = folder / "automation.toml"
    path.write_text(
        f'version = 1\nid = "{name}"\nname = "Name of {name}"\nprompt = "x"\n'
        f'status = "{status}"\nrrule = "DTSTART;TZID=Europe/London:20261124T090000\\n'
        f'RRULE:FREQ=DAILY"\ncwds = [{json.dumps(cwd)}]\n',
        encoding="utf-8",
    )
    return path


def test_detects_and_pauses_only_codex_automations_that_run_the_v1_checkout(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "OutlookAutomation"
    checkout.mkdir()
    codex, claude = tmp_path / "codex", tmp_path / "claude"
    mine = _codex(codex, "mail-check", str(checkout))
    other = _codex(codex, "unrelated", str(tmp_path / "elsewhere"))
    done = _codex(codex, "already", str(checkout / "sub"), status="PAUSED")
    before_other = other.read_bytes()

    found = detect_app_automations(checkout, codex_home=codex, claude_home=claude)
    names = {row["name"]: row for row in found["automations"]}
    assert set(names) == {"Name of mail-check", "Name of already"}
    assert names["Name of mail-check"]["status"] == "ACTIVE"
    assert "FREQ=DAILY" in names["Name of mail-check"]["schedule"]
    assert any("Claude" in note for note in found["notes"])

    backups = tmp_path / "backups"
    plan = pause_app_automations(
        checkout, codex_home=codex, claude_home=claude, backup_dir=backups, apply=False
    )
    assert plan["would_pause"] == ["Name of mail-check"]
    assert 'status = "ACTIVE"' in mine.read_text(encoding="utf-8")
    assert not backups.exists()

    result = pause_app_automations(
        checkout, codex_home=codex, claude_home=claude, backup_dir=backups, apply=True
    )
    assert result["paused"] == ["Name of mail-check"]
    assert result["already_paused"] == ["Name of already"]
    assert 'status = "PAUSED"' in mine.read_text(encoding="utf-8")
    assert 'status = "ACTIVE"' in (backups / "codex-mail-check.automation.toml").read_text(
        encoding="utf-8"
    )
    assert other.read_bytes() == before_other
    assert 'status = "PAUSED"' in done.read_text(encoding="utf-8")
    again = pause_app_automations(
        checkout, codex_home=codex, claude_home=claude, backup_dir=backups, apply=True
    )
    assert again["paused"] == []


def test_claude_tasks_are_listed_by_hand_and_unknown_checkout_is_said() -> None:
    unknown = detect_app_automations(None, codex_home=Path("x"), claude_home=Path("y"))
    assert unknown["checked"] is False
    assert "--old-checkout" in unknown["note"]


def test_claude_scheduled_task_files_are_read_when_found(tmp_path: Path) -> None:
    checkout = tmp_path / "OutlookAutomation"
    checkout.mkdir()
    task = tmp_path / "claude" / "scheduled-tasks" / "morning" / "SKILL.md"
    task.parent.mkdir(parents=True)
    task.write_text(f"Run {checkout}\\monitor.ps1 briefing", encoding="utf-8")
    result = pause_app_automations(
        checkout,
        codex_home=tmp_path / "codex",
        claude_home=tmp_path / "claude",
        backup_dir=tmp_path / "b",
        apply=True,
    )
    assert result["pause_by_hand"] == ["claude: morning"]


def test_the_checkout_is_found_next_to_the_data_folder(tmp_path: Path) -> None:
    (tmp_path / "Mail" / "monitor").mkdir(parents=True)
    (tmp_path / "Mail-data").mkdir()
    assert guess_checkout(tmp_path / "Mail-data") == tmp_path / "Mail"
    assert guess_checkout(tmp_path / "Other") is None


# --- item 5: iCloud after import ---------------------------------------------------------


def test_prepare_connects_icloud_when_the_saved_password_works(
    old_root: Path, data_root: Path, secrets: MemorySecrets, clock: ManualClock
) -> None:
    seen: list[tuple[str, str]] = []

    def icloud(username: str, password: str) -> list[Discovered]:
        seen.append((username, password))
        return [Discovered("Home", "https://caldav.example/1/home/")]

    migrate.prepare_migration(
        old_root, data_root, clock=clock, secrets=secrets, caldav_check=icloud
    )
    assert seen == [("owner@icloud.example", "abcd-efgh-ijkl-mnop")]
    assert load_connections(data_root).sources["calendar:icloud"].enabled


def test_without_a_check_the_summary_names_the_one_command(
    old_root: Path, data_root: Path, secrets: MemorySecrets, clock: ManualClock
) -> None:
    migrate.prepare_migration(old_root, data_root, clock=clock, secrets=secrets)
    report = json.loads((data_root / "import" / "report.json").read_text(encoding="utf-8"))
    assert not load_connections(data_root).sources["calendar:icloud"].enabled
    assert (
        "chamberlain setup icloud register --apple-id owner@icloud.example"
        in (report["sources"]["calendar:icloud"])
    )


# --- item 6: carried instructions that name v1 commands ----------------------------------


def test_instructions_naming_v1_commands_are_flagged_not_deleted(
    old_root: Path, data_root: Path, secrets: MemorySecrets, clock: ManualClock
) -> None:
    custom = yaml.safe_load((old_root / "customisations.yaml").read_text(encoding="utf-8"))
    custom["instructions"]["tooling"] = "Start every pass with `briefing`, then `outlook status`."
    (old_root / "customisations.yaml").write_text(yaml.safe_dump(custom), encoding="utf-8")
    migrate.prepare_migration(old_root, data_root, clock=clock, secrets=secrets)
    report = json.loads((data_root / "import" / "report.json").read_text(encoding="utf-8"))
    assert report["instructions_to_review"] == ["tooling"]
    topics = yaml.safe_load(
        (data_root / "config" / "owner-instructions.yaml").read_text(encoding="utf-8")
    )["topics"]
    assert "briefing" in topics["tooling"]["text"]
    assert "filing" in topics


# --- item 7: the old standing grants ------------------------------------------------------


def test_grants_lists_old_standing_wording_with_a_proposed_scope(old_root: Path) -> None:
    rows = migrate.old_standing_grants(old_root, ManualClock(datetime(2026, 1, 5, tzinfo=UTC)))
    by_id = {row["old_id"]: row for row in rows}
    assert {"sa-dated", "sa-undated", "sa-expired"} <= set(by_id)
    row = by_id["sa-dated"]
    assert row["title"] == "Standing sa-dated"
    assert "forward invoices" in row["old_wording"]
    assert row["source"].startswith("mail:")
    assert row["obsolete_in_v2"] is False
    assert by_id["sa-expired"]["expired_in_v1"] is True


# --- item 8: v1 processes and add-on ------------------------------------------------------


def test_v1_processes_block_prepare_with_a_fix(
    old_root: Path, data_root: Path, secrets: MemorySecrets, clock: ManualClock, tmp_path: Path
) -> None:
    checkout = tmp_path / "old"
    running = [
        RunningProcess(10, "python.exe", f"python {checkout}\\dashboard.py"),
        RunningProcess(11, "python.exe", "python -m monitor dashboard"),
        RunningProcess(12, "node.exe", "node other.js"),
        RunningProcess(os.getpid(), "python.exe", "python -m monitor"),
        RunningProcess(13, "python.exe", "python -m chamberlain migrate prepare -m monitor"),
    ]
    assert [p.pid for p in v1_processes(running, checkout)] == [10, 11]
    with pytest.raises(ChamberlainError) as raised:
        migrate.prepare_migration(
            old_root,
            data_root,
            clock=clock,
            secrets=secrets,
            processes=lambda: running,
            old_checkout=checkout,
        )
    assert "still running" in raised.value.message
    assert raised.value.fix
    assert not (data_root / "import" / "migration.json").exists()
    refuse_while_running([running[2]], checkout)
    migrate.prepare_migration(
        old_root, data_root, clock=clock, secrets=secrets, processes=lambda: [running[2]]
    )


class FakeDesktop:
    def __init__(self, running: bool) -> None:
        self.is_running = running
        self.closed = 0

    def running(self) -> bool:
        return self.is_running

    def close(self) -> None:
        self.closed += 1
        self.is_running = False

    def start(self) -> None:
        raise AssertionError("never restarted")


def _thunderbird(tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "tb"
    profile = home / "Profiles" / "x.default"
    (profile / "extensions").mkdir(parents=True)
    (home / "profiles.ini").write_text(
        "[Profile0]\nName=default\nIsRelative=1\nPath=Profiles/x.default\n", encoding="utf-8"
    )
    xpi = profile / "extensions" / V1_ADDON_FILE
    xpi.write_bytes(b"v1")
    return home, xpi


def test_retire_archives_the_v1_addon_and_host_after_closing_thunderbird(tmp_path: Path) -> None:
    home, xpi = _thunderbird(tmp_path)
    manifest = tmp_path / "host.json"
    manifest.write_text("{}", encoding="utf-8")
    archive = tmp_path / "retired"
    desktop = FakeDesktop(running=True)
    host = ManifestFileHost(manifest)

    plan = retire_v1_addon(archive, apply=False, desktop=desktop, host=host, home=home)
    assert xpi.exists()
    assert manifest.exists()
    assert not archive.exists()
    assert desktop.closed == 0
    assert plan.steps

    retire_v1_addon(
        archive, apply=True, desktop=desktop, host=host, home=home, sleep=lambda _s: None
    )
    assert desktop.closed == 1
    assert not xpi.exists()
    assert not manifest.exists()
    assert (archive / ("v1-addon-" + V1_ADDON_FILE)).read_bytes() == b"v1"
    assert (archive / "v1-native-host-host.json").read_text(encoding="utf-8") == "{}"
    retire_v1_addon(archive, apply=True, desktop=desktop, host=host, home=home)  # idempotent


def test_retire_stops_if_thunderbird_will_not_close(tmp_path: Path) -> None:
    home, xpi = _thunderbird(tmp_path)

    class Stuck(FakeDesktop):
        def close(self) -> None:
            self.closed += 1

    with pytest.raises(ChamberlainError):
        retire_v1_addon(
            tmp_path / "r",
            apply=True,
            desktop=Stuck(True),
            host=ManifestFileHost(tmp_path / "none"),
            home=home,
            sleep=lambda _s: None,
        )
    assert xpi.exists()


# --- item 10: the backup lives under the data root ---------------------------------------


def test_backup_is_under_the_data_root_and_a_legacy_one_still_resumes(
    old_root: Path, data_root: Path, secrets: MemorySecrets, clock: ManualClock
) -> None:
    status = migrate.prepare_migration(old_root, data_root, clock=clock, secrets=secrets)
    assert status.backup == data_root / "import" / "v1-backup"
    assert not (data_root.parent / (data_root.name + ".v1-backup")).exists()

    legacy = data_root.parent / (data_root.name + ".v1-backup")
    status.backup.replace(legacy)
    assert migrate.migration_status(old_root, data_root).backup == legacy
    assert migrate.verify_migration(old_root, data_root).stage == "verified"


# --- item 11: archive ---------------------------------------------------------------------


class Recorder:
    def __init__(self) -> None:
        self.deleted: list[tuple[Path, datetime]] = []
        self.moves: list[list[tuple[Path, Path]]] = []

    def delete_after(self, folder: Path, when: datetime) -> str:
        self.deleted.append((folder, when))
        return "registered"

    def move_later(self, moves: Sequence[tuple[Path, Path]], script: Path, now: datetime) -> str:
        self.moves.append(list(moves))
        return "later"


def _tree(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    for name in ("OutlookAutomation", "OutlookAutomation-data", "backup", "extra"):
        (tmp_path / "dev" / name).mkdir(parents=True)
        (tmp_path / "dev" / name / "f.txt").write_text(name, encoding="utf-8")
    base = tmp_path / "dev"
    return (
        base / "OutlookAutomation",
        base / "OutlookAutomation-data",
        base / "backup",
        base / "extra",
    )


def test_archive_dry_run_moves_nothing_then_apply_moves_and_writes_rollback(
    tmp_path: Path,
) -> None:
    checkout, data, backup, extra = _tree(tmp_path)
    scheduler = Recorder()
    clock = ManualClock(datetime(2026, 11, 26, 12, 0, tzinfo=UTC))
    moved: list[tuple[Path, Path]] = []

    def mover(source: Path, target: Path) -> None:
        moved.append((source, target))
        source.rename(target)

    common = {
        "old_root": data,
        "old_checkout": checkout,
        "import_backup": backup,
        "also": [extra],
        "keep_days": 30,
        "scheduler": scheduler,
        "mover": mover,
        "in_use": lambda _p: False,
        "clock": clock,
    }
    plan = archive_v1(apply=False, **common)  # type: ignore[arg-type]
    assert plan.archive.name == "2026-11-26-pre-v2-switch" or plan.archive.name.startswith(
        "2026-11-"
    )
    assert plan.archive.parent == tmp_path / "dev" / "_archive"
    assert checkout.exists()
    assert not moved
    assert not scheduler.deleted

    done = archive_v1(apply=True, **common)  # type: ignore[arg-type]
    assert len(moved) == 4
    assert not checkout.exists()
    assert not extra.exists()
    assert (done.archive / "OutlookAutomation-data.v1-backup" / "f.txt").exists()
    text = (done.archive / "ROLLBACK.md").read_text(encoding="utf-8")
    assert "Rolling back to v1" in text
    assert "OutlookAutomation-data" in text
    assert scheduler.deleted
    assert scheduler.deleted[0][0] == done.archive
    assert (scheduler.deleted[0][1] - clock.now()).days in {29, 30}
    again = archive_v1(apply=True, **common)  # type: ignore[arg-type]
    assert again.moved == []
    assert len(again.skipped) == 4


def test_a_folder_in_use_is_moved_later_not_now(tmp_path: Path) -> None:
    checkout, data, _backup, _extra = _tree(tmp_path)
    scheduler = Recorder()
    result = archive_v1(
        old_root=data,
        old_checkout=checkout,
        import_backup=None,
        apply=True,
        scheduler=scheduler,
        in_use=lambda p: p == checkout,
        clock=ManualClock(datetime(2026, 11, 26, 12, 0, tzinfo=UTC)),
    )
    assert checkout.exists()
    assert not data.exists()
    assert scheduler.moves == [[(checkout, result.archive / "OutlookAutomation")]]
    assert "move later" in (result.archive / "ROLLBACK.md").read_text(encoding="utf-8")


def test_a_failing_move_falls_back_to_the_later_move(tmp_path: Path) -> None:
    checkout, data, _b, _e = _tree(tmp_path)
    scheduler = Recorder()

    def locked(_source: Path, _target: Path) -> None:
        raise PermissionError("in use")

    result = archive_v1(
        old_root=data,
        old_checkout=checkout,
        import_backup=None,
        apply=True,
        scheduler=scheduler,
        mover=locked,
        in_use=lambda _p: False,
        clock=ManualClock(datetime(2026, 11, 26, 12, 0, tzinfo=UTC)),
    )
    assert checkout.exists()
    assert len(scheduler.moves[0]) == 2
    assert result.scheduled_moves


def test_windows_scheduler_registers_one_time_tasks_through_the_runner(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def run(args: list[str]) -> int:
        calls.append(args)
        return 0

    scheduler = WindowsScheduler(run)
    when = datetime(2026, 12, 26, 12, 0, tzinfo=UTC)
    text = scheduler.delete_after(tmp_path / "arch", when)
    assert CLEANUP_TASK in text
    assert "2026-12-26" in text
    assert "Register-ScheduledTask" in calls[0][-1]
    assert "2026-12-26T03:00:00" in calls[0][-1]
    script = tmp_path / "finish.cmd"
    scheduler.move_later([(tmp_path / "a", tmp_path / "b")], script, when)
    assert "move" in script.read_text(encoding="utf-8")
    assert "AtLogOn" in calls[1][-1]

    failing = WindowsScheduler(lambda _a: 1)
    with pytest.raises(OSError, match="could not be registered"):
        failing.delete_after(tmp_path, when)
    assert "rm -rf" in ManualScheduler().delete_after(tmp_path, when)


# --- the command line ---------------------------------------------------------------------


def test_cli_switch_plans_then_applies_pause_and_retire(
    old_root: Path,
    data_root: Path,
    secrets: MemorySecrets,
    clock: ManualClock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migrate.prepare_migration(old_root, data_root, clock=clock, secrets=secrets)
    migrate.verify_migration(old_root, data_root)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    codex = tmp_path / "codex"
    toml = _codex(codex, "check", str(checkout))
    home, xpi = _thunderbird(tmp_path)
    manifest = tmp_path / "host.json"
    manifest.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(migration, "app_homes", lambda: (codex, tmp_path / "claude"))
    monkeypatch.setattr(migration, "thunderbird_home", lambda: home)
    monkeypatch.setattr(migration, "SystemDesktop", lambda: FakeDesktop(False))
    monkeypatch.setattr(migration, "system_native_host", lambda: ManifestFileHost(manifest))

    def command(*options: str) -> dict[str, object]:
        stream = StringIO()
        args = ["--data", str(data_root), "migrate", "switch", str(old_root), *options]
        assert cli.main(args, stream) == 0, stream.getvalue()
        return json.loads(stream.getvalue())

    plan = command("--pause-app-automations", "--old-checkout", str(checkout))
    assert plan["stage"] == "verified"
    assert "note" in plan
    assert 'status = "ACTIVE"' in toml.read_text(encoding="utf-8")
    assert xpi.exists()

    done = command("--pause-app-automations", "--old-checkout", str(checkout), "--apply")
    assert done["app_automations"]["paused"] == ["Name of check"]  # type: ignore[index]
    assert 'status = "PAUSED"' in toml.read_text(encoding="utf-8")
    assert not xpi.exists()
    assert not manifest.exists()
    assert (data_root / "import" / "v1-retired" / ("v1-addon-" + V1_ADDON_FILE)).exists()
