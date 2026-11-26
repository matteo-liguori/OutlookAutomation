"""The migration runner: docs/contracts/09-storage.md (S-8 to S-12)."""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.store.legacy import legacy_migrations

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


def test_m35_migration_0080_keeps_every_artifact_owner_of_a_store_at_0071(
    clock: ManualClock,
) -> None:
    """A live store at 0071 holds real owner rows; the rebuild must carry all of them."""
    db = connect(MEMORY)
    upto = [m for m in legacy_migrations() if m.version <= 71]
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
    assert migrate(db, clock, backup_dir=None, migrations=legacy_migrations())[0] == 80
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


def test_m36_migration_0120_gives_each_seen_message_one_key_and_drops_second_copies(
    clock: ManualClock,
) -> None:
    """A live store at 0080 stored one row per copy; it must come out with one row per message."""
    db = connect(MEMORY)
    upto = [m for m in legacy_migrations() if m.version <= 80]
    assert migrate(db, clock, backup_dir=None, migrations=upto)[-1] == 80
    when = "2026-01-01T00:00:00Z"
    copies = [
        ("f-inbox/<A@x>", "msg_a1", "<A@x>"),
        ("f-all/<A@x>", "msg_a2", "<a@x>"),
        ("f-inbox/<B@x>", "msg_b1", "<B@x>"),
        ("f-inbox/none", "msg_c1", None),
    ]
    for locator, handle, message_id in copies:
        db.execute(
            "INSERT INTO refs (handle, kind, source_id, locator, internet_message_id, "
            "first_seen_at, last_seen_at) VALUES (?, 'message', 'mail:t', ?, ?, ?, ?)",
            (handle, locator, message_id, when, when),
        )
        db.execute(
            "INSERT INTO mail_seen (source_id, locator, handle, received_at, first_seen_at) "
            "VALUES ('mail:t', ?, ?, ?, ?)",
            (locator, handle, when, when),
        )
    db.execute(
        "INSERT INTO mail_watermarks (source_id, covered_through, watching_since, "
        "scan_started_at, scan_until) VALUES ('mail:t', ?, ?, ?, ?)",
        (when, when, when, when),
    )
    db.commit()
    assert migrate(db, clock, backup_dir=None, migrations=legacy_migrations())[-1] >= 120
    rows = db.execute("SELECT locator, message_key, sent FROM mail_seen ORDER BY seq").fetchall()
    assert [(r["locator"], r["message_key"], r["sent"]) for r in rows] == [
        ("f-inbox/<A@x>", "a@x", 0),
        ("f-inbox/<B@x>", "b@x", 0),
        ("f-inbox/none", "loc:f-inbox/none", 0),
    ]
    mark = db.execute(
        "SELECT scan_until, scan_started_at, held_from FROM mail_watermarks"
    ).fetchone()
    assert mark["scan_until"] is None
    assert mark["held_from"] is None
    assert mark["scan_started_at"] == when, "an unfinished scan restarts from the watermark"
    db.close()


def test_a1_migration_0250_retires_only_old_card_mail_events(
    empty: sqlite3.Connection, clock: ManualClock
) -> None:
    before = [m for m in legacy_migrations() if m.version < 250]
    migrate(empty, clock, backup_dir=None, migrations=before)
    empty.executemany(
        "INSERT INTO events (handle, kind, dedupe_key, priority, summary, detected_at) "
        "VALUES (?, ?, ?, 40, 'New mail', '2026-01-01T00:00:00Z')",
        [
            ("evt_0000000000000001", "mail.new_for_card", "retired"),
            ("evt_0000000000000002", "mail.new_unmatched", "current"),
            ("evt_0000000000000003", "operation.uncertain", "unsettled"),
        ],
    )
    assert 250 in migrate(empty, clock, backup_dir=None, migrations=legacy_migrations())
    kinds = {row["kind"] for row in empty.execute("SELECT kind FROM events")}
    assert kinds == {"mail.new_unmatched", "operation.uncertain"}


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
