"""All SQL of the review track (S-4): its own four tables, and SELECTs on `events` and `runs`.

Contract: docs/contracts/09-storage.md (S-3, S-4), 06-memory.md (M-40).
"""

import json
import sqlite3
from dataclasses import dataclass

from chamberlain.review.tuning import PATTERNS_MAX


@dataclass(frozen=True)
class FileRow:
    byte_pos: int
    size: int
    qualified: bool


@dataclass(frozen=True)
class PatternRow:
    agent: str
    tool: str
    pattern: str
    total: int
    last_seen_at: str
    added_at: str


@dataclass(frozen=True)
class RoundRow:
    id: int
    state: str
    opened_at: str
    new_patterns: int


@dataclass(frozen=True)
class FindingRow:
    id: int
    kind: str
    text: str


# --- transcript positions ------------------------------------------------------------------


def get_file(db: sqlite3.Connection, key: str) -> FileRow | None:
    row = db.execute(
        "SELECT byte_pos, size, qualified FROM improv_files WHERE file_key = ?", (key,)
    ).fetchone()
    return None if row is None else FileRow(row["byte_pos"], row["size"], bool(row["qualified"]))


def put_file(
    db: sqlite3.Connection, key: str, pos: int, size: int, qualified: bool, now: str
) -> None:
    db.execute(
        "INSERT INTO improv_files (file_key, byte_pos, size, qualified, seen_at) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT (file_key) DO UPDATE SET byte_pos = excluded.byte_pos, "
        "size = excluded.size, qualified = excluded.qualified, seen_at = excluded.seen_at",
        (key, pos, size, int(qualified), now),
    )


def delete_files_before(db: sqlite3.Connection, edge: str) -> int:
    return db.execute("DELETE FROM improv_files WHERE seen_at < ?", (edge,)).rowcount


# --- patterns ------------------------------------------------------------------------------


def add_counts(
    db: sqlite3.Connection, rows: list[tuple[str, str, str, int, str, str]], now: str
) -> None:
    """Each row is `(agent, tool, pattern, count, first_seen, last_seen)`."""
    for agent, tool, pattern, count, first, last in rows:
        db.execute(
            "INSERT INTO improv_patterns (agent, tool, pattern, total, first_seen_at, "
            "last_seen_at, added_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (agent, tool, pattern) DO UPDATE SET total = total + excluded.total, "
            "first_seen_at = min(first_seen_at, excluded.first_seen_at), "
            "last_seen_at = max(last_seen_at, excluded.last_seen_at)",
            (agent, tool, pattern, count, first, last, now),
        )


def delete_patterns(db: sqlite3.Connection, seen_before: str) -> int:
    """Drop patterns not seen since `seen_before`, then the least recently seen over the cap."""
    removed = db.execute("DELETE FROM improv_patterns WHERE last_seen_at < ?", (seen_before,))
    over = db.execute(
        "DELETE FROM improv_patterns WHERE rowid IN (SELECT rowid FROM improv_patterns "
        "ORDER BY last_seen_at DESC, rowid LIMIT -1 OFFSET ?)",
        (PATTERNS_MAX,),
    )
    return removed.rowcount + over.rowcount


def patterns(db: sqlite3.Connection, added_since: str | None, limit: int) -> list[PatternRow]:
    """New patterns first, then the most used."""
    rows = db.execute(
        "SELECT agent, tool, pattern, total, last_seen_at, added_at FROM improv_patterns "
        "ORDER BY (added_at > ?) DESC, total DESC, last_seen_at DESC LIMIT ?",
        (added_since or "9999", limit),
    )
    return [PatternRow(*(r[i] for i in range(6))) for r in rows]


def count_patterns(db: sqlite3.Connection) -> int:
    return int(db.execute("SELECT count(*) FROM improv_patterns").fetchone()[0])


def count_patterns_added_after(db: sqlite3.Connection, edge: str | None) -> int:
    if edge is None:
        return count_patterns(db)
    row = db.execute("SELECT count(*) FROM improv_patterns WHERE added_at > ?", (edge,))
    return int(row.fetchone()[0])


def first_added(db: sqlite3.Connection) -> str | None:
    value = db.execute("SELECT min(added_at) FROM improv_patterns").fetchone()[0]
    return None if value is None else str(value)


# --- rounds and findings -------------------------------------------------------------------


def _round(row: sqlite3.Row) -> RoundRow:
    return RoundRow(row["id"], row["state"], row["opened_at"], row["new_patterns"])


def latest_round(db: sqlite3.Connection) -> RoundRow | None:
    row = db.execute(
        "SELECT id, state, opened_at, new_patterns FROM review_rounds ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return None if row is None else _round(row)


def previous_round_opened(db: sqlite3.Connection, before: int) -> str | None:
    row = db.execute(
        "SELECT opened_at FROM review_rounds WHERE id < ? ORDER BY id DESC LIMIT 1", (before,)
    ).fetchone()
    return None if row is None else str(row["opened_at"])


def open_rounds(db: sqlite3.Connection) -> list[RoundRow]:
    rows = db.execute(
        "SELECT id, state, opened_at, new_patterns FROM review_rounds WHERE state = 'open' "
        "ORDER BY id"
    )
    return [_round(r) for r in rows]


def get_round(db: sqlite3.Connection, round_id: int) -> RoundRow | None:
    row = db.execute(
        "SELECT id, state, opened_at, new_patterns FROM review_rounds WHERE id = ?", (round_id,)
    ).fetchone()
    return None if row is None else _round(row)


def insert_round(db: sqlite3.Connection, opened_at: str, new_patterns: int) -> int:
    cursor = db.execute(
        "INSERT INTO review_rounds (state, opened_at, new_patterns) VALUES ('open', ?, ?)",
        (opened_at, new_patterns),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def close_round(db: sqlite3.Connection, round_id: int, state: str, now: str) -> None:
    db.execute(
        "UPDATE review_rounds SET state = ?, closed_at = ? WHERE id = ? AND state = 'open'",
        (state, now, round_id),
    )


def delete_rounds_before(db: sqlite3.Connection, edge: str) -> int:
    return db.execute(
        "DELETE FROM review_rounds WHERE opened_at < ? AND state != 'open'", (edge,)
    ).rowcount


def event_state(db: sqlite3.Connection, dedupe_key: str) -> str | None:
    """The newest `events` row for the round's key (a SELECT on attention's table)."""
    row = db.execute(
        "SELECT state FROM events WHERE dedupe_key = ? ORDER BY detected_at DESC LIMIT 1",
        (dedupe_key,),
    ).fetchone()
    return None if row is None else str(row["state"])


def event_identity(db: sqlite3.Connection, handle: str) -> tuple[str, str] | None:
    """`(kind, dedupe_key)` of an event."""
    row = db.execute("SELECT kind, dedupe_key FROM events WHERE handle = ?", (handle,)).fetchone()
    return None if row is None else (str(row["kind"]), str(row["dedupe_key"]))


def count_findings(db: sqlite3.Connection, round_id: int) -> int:
    row = db.execute("SELECT count(*) FROM review_findings WHERE round = ?", (round_id,))
    return int(row.fetchone()[0])


def add_finding(db: sqlite3.Connection, round_id: int, kind: str, text: str, now: str) -> None:
    db.execute(
        "INSERT INTO review_findings (round, kind, text, created_at) VALUES (?, ?, ?, ?)",
        (round_id, kind, text, now),
    )


def unreported_worrying(db: sqlite3.Connection) -> list[FindingRow]:
    rows = db.execute(
        "SELECT id, kind, text FROM review_findings WHERE kind = 'worrying' "
        "AND reported_at IS NULL ORDER BY id"
    )
    return [FindingRow(r["id"], r["kind"], r["text"]) for r in rows]


def mark_reported(db: sqlite3.Connection, ids: list[int], now: str) -> None:
    db.execute(
        "UPDATE review_findings SET reported_at = ? WHERE reported_at IS NULL "
        "AND id IN (SELECT value FROM json_each(?))",
        (now, json.dumps(ids)),
    )


# --- the pass that just ended (a SELECT on the runs track's table) -------------------------


def pass_finished_since(db: sqlite3.Connection, edge: str) -> bool:
    row = db.execute(
        "SELECT 1 FROM runs WHERE state IN ('finished', 'incomplete') AND finished_at > ? LIMIT 1",
        (edge,),
    ).fetchone()
    return row is not None
