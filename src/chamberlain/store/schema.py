"""What each table is for: its data class, its one writer, and the system keys.

Contract: docs/contracts/09-storage.md (S-3, S-6) and docs/contracts/06-memory.md
(M-1, M-2). A test compares this with the live schema in both directions, so a
migration that adds a table must add it here, with a lifetime line in 06.
"""

import sqlite3
from enum import StrEnum

from chamberlain.ports.clock import Clock
from chamberlain.store.migrate import stamp


class DataClass(StrEnum):
    RECORDS = "records"
    EVIDENCE = "evidence"
    WORKING_SET = "working_set"
    EPHEMERAL = "ephemeral"
    SYSTEM = "system"


class Track(StrEnum):
    STORE = "store"
    APP = "app"
    MEMORY = "memory"
    ATTENTION = "attention"
    WRITES = "writes"
    RUNS = "runs"
    ANY = "any"


_D = DataClass
_T = Track

TABLES: dict[str, tuple[DataClass, Track]] = {
    "schema_migrations": (_D.SYSTEM, _T.STORE),
    "system_state": (_D.SYSTEM, _T.ANY),
    "sources": (_D.RECORDS, _T.APP),
    "source_health": (_D.EPHEMERAL, _T.ATTENTION),
    "sessions": (_D.EPHEMERAL, _T.ATTENTION),
    "events": (_D.EPHEMERAL, _T.ATTENTION),
    "guidance_deliveries": (_D.EPHEMERAL, _T.ATTENTION),
    "cards": (_D.RECORDS, _T.MEMORY),
    "actions": (_D.RECORDS, _T.MEMORY),
    "card_links": (_D.RECORDS, _T.MEMORY),
    "notes": (_D.WORKING_SET, _T.MEMORY),
    "evidence": (_D.EVIDENCE, _T.MEMORY),
    "evidence_refs": (_D.EVIDENCE, _T.MEMORY),
    "artifacts": (_D.WORKING_SET, _T.MEMORY),
    "artifact_owners": (_D.WORKING_SET, _T.MEMORY),
    "refs": (_D.WORKING_SET, _T.MEMORY),
    "leases": (_D.EPHEMERAL, _T.MEMORY),
    "cursors": (_D.EPHEMERAL, _T.MEMORY),
    "quarantine": (_D.SYSTEM, _T.MEMORY),
    "grants": (_D.RECORDS, _T.WRITES),
    "known_recipients": (_D.RECORDS, _T.WRITES),
    "operations": (_D.RECORDS, _T.WRITES),
    "audit_log": (_D.SYSTEM, _T.WRITES),
    "schedules": (_D.RECORDS, _T.RUNS),
    "runs": (_D.RECORDS, _T.RUNS),
    "user_notes": (_D.EPHEMERAL, _T.RUNS),
}

TABLE_CLASSES: dict[str, DataClass] = {name: pair[0] for name, pair in TABLES.items()}
TABLE_WRITERS: dict[str, Track] = {name: pair[1] for name, pair in TABLES.items()}

SYSTEM_KEYS: frozenset[str] = frozenset(
    {
        "attention.last_tick_at",
        "attention.last_wake_at",
        "memory.last_sweep_at",
        "memory.last_backup_at",
        "memory.last_orphan_scan_at",
        "memory.pending_deletions",
        "memory.over_budget",
        "writes.audit_checkpoint_hash",
        "writes.audit_head",
        "app.installed_version",
        "importer.last_import",
        "runs.update_tag",
        "runs.update_checked_at",
    }
)
"""The closed key set of `system_state` (M-2). Prefix is the writing track."""


def get_state(connection: sqlite3.Connection, key: str) -> str | None:
    _check_key(key)
    row = connection.execute("SELECT value FROM system_state WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row["value"])


def set_state(connection: sqlite3.Connection, clock: Clock, key: str, value: str) -> None:
    _check_key(key)
    connection.execute(
        "INSERT INTO system_state (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, value, stamp(clock)),
    )


def _check_key(key: str) -> None:
    if key not in SYSTEM_KEYS:
        raise KeyError(f"{key!r} is not a system_state key; add it to SYSTEM_KEYS (M-2)")
