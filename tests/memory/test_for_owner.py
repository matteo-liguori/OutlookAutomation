"""Actions that wait for the owner: docs/contracts/02-tools.md (`card`) and 09-storage.md."""

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from memory_helpers import SESSION

from chamberlain.app.core import App
from chamberlain.ports.clock import ManualClock
from chamberlain.store.db import DB_NAME, connect, transaction
from chamberlain.store.migrate import discover, migrate

ASK = "Diana (solicitor, 27 Nov 14:08) asks which Swiss university Giulia graduated from"


def call(app: App, **request: Any) -> dict[str, Any]:
    reply = app.call("card", {"session": SESSION, **request})
    return reply.model_dump(mode="json", exclude_none=True)


@pytest.mark.usefixtures("session")
def test_a_waiting_for_you_action_round_trips_and_finishes_with_the_answer(app: App) -> None:
    card = call(app, op="create", fields={"title": "Giulia's diploma"})["data"]["card"]
    added = call(
        app, op="add_action", card=card["handle"], action_fields={"title": ASK, "for_owner": True}
    )
    action = added["data"]["card"]["actions"][0]
    assert action["for_owner"] is True
    assert action["title"] == ASK
    plain = call(app, op="add_action", card=card["handle"], action_fields={"title": "File it"})
    assert plain["data"]["card"]["actions"][1]["for_owner"] is False

    renamed = call(
        app,
        op="update_action",
        action=action["handle"],
        action_fields={"title": ASK + " (asked again)"},
    )
    assert renamed["data"]["card"]["actions"][0]["for_owner"] is True

    refused = call(app, op="finish_action", action=action["handle"])
    assert refused["ok"] is False  # finishing always needs the owner's answer as the outcome
    finished = call(
        app, op="finish_action", action=action["handle"], outcome="Owner: University of Geneva."
    )
    assert finished["ok"] is True
    assert [a["title"] for a in finished["data"]["card"]["actions"]] == ["File it"]

    moved = call(
        app,
        op="update_action",
        action=plain["data"]["card"]["actions"][1]["handle"],
        action_fields={"for_owner": True},
    )
    assert moved["data"]["card"]["actions"][0]["for_owner"] is True


def test_the_migration_gives_existing_actions_for_owner_0(tmp_path: Path) -> None:
    clock = ManualClock()
    before = [m for m in discover() if m.version < 290]
    connection = connect(tmp_path / DB_NAME)
    try:
        migrate(connection, clock, backup_dir=tmp_path / "backups", migrations=before)
        with transaction(connection):
            connection.execute(
                "INSERT INTO cards (handle, title, created_at, updated_at) "
                "VALUES ('card_aaaaaaaa', 'Old', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
            )
            connection.execute(
                "INSERT INTO actions (handle, card, title, created_at, updated_at) VALUES "
                "('act_aaaaaaaa', 'card_aaaaaaaa', 'Old step', '2026-01-01T00:00:00Z', "
                "'2026-01-01T00:00:00Z')"
            )
        assert migrate(connection, clock, backup_dir=tmp_path / "backups") == [290]
        row = connection.execute("SELECT for_owner FROM actions").fetchone()
        assert row["for_owner"] == 0
        frozen = [r["name"] for r in connection.execute("PRAGMA table_info(run_reports)")]
        assert "frozen_at" in frozen
        with pytest.raises(sqlite3.IntegrityError), transaction(connection):
            connection.execute("UPDATE actions SET for_owner = 2")
    finally:
        connection.close()
