import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from old_fixture import MemorySecrets

from chamberlain.importer.migrate import (
    SwitchTarget,
    detect_migration,
    migration_status,
    prepare_migration,
    switch_migration,
    verify_migration,
)
from chamberlain.ports.clock import ManualClock


@pytest.fixture(autouse=True)
def isolate_legacy_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "empty-local"))


def fingerprint(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file()
    }


def test_detect_drift_and_reject_changed_target(
    old_root: Path, data_root: Path, secrets: MemorySecrets, clock: ManualClock, tmp_path: Path
) -> None:
    prepare_migration(old_root, data_root, clock=clock, secrets=secrets)
    db = sqlite3.connect(data_root / "chamberlain.sqlite3")
    db.execute("DELETE FROM actions")
    db.commit()
    db.close()
    result = verify_migration(old_root, data_root)
    assert result.verification is not None
    assert not result.verification.passed
    assert migration_status(old_root, data_root).verification == result.verification
    target = data_root / "import" / "sandbox" / "scheduler.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('{"active":"v1", "schedules": []}')
    with pytest.raises(ValueError, match="successful verification"):
        switch_migration(old_root, data_root, switch=SwitchTarget(target, "bad"))
    assert json.loads(target.read_text())["active"] == "v1"


def test_refuses_changed_backup(
    old_root: Path, data_root: Path, secrets: MemorySecrets, clock: ManualClock
) -> None:
    result = prepare_migration(old_root, data_root, clock=clock, secrets=secrets)
    (result.backup / "unexpected").write_text("changed")
    with pytest.raises(ValueError, match="backup has changed"):
        verify_migration(old_root, data_root)


def test_windows_switch_checks_readback_and_rolls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    from chamberlain.importer import migrate

    target = migrate.WindowsTaskSwitch("old", "new", "C:/old.exe", "C:/new.exe")
    states = {"old": True, "new": False}
    calls: list[tuple[str, bool]] = []

    def info(name: str, command: str, arguments: str) -> tuple[bool, bytes]:
        return states[name], name.encode()

    def change(name: str, *, enabled: bool) -> None:
        calls.append((name, enabled))
        states[name] = enabled

    monkeypatch.setattr(migrate, "_task_info", info)
    monkeypatch.setattr(migrate, "_task_busy", lambda name: False)
    monkeypatch.setattr(migrate, "_task_change", change)
    migrate._switch_windows(target)
    assert states == {"old": False, "new": True}
    assert calls == [("old", False), ("new", True)]

    states.update(old=True, new=False)
    calls.clear()

    def failed_change(name: str, *, enabled: bool) -> None:
        if name == "new" and enabled:
            raise OSError("synthetic failure")
        change(name, enabled=enabled)

    monkeypatch.setattr(migrate, "_task_change", failed_change)
    with pytest.raises(RuntimeError, match="old task was restored"):
        migrate._switch_windows(target)
    assert states == {"old": True, "new": False}
    assert calls == [("old", False), ("new", False), ("old", True)]


def test_interrupted_windows_switch_resumes(
    old_root: Path,
    data_root: Path,
    secrets: MemorySecrets,
    clock: ManualClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from chamberlain.importer import migrate

    prepare_migration(old_root, data_root, clock=clock, secrets=secrets)
    verify_migration(old_root, data_root)
    target = migrate.WindowsTaskSwitch("old", "new", "C:/old.exe", "C:/new.exe")
    states = {"old": True, "new": False}
    interrupted = False

    def info(name: str, command: str, arguments: str) -> tuple[bool, bytes]:
        return states[name], name.encode()

    def change(name: str, *, enabled: bool) -> None:
        nonlocal interrupted
        states[name] = enabled
        if name == "old" and not enabled and not interrupted:
            interrupted = True
            raise KeyboardInterrupt()

    monkeypatch.setattr(migrate, "_task_info", info)
    monkeypatch.setattr(migrate, "_task_change", change)
    monkeypatch.setattr(migrate, "_task_busy", lambda name: False)
    with pytest.raises(KeyboardInterrupt):
        switch_migration(old_root, data_root, switch=target)
    assert states == {"old": False, "new": False}
    assert switch_migration(old_root, data_root, switch=target).stage == "switched"
    assert states == {"old": False, "new": True}
    assert switch_migration(old_root, data_root, switch=target).stage == "switched"
    assert states == {"old": False, "new": True}


def test_busy_old_task_is_restored(monkeypatch: pytest.MonkeyPatch) -> None:
    from chamberlain.importer import migrate

    states = {"old": True, "new": False}

    def info(name: str, command: str, arguments: str) -> tuple[bool, bytes]:
        return states[name], name.encode()

    def change(name: str, *, enabled: bool) -> None:
        states[name] = enabled

    monkeypatch.setattr(migrate, "_task_info", info)
    monkeypatch.setattr(migrate, "_task_change", change)
    monkeypatch.setattr(migrate, "_task_busy", lambda name: name == "old")
    with pytest.raises(RuntimeError, match="old task was restored"):
        migrate._switch_windows(migrate.WindowsTaskSwitch("old", "new", "C:/old.exe", "C:/new.exe"))
    assert states == {"old": True, "new": False}


def test_report_and_instruction_drift_are_detected(
    old_root: Path, data_root: Path, secrets: MemorySecrets, clock: ManualClock, tmp_path: Path
) -> None:
    import yaml

    from chamberlain.connections.load import load_connections

    prepare_migration(old_root, data_root, clock=clock, secrets=secrets)
    sources = load_connections(data_root).sources
    name = next(iter(sources))
    values = sources[name].model_dump(mode="json")
    key = next(iter(values))
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"sources": {name: {key: values[key]}}}))
    result = verify_migration(old_root, data_root, report_snapshot=report)
    assert result.verification is not None
    assert result.verification.passed
    report.write_text(json.dumps({"sources": {name: {key: "wrong"}}}))
    result = verify_migration(old_root, data_root, report_snapshot=report)
    assert result.verification is not None
    assert any("source report" in item for item in result.verification.differences)
    path = data_root / "config" / "owner-instructions.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["topics"][next(iter(document["topics"]))]["text"] = "changed"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    result = verify_migration(old_root, data_root)
    assert result.verification is not None
    assert any("owner instruction" in item for item in result.verification.differences)


def test_backup_excludes_unrelated_files(
    old_root: Path, data_root: Path, secrets: MemorySecrets, clock: ManualClock
) -> None:
    (old_root / "download.dat").write_bytes(b"unrelated")
    backup = prepare_migration(old_root, data_root, clock=clock, secrets=secrets).backup
    assert not (backup / "download.dat").exists()
    assert (old_root / "download.dat").is_file()


def test_rollback_waits_for_running_new_task(monkeypatch: pytest.MonkeyPatch) -> None:
    from chamberlain.importer import migrate

    states = {"old": True, "new": False}
    running = {"old": False, "new": False}

    def info(name: str, command: str, arguments: str) -> tuple[bool, bytes]:
        return states[name], name.encode()

    def change(name: str, *, enabled: bool) -> None:
        states[name] = enabled
        if name == "new" and enabled:
            running[name] = True
            raise OSError("synthetic response failure")

    monkeypatch.setattr(migrate, "_task_info", info)
    monkeypatch.setattr(migrate, "_task_change", change)
    monkeypatch.setattr(migrate, "_task_busy", lambda name: running[name])
    with pytest.raises(RuntimeError, match=r"Both tasks are disabled.*still running"):
        migrate._switch_windows(migrate.WindowsTaskSwitch("old", "new", "C:/old.exe", "C:/new.exe"))
    assert states == {"old": False, "new": False}


def test_sandbox_switch_resumes_after_manifest_write(
    old_root: Path,
    data_root: Path,
    secrets: MemorySecrets,
    clock: ManualClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from chamberlain.importer import migrate

    before = fingerprint(old_root)
    assert detect_migration(old_root)["detected"] is True
    assert migration_status(old_root, data_root).stage == "not_started"
    assert not data_root.exists()
    result = prepare_migration(old_root, data_root, clock=clock, secrets=secrets)
    assert result.stage == "imported"
    backup = fingerprint(result.backup)
    assert backup == {name: before[name] for name in backup}
    assert fingerprint(old_root) == before
    assert prepare_migration(old_root, data_root, clock=clock, secrets=secrets).stage == "imported"
    verified = verify_migration(old_root, data_root)
    assert verified.verification is not None
    assert verified.verification.passed
    assert verified.verification.matched_cards > 0
    saved = migration_status(old_root, data_root)
    assert saved.stage == "verified"
    assert saved.verification == verified.verification
    path = data_root / "import" / "sandbox" / "scheduler.json"
    path.parent.mkdir(parents=True)
    original_manifest = {"active": "v1", "schedules": [{"time": "08:00"}], "other": 42}
    path.write_text(json.dumps(original_manifest), encoding="utf-8")
    target = SwitchTarget(path, hashlib.sha256(path.read_bytes()).hexdigest())
    original = migrate._atomic_json

    def interrupt(path: Path, value: object) -> None:
        if (
            path.name == "migration.json"
            and isinstance(value, dict)
            and value.get("stage") == "switched"
        ):
            raise KeyboardInterrupt()
        original(path, value)

    monkeypatch.setattr(migrate, "_atomic_json", interrupt)
    with pytest.raises(KeyboardInterrupt):
        switch_migration(old_root, data_root, switch=target)
    assert json.loads(path.read_text())["active"] == "v2"
    assert migration_status(old_root, data_root).stage == "switching"
    monkeypatch.setattr(migrate, "_atomic_json", original)
    switched = switch_migration(old_root, data_root, switch=target)
    assert switched.stage == "switched"
    assert migration_status(old_root, data_root).verification == switched.verification
    assert switch_migration(old_root, data_root, switch=target).stage == "switched"
    assert json.loads(path.read_text()) == {**original_manifest, "active": "v2"}
    assert fingerprint(old_root) == before


def test_migration_does_not_consult_external_legacy_credential_location(
    old_root: Path,
    data_root: Path,
    secrets: MemorySecrets,
    clock: ManualClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from chamberlain.importer import run

    extra_dirs_seen: list[object] = []

    def credential(root: Path, *, extra_dirs: object = None) -> None:
        extra_dirs_seen.append(extra_dirs)

    monkeypatch.setattr(run, "icloud_credential", credential)
    prepare_migration(old_root, data_root, clock=clock, secrets=secrets)
    assert extra_dirs_seen == [[]]
