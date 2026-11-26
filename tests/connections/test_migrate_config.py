"""Config preview uses a consistent store copy and apply keeps recoverable backups."""

from contextlib import closing
from pathlib import Path

import pytest
from tests.store.legacy import legacy_migrations

from chamberlain.connections import migrate_config as config_module
from chamberlain.connections.load import profile_path
from chamberlain.connections.migrate_config import migrate_config
from chamberlain.ports.clock import ManualClock
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.store.db import DB_NAME, connect
from chamberlain.store.migrate import (
    BASELINE_VERSION,
    BRIDGE_VERSION,
    applied_versions,
    discover,
    migrate,
)
from chamberlain.writes.audit import AuditChain, verify

PROFILE = (
    b"\xef\xbb\xbf# Keep this comment and these line endings.\r\nversion: 1\r\ntimezone: UTC\r\n"
)


def _write_profile(root: Path, contents: bytes) -> Path:
    profile = profile_path(root)
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_bytes(contents)
    return profile


def _legacy(data_root: Path, clock: ManualClock) -> Path:
    data_root.mkdir(exist_ok=True)
    path = data_root / DB_NAME
    with closing(connect(path)) as installed:
        migrate(
            installed,
            clock,
            backup_dir=None,
            migrations=[m for m in legacy_migrations() if m.version < BRIDGE_VERSION],
        )
        AuditChain(installed, clock).append(
            actor="system:test", kind="sweep", subject=None, summary="Kept through upgrade."
        )
    return path


def test_config_migrate_proves_preview_and_retains_db_and_exact_yaml_backup(
    data_root: Path, clock: ManualClock
) -> None:
    database = _legacy(data_root, clock)
    profile = _write_profile(data_root, PROFILE)
    original = database.read_bytes()
    preview = migrate_config(data_root, clock=clock)
    assert preview.profile == profile
    assert preview.store_version == 250
    current = discover()[-1].version
    assert preview.pending == (BRIDGE_VERSION, BASELINE_VERSION, 280)
    assert preview.applied == ()
    assert preview.backup is None
    assert database.read_bytes() == original
    assert {p.name for p in data_root.iterdir()} == {"config", DB_NAME}

    result = migrate_config(data_root, apply=True, clock=clock)
    assert result.applied == preview.pending
    assert result.backup is not None
    assert (result.backup / profile.name).read_bytes() == PROFILE
    archived = result.backup / f"pre-migration-{BASELINE_VERSION:04d}.sqlite3"
    for path, version in ((archived, 250), (database, current)):
        with closing(connect(path)) as store:
            assert max(applied_versions(store)) == version
            audit = verify(store)
            assert audit.ok
            assert audit.rows == 1

    current_bytes = database.read_bytes()
    current_contents = {p.relative_to(data_root) for p in data_root.rglob("*")}
    repeated = migrate_config(data_root, apply=True, clock=clock)
    assert repeated.store_version == current
    assert repeated.pending == repeated.applied == ()
    assert repeated.backup is None
    assert database.read_bytes() == current_bytes
    assert profile.read_bytes() == PROFILE
    assert {p.relative_to(data_root) for p in data_root.rglob("*")} == current_contents


def test_config_migrate_reads_committed_wal_instead_of_stale_main_db(
    data_root: Path, clock: ManualClock
) -> None:
    database = _legacy(data_root, clock)
    with closing(connect(database)) as active:
        active.execute("PRAGMA wal_autocheckpoint = 0")
        active.execute(
            "INSERT INTO schema_migrations VALUES (?, 'future', ?, '2027-01-01T00:00:00Z')",
            (900, "a" * 64),
        )
        before = database.read_bytes()
        with pytest.raises(ChamberlainError) as refused:
            migrate_config(data_root, apply=True, clock=clock)
        assert refused.value.code is ErrorCode.STORE_MIGRATION_FAILED
        assert database.read_bytes() == before
        assert max(applied_versions(active)) == 900
        assert not (data_root / "backups").exists()


def test_config_migrate_with_no_store_creates_nothing(data_root: Path) -> None:
    preview = migrate_config(data_root, apply=True)
    assert preview.store_version is None
    assert preview.backup is None
    assert not data_root.exists()


def test_config_migrate_does_not_recreate_a_store_removed_after_preview(
    data_root: Path, clock: ManualClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = _legacy(data_root, clock)
    preview = config_module._preview_store

    def relocate_after_preview(path: Path, using_clock: ManualClock) -> tuple[int, tuple[int, ...]]:
        result = preview(path, using_clock)
        path.rename(path.with_suffix(".original"))
        return result

    monkeypatch.setattr(config_module, "_preview_store", relocate_after_preview)
    with pytest.raises(ChamberlainError) as refused:
        migrate_config(data_root, apply=True, clock=clock)
    assert refused.value.code is ErrorCode.STORE_MIGRATION_FAILED
    assert not original.exists()
    assert not (data_root / "backups").exists()


@pytest.mark.parametrize(
    "unsupported",
    [b"version: 99\n", b"sources:\n  mail:test: {kind: demo}\n", b"config: [\n"],
)
def test_config_migrate_preserves_unsupported_profile(data_root: Path, unsupported: bytes) -> None:
    profile = _write_profile(data_root, unsupported)
    with pytest.raises(ChamberlainError) as refused:
        migrate_config(data_root, apply=True)
    assert refused.value.code is ErrorCode.CONFIG_INVALID
    assert profile.read_bytes() == unsupported
    assert not (data_root / DB_NAME).exists()
