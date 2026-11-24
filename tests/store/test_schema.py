"""The initial schema: docs/contracts/09-storage.md (S-2, S-6) and 06-memory.md (M-1, M-2)."""

import sqlite3

import pytest

from chamberlain.domain.capabilities import HealthState
from chamberlain.domain.events import EventState
from chamberlain.domain.grants import GrantState
from chamberlain.domain.operations import OperationState
from chamberlain.ports.clock import ManualClock
from chamberlain.store.db import table_names, transaction
from chamberlain.store.schema import (
    SYSTEM_KEYS,
    TABLE_CLASSES,
    TABLE_WRITERS,
    Track,
    get_state,
    set_state,
)

NOW = "2026-01-05T09:00:00Z"
SHA = "0" * 64


def test_s6_m1_every_table_has_a_data_class_and_none_is_spare(db: sqlite3.Connection) -> None:
    assert table_names(db) == set(TABLE_CLASSES)


def test_s3_every_table_has_exactly_one_writer() -> None:
    assert set(TABLE_WRITERS) == set(TABLE_CLASSES)
    shared = [name for name, track in TABLE_WRITERS.items() if track is Track.ANY]
    assert shared == ["system_state"]


def test_s2_every_table_is_strict(db: sqlite3.Connection) -> None:
    for row in db.execute("PRAGMA table_list"):
        if row["schema"] == "main" and row["name"] in TABLE_CLASSES:
            assert row["strict"] == 1, row["name"]


def test_m2_there_is_no_meta_table_and_system_state_keys_are_closed(
    db: sqlite3.Connection, clock: ManualClock
) -> None:
    assert "meta" not in table_names(db)
    key = "memory.last_sweep_at"
    assert key in SYSTEM_KEYS
    assert get_state(db, key) is None
    set_state(db, clock, key, "first")
    set_state(db, clock, key, "second")
    assert get_state(db, key) == "second"
    with pytest.raises(KeyError):
        set_state(db, clock, "journal:send:123", "anything")


def test_s2_state_columns_accept_exactly_the_domain_states(db: sqlite3.Connection) -> None:
    for state in OperationState:
        _insert_operation(db, f"op_{state.value}", state.value)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_operation(db, "op_bad", "retrying")
    for state in GrantState:
        _insert_grant(db, f"grant_{state.value}", state.value)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_grant(db, "grant_bad", "deleted")
    db.execute(
        "INSERT INTO sources (id, kind, connector, created_at, updated_at) "
        "VALUES ('mail:test', 'mail', 'fake', ?, ?)",
        (NOW, NOW),
    )
    for state in HealthState:
        db.execute(
            "INSERT OR REPLACE INTO source_health (source_id, state, checked_at) VALUES (?, ?, ?)",
            ("mail:test", state.value, NOW),
        )


def test_w15_idempotency_keys_are_unique(db: sqlite3.Connection) -> None:
    _insert_operation(db, "op_first", "proposed", key="same-key")
    with pytest.raises(sqlite3.IntegrityError):
        _insert_operation(db, "op_second", "proposed", key="same-key")


def test_a1_at_most_one_live_event_per_dedupe_key(db: sqlite3.Connection) -> None:
    _insert_event(db, "evt_1", EventState.NEW)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_event(db, "evt_2", EventState.DELIVERED)
    db.execute("UPDATE events SET state = 'handled' WHERE handle = 'evt_1'")
    _insert_event(db, "evt_3", EventState.NEW)


def test_m4_owner_rows_go_with_their_artifact(db: sqlite3.Connection) -> None:
    with transaction(db):
        db.execute(
            "INSERT INTO artifacts (handle, kind, media_type, size, sha256, reason, "
            "created_at, expires_at) VALUES ('art_1', 'text', 'text/plain', 3, ?, 'r', ?, ?)",
            (SHA, NOW, NOW),
        )
        db.execute(
            "INSERT INTO artifact_owners (artifact, owner_kind, owner, created_at) "
            "VALUES ('art_1', 'action', 'act_1', ?)",
            (NOW,),
        )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO artifact_owners (artifact, owner_kind, owner, created_at) "
            "VALUES ('art_missing', 'action', 'act_1', ?)",
            (NOW,),
        )
    db.execute("DELETE FROM artifacts WHERE handle = 'art_1'")
    assert db.execute("SELECT count(*) AS n FROM artifact_owners").fetchone()["n"] == 0


def test_session_scoped_rows_go_with_the_session(db: sqlite3.Connection) -> None:
    db.execute(
        "INSERT INTO sessions (handle, purpose, started_at, last_seen_at) "
        "VALUES ('ses_1', 'interactive', ?, ?)",
        (NOW, NOW),
    )
    db.execute(
        "INSERT INTO guidance_deliveries (session, rule_id, version, scope_key, delivered_at) "
        "VALUES ('ses_1', 'send.uncertain', 1, 'op_1', ?)",
        (NOW,),
    )
    db.execute("DELETE FROM sessions WHERE handle = 'ses_1'")
    assert db.execute("SELECT count(*) AS n FROM guidance_deliveries").fetchone()["n"] == 0


def _insert_operation(
    db: sqlite3.Connection, handle: str, state: str, key: str | None = None
) -> None:
    db.execute(
        "INSERT INTO operations (handle, idempotency_key, kind, source_id, state, "
        "intent_sha256, created_at, updated_at) "
        "VALUES (?, ?, 'mail.send', 'mail:test', ?, ?, ?, ?)",
        (handle, key or handle, state, SHA, NOW, NOW),
    )


def _insert_grant(db: sqlite3.Connection, handle: str, state: str) -> None:
    db.execute(
        "INSERT INTO grants (handle, kind, state, source_id, title, requirement, "
        "content_sha256, proposed_at) "
        "VALUES (?, 'one_off', ?, 'mail:test', 't', 'human_confirmation', ?, ?)",
        (handle, state, SHA, NOW),
    )


def _insert_event(db: sqlite3.Connection, handle: str, state: EventState) -> None:
    db.execute(
        "INSERT INTO events (handle, kind, subject, dedupe_key, priority, summary, state, "
        "detected_at) VALUES (?, 'action.due', 'act_1', 'action.due:act_1', 50, 's', ?, ?)",
        (handle, state.value, NOW),
    )
