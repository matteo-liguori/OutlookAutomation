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
    assert found[0].version == 270
    assert [m.version for m in found] == sorted({m.version for m in found})


def test_s8_migrations_apply_in_order_once(empty: sqlite3.Connection, clock: ManualClock) -> None:
    assert migrate(empty, clock, backup_dir=None, migrations=[TWO, ONE][::-1]) == [1, 2]
    assert migrate(empty, clock, backup_dir=None, migrations=[ONE, TWO]) == []
    assert {"one", "two", "schema_migrations"} <= table_names(empty)
    assert applied_versions(empty)[2] == ("two", TWO.checksum)


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


def test_s10_a_retry_publishes_a_new_backup_without_replacing_the_first(
    tmp_path: Path, clock: ManualClock
) -> None:
    database = connect(tmp_path / DB_NAME)
    backups = tmp_path / "backups"
    first = backups / "pre-migration-0003.sqlite3"
    second = backups / "pre-migration-0003-2.sqlite3"
    try:
        migrate(database, clock, backup_dir=backups, migrations=[ONE])
        database.execute("INSERT INTO one VALUES (7)")
        with pytest.raises(ChamberlainError):
            migrate(database, clock, backup_dir=backups, migrations=[ONE, BROKEN])
        previous = first.read_bytes()
        database.execute("INSERT INTO one VALUES (8)")
        with pytest.raises(ChamberlainError):
            migrate(database, clock, backup_dir=backups, migrations=[ONE, BROKEN])
        assert first.read_bytes() == previous
        assert set(backups.iterdir()) == {first, second}
        assert set(applied_versions(database)) == {1}
    finally:
        database.close()
    for path, expected in ((first, [7]), (second, [7, 8])):
        archive = connect(path)
        try:
            assert [
                row["id"] for row in archive.execute("SELECT id FROM one ORDER BY id")
            ] == expected
        finally:
            archive.close()
