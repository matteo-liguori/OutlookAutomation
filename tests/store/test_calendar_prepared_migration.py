"""One-time conversion of in-flight calendar plans from the pre-R3 store."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from tests.store.legacy import legacy_migrations

from chamberlain.connectors.calendar_support.port import expected_etag, fields, locator, payload
from chamberlain.domain.calendar import EventFields
from chamberlain.domain.operations import Prepared
from chamberlain.ports.clock import ManualClock
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.store.db import MEMORY, connect
from chamberlain.store.migrate import migrate

NOW = "2026-11-25T12:00:00Z"
EVENT = EventFields(
    title="private",
    start=datetime(2027, 1, 1, tzinfo=UTC),
    end=datetime(2027, 1, 1, tzinfo=UTC) + timedelta(hours=1),
    visibility="private",
).model_dump(mode="json")


def insert_source(db, source: str, connector: str) -> None:
    db.execute(
        "INSERT INTO sources (id,kind,connector,created_at,updated_at) "
        "VALUES (?, 'calendar', ?, ?, ?)",
        (source, connector, NOW, NOW),
    )


def insert_operation(
    db, name: str, source: str, kind: str, data: dict, state: str = "uncertain"
) -> str:
    uid = f"uid-{name}" if kind == "calendar.create" else None
    doc = {"source": source, "intent_kind": kind, "data": data}
    if uid:
        doc["reconcile_token"] = uid
    raw = json.dumps(doc)
    db.execute(
        "INSERT INTO operations (handle,idempotency_key,kind,source_id,state,intent_sha256,"
        "prepared_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?, ?,?)",
        (name, f"key-{name}", kind, source, state, "0" * 64, raw, NOW, NOW),
    )
    return raw


def read_prepared(db, name: str) -> Prepared:
    row = db.execute("SELECT prepared_json FROM operations WHERE handle=?", (name,)).fetchone()
    return Prepared.model_validate_json(row["prepared_json"])


def test_old_inflight_plans_convert_once_without_changing_facts_or_terminal_rows() -> None:
    db, clock = connect(MEMORY), ManualClock()
    try:
        migrate(
            db,
            clock,
            backup_dir=None,
            migrations=[m for m in legacy_migrations() if m.version < 260],
        )
        for source, connector in (
            ("calendar:g", "google_calendar"),
            ("calendar:d", "caldav"),
            ("calendar:o", "thunderbird"),
            ("calendar:f", "demo"),
        ):
            insert_source(db, source, connector)
        snapshots = {
            "gc": (
                "calendar:g",
                "calendar.create",
                {"body": {"iCalUID": "uid-gc"}, "fields": EVENT},
            ),
            "gi": (
                "calendar:g",
                "calendar.create",
                {"body": {"id": "chosen"}, "event_id": "chosen", "notify": True, "fields": EVENT},
            ),
            "gu": (
                "calendar:g",
                "calendar.update",
                {
                    "locator": "google-id",
                    "expected_etag": "tag-g",
                    "body": {"summary": "x"},
                    "notify": False,
                    "fields": EVENT,
                },
            ),
            "gd": (
                "calendar:g",
                "calendar.delete",
                {"locator": "google-id", "expected_etag": "tag-g", "notify": True},
            ),
            "gr": (
                "calendar:g",
                "calendar.respond",
                {
                    "locator": "google-id",
                    "expected_etag": "tag-g",
                    "response": "accepted",
                    "attendees": [{"self": True, "responseStatus": "accepted"}],
                },
            ),
            "dc": (
                "calendar:d",
                "calendar.create",
                {"name": "abc.ics", "ics": "create-data", "fields": EVENT, "guests": False},
            ),
            "du": (
                "calendar:d",
                "calendar.update",
                {
                    "name": "abc.ics",
                    "occurrence": "20270101T000000Z",
                    "expected_etag": "tag-d",
                    "ics": "update-data",
                    "fields": EVENT,
                },
            ),
            "dd": (
                "calendar:d",
                "calendar.delete",
                {
                    "name": "abc.ics",
                    "occurrence": "20270101T000000Z",
                    "expected_etag": "tag-d",
                    "ics": "delete-data",
                },
            ),
            "dr": (
                "calendar:d",
                "calendar.respond",
                {
                    "name": "abc.ics",
                    "expected_etag": "tag-d",
                    "ics": "respond-data",
                    "response": "accepted",
                    "address": "me@example.org",
                },
            ),
            "oc": (
                "calendar:o",
                "calendar.create",
                {"event_id": "owl-created", "icalendar": "create-ics", "fields": EVENT},
            ),
            "ou": (
                "calendar:o",
                "calendar.update",
                {
                    "event_id": "owl-actual",
                    "expected_icalendar": "old-ics",
                    "expected_etag": "tag-o",
                    "icalendar": "new-ics",
                    "fields": EVENT,
                },
            ),
            "od": (
                "calendar:o",
                "calendar.delete",
                {
                    "event_id": "owl-actual",
                    "expected_icalendar": "old-ics",
                    "expected_etag": "tag-o",
                },
            ),
        }
        for i, (name, (source, kind, data)) in enumerate(snapshots.items()):
            insert_operation(
                db,
                name,
                source,
                kind,
                data,
                ("prepared", "dispatched", "uncertain", "reconciling")[i % 4],
            )
        old_terminal = insert_operation(
            db,
            "terminal",
            "calendar:g",
            "calendar.delete",
            {"locator": "done", "expected_etag": "tag"},
            "confirmed",
        )
        old_fake = insert_operation(
            db,
            "fake",
            "calendar:f",
            "calendar.update",
            {"locator": "fake", "expected_etag": "tag", "fields": EVENT},
        )
        assert 260 in migrate(db, clock, backup_dir=None)
        for name, (source, kind, data) in snapshots.items():
            p = read_prepared(db, name)
            assert p.source == source
            assert p.intent_kind.value == kind
            if kind == "calendar.create":
                assert p.reconcile_token == f"uid-{name}"
            else:
                assert expected_etag(p) == data["expected_etag"]
            if "fields" in data:
                assert fields(p).model_dump(mode="json") == data["fields"]
        assert payload(read_prepared(db, "gr")) == {
            "response": "accepted",
            "body": {"attendees": [{"self": True, "responseStatus": "accepted"}]},
            "notify": True,
        }
        assert payload(read_prepared(db, "gi"))["event_id"] == "chosen"
        assert locator(read_prepared(db, "du")) == "abc.ics#20270101T000000Z"
        assert payload(read_prepared(db, "dd"))["ics"] == "delete-data"
        assert locator(read_prepared(db, "ou")) == "owl-actual"
        assert payload(read_prepared(db, "ou")) == {
            "event_id": "owl-actual",
            "expected_icalendar": "old-ics",
            "icalendar": "new-ics",
            "kind": "update",
        }
        assert payload(read_prepared(db, "oc"))["kind"] == "create"
        assert payload(read_prepared(db, "od"))["kind"] == "delete"
        assert (
            db.execute("SELECT prepared_json FROM operations WHERE handle='terminal'").fetchone()[0]
            == old_terminal
        )
        assert (
            db.execute("SELECT prepared_json FROM operations WHERE handle='fake'").fetchone()[0]
            == old_fake
        )
        assert migrate(db, clock, backup_dir=None) == []
    finally:
        db.close()


@pytest.mark.parametrize(
    "data",
    [{"fields": EVENT}, {"event_id": "x", "expected_etag": "v", "expected_icalendar": "old"}],
)
def test_migration_refuses_to_guess_missing_identity_or_body(data: dict) -> None:
    db, clock = connect(MEMORY), ManualClock()
    try:
        migrate(
            db,
            clock,
            backup_dir=None,
            migrations=[m for m in legacy_migrations() if m.version < 260],
        )
        insert_source(db, "calendar:o", "thunderbird")
        valid = insert_operation(
            db,
            "valid",
            "calendar:o",
            "calendar.create",
            {"event_id": "valid-id", "icalendar": "valid-ics", "fields": EVENT},
        )
        original = insert_operation(db, "bad", "calendar:o", "calendar.update", data)
        with pytest.raises(ChamberlainError) as raised:
            migrate(db, clock, backup_dir=None)
        assert raised.value.code is ErrorCode.STORE_MIGRATION_FAILED
        assert (
            db.execute("SELECT prepared_json FROM operations WHERE handle='bad'").fetchone()[0]
            == original
        )
        assert (
            db.execute("SELECT prepared_json FROM operations WHERE handle='valid'").fetchone()[0]
            == valid
        )
        assert (
            db.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=260").fetchone()[0]
            == 0
        )
    finally:
        db.close()
