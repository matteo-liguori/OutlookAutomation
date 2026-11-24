# pyright: reportAttributeAccessIssue=false
"""Fixtures for the attention tests: a seeded store and an app with the attention hooks."""

import sqlite3
from datetime import timedelta

import pytest

from chamberlain.app.context import Context
from chamberlain.app.core import App
from chamberlain.attention.handlers import HANDLERS, HOOKS
from chamberlain.ports.clock import ManualClock

SHA = "a" * 64


class Seed:
    """Writes rows that other tracks own, the way their code would (tests only)."""

    def __init__(self, db: sqlite3.Connection, clock: ManualClock) -> None:
        self.db = db
        self.clock = clock
        self.n = 0

    def ts(self, **delta: float) -> str:
        return (self.clock.now() + timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _id(self, prefix: str) -> str:
        self.n += 1
        return f"{prefix}_{self.n:016x}"

    def source(self, source_id: str) -> None:
        now = self.ts()
        self.db.execute(
            "INSERT OR IGNORE INTO sources (id, kind, connector, created_at, updated_at) "
            "VALUES (?, ?, 'fake', ?, ?)",
            (source_id, source_id.split(":")[0], now, now),
        )

    def card(
        self, title: str = "A card", *, review_at: str | None = None, over_cap: int = 0
    ) -> str:
        handle = self._id("card")
        now = self.ts()
        self.db.execute(
            "INSERT INTO cards (handle, title, review_at, over_cap, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (handle, title, review_at, over_cap, now, now),
        )
        return handle

    def close_card(self, handle: str) -> None:
        self.db.execute("UPDATE cards SET state = 'closed' WHERE handle = ?", (handle,))

    def action(
        self, card: str, *, due_at: str | None = None, snooze_until: str | None = None
    ) -> str:
        handle = self._id("act")
        now = self.ts()
        self.db.execute(
            "INSERT INTO actions (handle, card, title, due_at, snooze_until, created_at, "
            "updated_at) VALUES (?, ?, 'Do it', ?, ?, ?, ?)",
            (handle, card, due_at, snooze_until, now, now),
        )
        return handle

    def finish_action(self, handle: str) -> None:
        self.db.execute("UPDATE actions SET state = 'done' WHERE handle = ?", (handle,))

    def grant(
        self,
        *,
        state: str = "proposed",
        kind: str = "standing",
        requirement: str = "human_confirmation",
        review_at: str | None = None,
    ) -> str:
        handle = self._id("grant")
        self.db.execute(
            "INSERT INTO grants (handle, kind, state, source_id, title, requirement, "
            "content_sha256, proposed_at, review_at) "
            "VALUES (?, ?, ?, 'mail:test', 'g', ?, ?, ?, ?)",
            (handle, kind, state, requirement, SHA, self.ts(), review_at),
        )
        return handle

    def set_grant_state(self, handle: str, state: str) -> None:
        self.db.execute("UPDATE grants SET state = ? WHERE handle = ?", (state, handle))

    def operation(self, kind: str = "mail.send", state: str = "uncertain") -> str:
        handle = self._id("op")
        now = self.ts()
        self.db.execute(
            "INSERT INTO operations (handle, idempotency_key, kind, source_id, state, "
            "intent_sha256, created_at, updated_at) VALUES (?, ?, ?, 'mail:test', ?, ?, ?, ?)",
            (handle, handle, kind, state, SHA, now, now),
        )
        return handle

    def set_operation_state(self, handle: str, state: str) -> None:
        self.db.execute("UPDATE operations SET state = ? WHERE handle = ?", (state, handle))

    def system(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO system_state (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value, self.ts()),
        )

    def orphans(self, found_at: str, count: int = 1) -> None:
        for i in range(count):
            self.db.execute(
                "INSERT INTO quarantine (original_path, quarantined_path, size, found_at, "
                "delete_after) VALUES (?, ?, 1, ?, ?)",
                (f"o{i}", f"q{i}", found_at, found_at),
            )


@pytest.fixture
def seed(ctx: Context, clock: ManualClock) -> Seed:
    s = Seed(ctx.db, clock)
    for source in ("mail:test", "calendar:test", "contacts:test"):
        s.source(source)
    return s


@pytest.fixture
def app(ctx: Context, seed: Seed) -> App:
    return App(ctx, HANDLERS, HOOKS)
