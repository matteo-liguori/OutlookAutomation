"""The migration runner: docs/contracts/09-storage.md (S-8 to S-12)."""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from chamberlain.ports.clock import ManualClock
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.store.db import DB_NAME, MEMORY, connect, table_names, transaction
from chamberlain.store.migrate import (
    Migration,
    applied_versions,
    discover,
    migrate,
    open_store,
    split_statements,
)

ONE = Migration(1, "one", "CREATE TABLE one (id INTEGER PRIMARY KEY) STRICT;\n")
TWO = Migration(2, "two", "CREATE TABLE two (id INTEGER PRIMARY KEY) STRICT;\n")
BROKEN = Migration(
    3, "broken", "CREATE TABLE three (id INTEGER PRIMARY KEY) STRICT;\nCREATE TABLE one (x TEXT);\n"
)


@pytest.fixture
def empty() -> Iterator[sqlite3.Connection]:
    connection = connect(MEMORY)
    yield connection
    connection.close()


def test_s8_packaged_migrations_are_numbered_and_start_at_one() -> None:
    found = discover()
    assert found[0].version == 1
    assert [m.version for m in found] == sorted({m.version for m in found})


def test_s8_migrations_apply_in_order_once(empty: sqlite3.Connection, clock: ManualClock) -> None:
    assert migrate(empty, clock, backup_dir=None, migrations=[TWO, ONE][::-1]) == [1, 2]
    assert migrate(empty, clock, backup_dir=None, migrations=[ONE, TWO]) == []
    assert {"one", "two", "schema_migrations"} <= table_names(empty)
    assert applied_versions(empty)[2] == ("two", TWO.checksum)


def test_s8_the_ledger_records_when(empty: sqlite3.Connection, clock: ManualClock) -> None:
    migrate(empty, clock, backup_dir=None, migrations=[ONE])
    row = empty.execute("SELECT applied_at FROM schema_migrations").fetchone()
    assert row["applied_at"] == "2026-01-05T09:00:00Z"


def test_s9_an_edited_applied_migration_is_refused(
    empty: sqlite3.Connection, clock: ManualClock
) -> None:
    migrate(empty, clock, backup_dir=None, migrations=[ONE])
    edited = Migration(1, "one", ONE.sql + "-- changed later\n")
    with pytest.raises(ChamberlainError) as raised:
        migrate(empty, clock, backup_dir=None, migrations=[edited, TWO])
    assert raised.value.code is ErrorCode.STORE_MIGRATION_FAILED
    assert "two" not in table_names(empty)


def test_s9_line_endings_do_not_change_the_checksum() -> None:
    assert Migration(1, "one", ONE.sql.replace("\n", "\r\n")).checksum == ONE.checksum


def test_s10_a_failed_migration_rolls_back_whole(
    empty: sqlite3.Connection, clock: ManualClock
) -> None:
    with pytest.raises(ChamberlainError) as raised:
        migrate(empty, clock, backup_dir=None, migrations=[ONE, TWO, BROKEN])
    assert raised.value.code is ErrorCode.STORE_MIGRATION_FAILED
    assert set(applied_versions(empty)) == {1, 2}
    assert "three" not in table_names(empty)


def test_s10_a_backup_is_written_before_upgrading_existing_data(
    tmp_path: Path, clock: ManualClock
) -> None:
    connection = connect(tmp_path / DB_NAME)
    try:
        migrate(connection, clock, backup_dir=tmp_path / "backups", migrations=[ONE])
        assert not (tmp_path / "backups").exists()
        with transaction(connection):
            connection.execute("INSERT INTO one (id) VALUES (7)")
        migrate(connection, clock, backup_dir=tmp_path / "backups", migrations=[ONE, TWO])
    finally:
        connection.close()
    backup = connect(tmp_path / "backups" / "pre-migration-0002.sqlite3")
    try:
        assert backup.execute("SELECT id FROM one").fetchone()["id"] == 7
        assert "two" not in table_names(backup)
    finally:
        backup.close()


def test_s11_a_database_newer_than_the_code_is_refused(
    empty: sqlite3.Connection, clock: ManualClock
) -> None:
    migrate(empty, clock, backup_dir=None, migrations=[ONE, TWO])
    with pytest.raises(ChamberlainError):
        migrate(empty, clock, backup_dir=None, migrations=[ONE])


def test_s11_s12_gaps_are_allowed_but_out_of_order_is_refused(
    empty: sqlite3.Connection, clock: ManualClock
) -> None:
    thirty = Migration(30, "writes", "CREATE TABLE w (id INTEGER PRIMARY KEY) STRICT;\n")
    ten = Migration(10, "memory", "CREATE TABLE m (id INTEGER PRIMARY KEY) STRICT;\n")
    assert migrate(empty, clock, backup_dir=None, migrations=[ONE, thirty]) == [1, 30]
    with pytest.raises(ChamberlainError):
        migrate(empty, clock, backup_dir=None, migrations=[ONE, ten, thirty])


def test_s8_file_names_must_be_numbered(tmp_path: Path) -> None:
    (tmp_path / "0001_ok.sql").write_text(ONE.sql, encoding="utf-8")
    (tmp_path / "two.sql").write_text(TWO.sql, encoding="utf-8")
    with pytest.raises(ChamberlainError):
        discover(tmp_path)
    (tmp_path / "two.sql").unlink()
    (tmp_path / "0001_again.sql").write_text(TWO.sql, encoding="utf-8")
    with pytest.raises(ChamberlainError):
        discover(tmp_path)


def test_statements_split_on_semicolons_outside_strings_and_comments() -> None:
    script = "-- a comment; with a semicolon\nCREATE TABLE a (x TEXT DEFAULT ';');\n\nSELECT 1;\n"
    assert len(split_statements(script)) == 2
    with pytest.raises(ChamberlainError):
        split_statements("CREATE TABLE unfinished (x TEXT")


def test_s1_s5_transactions_roll_back_and_nest(db: sqlite3.Connection) -> None:
    def insert(key: str) -> None:
        with transaction(db):
            db.execute(
                "INSERT INTO system_state (key, value, updated_at) VALUES (?, 'v', 't')", (key,)
            )

    def outer() -> None:
        with transaction(db):
            insert("inner")
            raise RuntimeError("abandon the outer change")

    with pytest.raises(RuntimeError):
        outer()
    assert db.execute("SELECT count(*) AS n FROM system_state").fetchone()["n"] == 0
    assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_s18_open_store_creates_the_data_root_and_migrates(
    data_root: Path, clock: ManualClock
) -> None:
    connection = open_store(data_root, clock)
    try:
        assert (data_root / DB_NAME).exists()
        assert "cards" in table_names(connection)
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        connection.close()


def test_m35_migration_0080_keeps_every_artifact_owner_of_a_store_at_0071(
    clock: ManualClock,
) -> None:
    """A live store at 0071 holds real owner rows; the rebuild must carry all of them."""
    db = connect(MEMORY)
    upto = [m for m in discover() if m.version <= 71]
    assert migrate(db, clock, backup_dir=None, migrations=upto)[-1] == 71
    for n, (kind, owner) in enumerate(
        [
            ("action", "act_0000000000000001"),
            ("operation", "op_0000000000000001"),
            ("run", "run_00000001"),
            ("session", "ses_0000000000000001"),
            ("grant", "grant_0000000000000001"),
        ]
    ):
        db.execute(
            "INSERT INTO artifacts (handle, kind, media_type, size, sha256, reason, created_at, "
            "expires_at) VALUES (?, 'text', 'text/plain', 1, ?, 'r', "
            "'2026-01-01T00:00:00Z', '2026-02-01T00:00:00Z')",
            (f"art_{n:016d}", f"{n:064d}"),
        )
        db.execute(
            "INSERT INTO artifact_owners (artifact, owner_kind, owner, created_at) "
            "VALUES (?, ?, ?, '2026-01-01T00:00:00Z')",
            (f"art_{n:016d}", kind, owner),
        )
    db.commit()
    before = [tuple(r) for r in db.execute("SELECT * FROM artifact_owners ORDER BY artifact")]
    assert migrate(db, clock, backup_dir=None)[0] == 80
    after = [tuple(r) for r in db.execute("SELECT * FROM artifact_owners ORDER BY artifact")]
    assert after == before
    index = db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND name = 'artifact_owners_owner'"
    ).fetchone()
    assert index is not None
    db.execute(
        "INSERT INTO artifact_owners (artifact, owner_kind, owner, created_at) "
        "VALUES ('art_0000000000000000', 'card', 'card_0000000000000001', '2026-01-01T00:00:00Z')"
    )
    db.close()
