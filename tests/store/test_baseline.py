"""The Step 3 store is adopted without rebuilding or losing application records."""

from pathlib import Path

import pytest
from tests.store.legacy import legacy_migrations

from chamberlain.ports.clock import ManualClock
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.store.db import DB_NAME, MEMORY, connect
from chamberlain.store.migrate import applied_versions, discover, migrate
from chamberlain.writes.audit import AuditChain, verify


def schema(db):
    return [
        tuple(row)
        for row in db.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        )
    ]


def rows(db):
    names = [
        r[0]
        for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND (name NOT LIKE 'sqlite_%' OR name = 'sqlite_sequence') "
            "AND name != 'schema_migrations' ORDER BY name"
        )
    ]
    return {
        name: sorted((tuple(row) for row in db.execute(f'SELECT * FROM "{name}"')), key=repr)
        for name in names
    }


def populate(db, clock: ManualClock) -> None:
    when = "2026-01-05T09:00:00Z"
    db.execute(
        "INSERT INTO sources(id,kind,connector,created_at,updated_at) "
        "VALUES ('mail:t','mail','thunderbird',?,?)",
        (when, when),
    )
    db.execute(
        "INSERT INTO grants(handle,kind,state,source_id,title,requirement,"
        "content_sha256,proposed_at) "
        "VALUES ('grant_t','one_off','proposed','mail:t','test','agent_evidence',?,?)",
        ("a" * 64, when),
    )
    db.execute(
        "INSERT INTO operations(handle,idempotency_key,kind,source_id,state,grant,intent_sha256,"
        "intent_json,created_at,updated_at) "
        "VALUES ('op_t','key-t','mail.send','mail:t','uncertain','grant_t',?,'{}',?,?)",
        ("b" * 64, when, when),
    )
    AuditChain(db, clock).append(actor="system:test", kind="test", subject="op_t", summary="kept")


def test_single_baseline_matches_archived_step3_schema_and_keeps_every_row(tmp_path: Path) -> None:
    clock = ManualClock()
    old = connect(tmp_path / DB_NAME)
    fresh = connect(MEMORY)
    try:
        assert len(discover()) == 1
        assert discover()[0].version == 270
        migrate(old, clock, backup_dir=None, migrations=legacy_migrations())
        populate(old, clock)
        before_rows = rows(old)
        before_ledger = [
            tuple(r) for r in old.execute("SELECT * FROM schema_migrations ORDER BY version")
        ]
        old_schema = schema(old)
        assert verify(old).ok
        assert migrate(fresh, clock, backup_dir=None) == [270]
        assert schema(fresh) == old_schema
        assert migrate(old, clock, backup_dir=tmp_path / "backups") == [270]
        assert schema(old) == old_schema
        assert rows(old) == before_rows
        assert [
            tuple(r)
            for r in old.execute(
                "SELECT * FROM schema_migrations WHERE version<270 ORDER BY version"
            )
        ] == before_ledger
        assert verify(old).ok
        assert old.execute("PRAGMA foreign_key_check").fetchall() == []
        assert old.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert migrate(old, clock, backup_dir=None) == []
        backup = connect(tmp_path / "backups" / "pre-migration-0270.sqlite3")
        try:
            assert rows(backup) == before_rows
            assert [
                tuple(r) for r in backup.execute("SELECT * FROM schema_migrations ORDER BY version")
            ] == before_ledger
        finally:
            backup.close()
    finally:
        old.close()
        fresh.close()


@pytest.mark.parametrize("damage", ["schema", "ledger", "partial"])
def test_unrecognized_prebaseline_store_is_rejected_without_stamping(damage: str) -> None:
    clock = ManualClock()
    db = connect(MEMORY)
    try:
        legacy = legacy_migrations()
        if damage == "partial":
            legacy = [m for m in legacy if m.version <= 230]
        migrate(db, clock, backup_dir=None, migrations=legacy)
        populate(db, clock)
        if damage == "schema":
            db.execute("CREATE TABLE unexpected (x TEXT)")
        elif damage == "ledger":
            db.execute("UPDATE schema_migrations SET checksum='changed' WHERE version=30")
        before_rows, before_ledger = rows(db), applied_versions(db)
        with pytest.raises(ChamberlainError) as raised:
            migrate(db, clock, backup_dir=None)
        assert raised.value.code is ErrorCode.STORE_MIGRATION_FAILED
        assert rows(db) == before_rows
        assert applied_versions(db) == before_ledger
    finally:
        db.close()
