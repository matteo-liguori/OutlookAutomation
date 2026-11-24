"""All SQL of the memory track (S-4). Nothing else in the track writes a statement.

Contract: docs/contracts/09-storage.md. Tables written here: cards, actions,
card_links, notes, evidence, evidence_refs, artifacts, artifact_owners, refs,
leases, cursors, quarantine. Tables of other tracks are only read (S-3).
Callers wrap writes in `store.db.transaction`; timestamps arrive as D-7 strings.
"""

import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

Row = sqlite3.Row

# --- liveness of owners, read from the owning tracks' tables (M-4, M-5) -------

LIVE_ACTION = "SELECT handle FROM actions WHERE state = 'open'"
LIVE_OPERATION = (
    "SELECT handle FROM operations WHERE "
    "state NOT IN ('confirmed', 'failed_clean', 'confirmed_absent', 'cancelled', 'abandoned', "
    "'superseded') "
    "OR COALESCE(settled_at, updated_at) > :grace_edge"
)
LIVE_GRANT = "SELECT handle FROM grants WHERE state IN ('proposed', 'active', 'suspended')"
LIVE_SESSION = "SELECT handle FROM sessions WHERE ended_at IS NULL AND last_seen_at > :idle_edge"

LIVE_RUN = "SELECT handle FROM runs"
LIVE_CARD = "SELECT handle FROM cards WHERE state = 'open'"

_LIVE_BY_KIND = {
    "action": LIVE_ACTION,
    "operation": LIVE_OPERATION,
    "grant": LIVE_GRANT,
    "session": LIVE_SESSION,
    "run": LIVE_RUN,
    "card": LIVE_CARD,
}


@dataclass(frozen=True)
class Edges:
    """The two instants liveness is measured against."""

    grace_edge: str
    """Terminal operations settled after this are still live (M-4)."""
    idle_edge: str
    """Sessions last seen before this are idle (4 hours)."""


def owner_is_live(db: sqlite3.Connection, kind: str, handle: str, edges: Edges) -> bool:
    """Whether `handle` is a live owner of `kind` (action, operation, grant, session)."""
    sql = _LIVE_BY_KIND.get(kind)
    if sql is None:
        return False
    params = {"handle": handle, "grace_edge": edges.grace_edge, "idle_edge": edges.idle_edge}
    row = db.execute(f"SELECT 1 FROM ({sql}) WHERE handle = :handle", params).fetchone()
    return row is not None


def owner_exists(db: sqlite3.Connection, kind: str, handle: str) -> bool:
    table = {
        "action": "actions",
        "operation": "operations",
        "grant": "grants",
        "session": "sessions",
        "run": "runs",
        "card": "cards",
    }[kind]
    return db.execute(f"SELECT 1 FROM {table} WHERE handle = ?", (handle,)).fetchone() is not None


# --- cards -----------------------------------------------------------------


def insert_card(
    db: sqlite3.Connection,
    *,
    handle: str,
    title: str,
    goal: str,
    position: str,
    rules: str,
    over_cap: bool,
    review_at: str | None,
    now: str,
) -> None:
    db.execute(
        "INSERT INTO cards (handle, title, goal, position, rules, over_cap, review_at, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (handle, title, goal, position, rules, int(over_cap), review_at, now, now),
    )


def get_card(db: sqlite3.Connection, handle: str) -> Row | None:
    row: Row | None = db.execute("SELECT * FROM cards WHERE handle = ?", (handle,)).fetchone()
    return row


def update_card(
    db: sqlite3.Connection,
    *,
    handle: str,
    title: str,
    goal: str,
    position: str,
    rules: str,
    over_cap: bool,
    review_at: str | None,
    now: str,
) -> None:
    db.execute(
        "UPDATE cards SET title = ?, goal = ?, position = ?, rules = ?, over_cap = ?, "
        "review_at = ?, version = version + 1, updated_at = ? WHERE handle = ?",
        (title, goal, position, rules, int(over_cap), review_at, now, handle),
    )


def bump_card(db: sqlite3.Connection, handle: str, now: str) -> None:
    db.execute(
        "UPDATE cards SET version = version + 1, updated_at = ? WHERE handle = ?", (now, handle)
    )


def close_card(db: sqlite3.Connection, handle: str, outcome: str, now: str, purge_at: str) -> None:
    """Compact to the outcome line: position, goal and rules are erased (lifetime table)."""
    db.execute(
        "UPDATE cards SET state = 'closed', goal = '', position = '', rules = '', over_cap = 0, "
        "review_at = NULL, outcome = ?, closed_at = ?, purge_at = ?, "
        "version = version + 1, updated_at = ? WHERE handle = ?",
        (outcome, now, purge_at, now, handle),
    )


def reopen_card(db: sqlite3.Connection, handle: str, now: str) -> None:
    db.execute(
        "UPDATE cards SET state = 'open', outcome = NULL, closed_at = NULL, purge_at = NULL, "
        "version = version + 1, updated_at = ? WHERE handle = ?",
        (now, handle),
    )


def set_card_review(db: sqlite3.Connection, handle: str, review_at: str, over_cap: bool) -> None:
    """Make a card due for review now (or flag it over cap) without changing its content."""
    db.execute(
        "UPDATE cards SET review_at = ?, over_cap = ? WHERE handle = ? AND state = 'open'",
        (review_at, int(over_cap), handle),
    )


# --- context carried over from the old harness (M-35) ----------------------------


def imported_context_done(db: sqlite3.Connection, case_id: str) -> bool:
    row = db.execute("SELECT 1 FROM imported_context WHERE case_id = ?", (case_id,)).fetchone()
    return row is not None


def record_imported_context(
    db: sqlite3.Connection, case_id: str, card: str, artifact: str, now: str
) -> None:
    db.execute(
        "INSERT OR IGNORE INTO imported_context (case_id, card, artifact, imported_at) "
        "VALUES (?, ?, ?, ?)",
        (case_id, card, artifact, now),
    )


def card_context_artifact(db: sqlite3.Connection, card: str, now: str) -> str | None:
    """The live, unexpired imported-context item a card holds, if any."""
    row = db.execute(
        "SELECT a.handle FROM artifacts a JOIN artifact_owners o ON o.artifact = a.handle "
        "JOIN imported_context i ON i.artifact = a.handle "
        "WHERE o.owner_kind = 'card' AND o.owner = ? AND a.state = 'live' AND a.expires_at > ? "
        "ORDER BY a.created_at DESC, a.handle LIMIT 1",
        (card, now),
    ).fetchone()
    return None if row is None else str(row["handle"])


# --- larger limits for complex cards (M-36) -------------------------------------------


def get_complex(db: sqlite3.Connection, card: str) -> Row | None:
    row: Row | None = db.execute("SELECT * FROM complex_cards WHERE card = ?", (card,)).fetchone()
    return row


def complex_rows(db: sqlite3.Connection, state: str) -> list[Row]:
    rows: list[Row] = db.execute(
        "SELECT x.*, c.title AS title FROM complex_cards x JOIN cards c ON c.handle = x.card "
        "WHERE x.state = ? AND c.state = 'open' ORDER BY x.requested_at, x.card",
        (state,),
    ).fetchall()
    return rows


def set_complex(
    db: sqlite3.Connection,
    *,
    card: str,
    state: str,
    reason: str,
    requested_at: str,
    decided_at: str | None,
    blocked_until: str | None,
) -> None:
    db.execute(
        "INSERT INTO complex_cards (card, state, reason, requested_at, decided_at, blocked_until) "
        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (card) DO UPDATE SET state = excluded.state, "
        "reason = excluded.reason, requested_at = excluded.requested_at, "
        "decided_at = excluded.decided_at, blocked_until = excluded.blocked_until",
        (card, state, reason, requested_at, decided_at, blocked_until),
    )


def delete_complex(db: sqlite3.Connection, card: str) -> None:
    db.execute("DELETE FROM complex_cards WHERE card = ?", (card,))


def delete_card_links(db: sqlite3.Connection, card: str) -> None:
    db.execute("DELETE FROM card_links WHERE card = ?", (card,))


def cards_for_ref(db: sqlite3.Connection, ref: str) -> list[str]:
    rows = db.execute("SELECT card FROM card_links WHERE ref = ? ORDER BY created_at", (ref,))
    return [str(row["card"]) for row in rows]


def link_card(
    db: sqlite3.Connection, card: str, ref: str, internet_message_id: str | None, now: str
) -> None:
    db.execute(
        "INSERT OR IGNORE INTO card_links (card, ref, internet_message_id, created_at) "
        "VALUES (?, ?, ?, ?)",
        (card, ref, internet_message_id, now),
    )


def list_cards(db: sqlite3.Connection, text: str | None, limit: int) -> list[Row]:
    pattern = f"%{_like(text)}%" if text else "%"
    rows: list[Row] = db.execute(
        "SELECT * FROM cards WHERE title LIKE ? ESCAPE '\\' OR goal LIKE ? ESCAPE '\\' "
        "ORDER BY state = 'open' DESC, updated_at DESC, handle LIMIT ?",
        (pattern, pattern, limit),
    ).fetchall()
    return rows


def purge_closed_cards(db: sqlite3.Connection, now: str) -> int:
    return db.execute(
        "DELETE FROM cards WHERE state = 'closed' AND purge_at IS NOT NULL AND purge_at <= ?",
        (now,),
    ).rowcount


# --- actions and notes -------------------------------------------------------


def insert_action(
    db: sqlite3.Connection,
    *,
    handle: str,
    card: str,
    title: str,
    due_at: str | None,
    snooze_until: str | None,
    now: str,
    important: bool = False,
) -> None:
    db.execute(
        "INSERT INTO actions (handle, card, title, due_at, snooze_until, important, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (handle, card, title, due_at, snooze_until, int(important), now, now),
    )


def get_action(db: sqlite3.Connection, handle: str) -> Row | None:
    row: Row | None = db.execute("SELECT * FROM actions WHERE handle = ?", (handle,)).fetchone()
    return row


def open_actions(db: sqlite3.Connection, card: str) -> list[Row]:
    rows: list[Row] = db.execute(
        "SELECT * FROM actions WHERE card = ? AND state = 'open' ORDER BY created_at, handle",
        (card,),
    ).fetchall()
    return rows


def action_handles(db: sqlite3.Connection, card: str, open_only: bool) -> list[str]:
    sql = "SELECT handle FROM actions WHERE card = ?"
    if open_only:
        sql += " AND state = 'open'"
    return [str(row["handle"]) for row in db.execute(sql, (card,))]


def update_action(
    db: sqlite3.Connection,
    *,
    handle: str,
    title: str,
    due_at: str | None,
    snooze_until: str | None,
    now: str,
    important: bool | None = None,
) -> None:
    db.execute(
        "UPDATE actions SET title = ?, due_at = ?, snooze_until = ?, "
        "important = coalesce(?, important), updated_at = ? WHERE handle = ?",
        (title, due_at, snooze_until, None if important is None else int(important), now, handle),
    )


def end_action(
    db: sqlite3.Connection, handle: str, state: str, outcome: str, now: str, purge_at: str
) -> None:
    """Finish or drop: state, outcome line and purge date (lifetime table)."""
    db.execute(
        "UPDATE actions SET state = ?, outcome = ?, closed_at = ?, purge_at = ?, "
        "due_at = NULL, snooze_until = NULL, updated_at = ? WHERE handle = ?",
        (state, outcome, now, purge_at, now, handle),
    )


def purge_ended_actions(db: sqlite3.Connection, now: str) -> int:
    return db.execute(
        "DELETE FROM actions WHERE state != 'open' AND purge_at IS NOT NULL AND purge_at <= ?",
        (now,),
    ).rowcount


def insert_note(db: sqlite3.Connection, handle: str, action: str, text: str, now: str) -> None:
    db.execute(
        "INSERT INTO notes (handle, action, text, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (handle, action, text, now, now),
    )


def get_note(db: sqlite3.Connection, handle: str) -> Row | None:
    row: Row | None = db.execute("SELECT * FROM notes WHERE handle = ?", (handle,)).fetchone()
    return row


def notes_of(db: sqlite3.Connection, action: str) -> list[Row]:
    rows: list[Row] = db.execute(
        "SELECT * FROM notes WHERE action = ? ORDER BY created_at, handle", (action,)
    ).fetchall()
    return rows


def count_notes(db: sqlite3.Connection, action: str) -> int:
    sql = "SELECT count(*) FROM notes WHERE action = ?"
    return scalar(db, sql, (action,))


def delete_note(db: sqlite3.Connection, handle: str) -> int:
    return db.execute("DELETE FROM notes WHERE handle = ?", (handle,)).rowcount


def delete_notes_of(db: sqlite3.Connection, action: str) -> int:
    return db.execute("DELETE FROM notes WHERE action = ?", (action,)).rowcount


def delete_idle_notes(db: sqlite3.Connection, idle_edge: str) -> int:
    """Hard cap: a note untouched for 30 days goes even if its action is open."""
    return db.execute("DELETE FROM notes WHERE updated_at <= ?", (idle_edge,)).rowcount


def delete_notes_of_ended_actions(db: sqlite3.Connection) -> int:
    return db.execute(
        "DELETE FROM notes WHERE action IN (SELECT handle FROM actions WHERE state != 'open')"
    ).rowcount


def search_notes(db: sqlite3.Connection, text: str | None, limit: int) -> list[Row]:
    rows: list[Row] = db.execute(
        "SELECT * FROM notes WHERE text LIKE ? ESCAPE '\\' "
        "ORDER BY created_at DESC, handle LIMIT ?",
        (f"%{_like(text)}%" if text else "%", limit),
    ).fetchall()
    return rows


def mail_suggestions(
    db: sqlite3.Connection, source: str, keys: list[str]
) -> dict[str, tuple[str, str]]:
    """Message key to (open card handle, title), read from the attention track's batch rows.

    One row stands for every copy of a message, so the key (`domain.mail.message_key`), not a
    handle, is how a listing finds it.
    """
    found: dict[str, tuple[str, str]] = {}
    for start in range(0, len(keys), 200):
        chunk = keys[start : start + 200]
        marks = ",".join("?" * len(chunk))
        rows = db.execute(
            "SELECT s.message_key AS message, c.handle AS card, c.title AS title FROM mail_seen s "
            "JOIN cards c ON c.handle = s.suggested_card AND c.state = 'open' "
            f"WHERE s.source_id = ? AND s.message_key IN ({marks})",
            (source, *chunk),
        )
        for row in rows:
            found[str(row["message"])] = (str(row["card"]), str(row["title"]))
    return found


# --- evidence ----------------------------------------------------------------


def insert_evidence(
    db: sqlite3.Connection,
    *,
    handle: str,
    text: str,
    ref: str | None,
    subject_sha256: str | None,
    session: str | None,
    now: str,
) -> None:
    db.execute(
        "INSERT INTO evidence (handle, text, ref, subject_sha256, session, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (handle, text, ref, subject_sha256, session, now),
    )


def get_evidence(db: sqlite3.Connection, handle: str) -> Row | None:
    row: Row | None = db.execute("SELECT * FROM evidence WHERE handle = ?", (handle,)).fetchone()
    return row


def add_evidence_ref(
    db: sqlite3.Connection, evidence: str, owner_kind: str, owner: str, now: str
) -> None:
    db.execute(
        "INSERT OR IGNORE INTO evidence_refs (evidence, owner_kind, owner, created_at) "
        "VALUES (?, ?, ?, ?)",
        (evidence, owner_kind, owner, now),
    )


def evidence_referrers(db: sqlite3.Connection, evidence: str) -> int:
    return scalar(db, "SELECT count(*) FROM evidence_refs WHERE evidence = ?", (evidence,))


def remove_evidence_ref(db: sqlite3.Connection, evidence: str, owner: str | None) -> int:
    if owner is None:
        return db.execute("DELETE FROM evidence_refs WHERE evidence = ?", (evidence,)).rowcount
    return db.execute(
        "DELETE FROM evidence_refs WHERE evidence = ? AND owner = ?", (evidence, owner)
    ).rowcount


def remove_evidence_refs_of(db: sqlite3.Connection, owners: Sequence[str]) -> int:
    count = 0
    for owner in owners:
        count += db.execute("DELETE FROM evidence_refs WHERE owner = ?", (owner,)).rowcount
    return count


def delete_unreferenced_evidence(db: sqlite3.Connection) -> int:
    return db.execute(
        "DELETE FROM evidence WHERE handle NOT IN (SELECT evidence FROM evidence_refs)"
    ).rowcount


def delete_dead_evidence_refs(db: sqlite3.Connection) -> int:
    """Referrers that are gone or compacted no longer hold evidence (M-15)."""
    live = (
        "(owner_kind = 'card' AND owner IN (SELECT handle FROM cards WHERE state = 'open')) "
        "OR (owner_kind = 'action' AND owner IN (SELECT handle FROM actions WHERE state = 'open')) "
        "OR (owner_kind = 'grant' AND owner IN "
        "(SELECT handle FROM grants WHERE compacted_at IS NULL)) "
        "OR (owner_kind = 'operation' AND owner IN "
        "(SELECT handle FROM operations WHERE compacted_at IS NULL))"
    )
    return db.execute(f"DELETE FROM evidence_refs WHERE NOT ({live})").rowcount


def count_evidence_for_card(db: sqlite3.Connection, card: str) -> int:
    return scalar(
        db,
        "SELECT count(DISTINCT evidence) FROM evidence_refs WHERE owner = ? OR owner IN "
        "(SELECT handle FROM actions WHERE card = ?)",
        (card, card),
    )


def search_evidence(db: sqlite3.Connection, text: str | None, limit: int) -> list[Row]:
    rows: list[Row] = db.execute(
        "SELECT * FROM evidence WHERE text LIKE ? ESCAPE '\\' "
        "ORDER BY created_at DESC, handle LIMIT ?",
        (f"%{_like(text)}%" if text else "%", limit),
    ).fetchall()
    return rows


# --- artifacts and owners ------------------------------------------------------


def insert_artifact(
    db: sqlite3.Connection,
    *,
    handle: str,
    kind: str,
    media_type: str,
    size: int,
    sha256: str,
    filename: str | None,
    reason: str,
    now: str,
    expires_at: str,
) -> None:
    db.execute(
        "INSERT INTO artifacts (handle, kind, media_type, size, sha256, filename, reason, "
        "created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (handle, kind, media_type, size, sha256, filename, reason, now, expires_at),
    )


def get_artifact(db: sqlite3.Connection, handle: str) -> Row | None:
    row: Row | None = db.execute("SELECT * FROM artifacts WHERE handle = ?", (handle,)).fetchone()
    return row


def add_artifact_owner(
    db: sqlite3.Connection, artifact: str, owner_kind: str, owner: str, now: str
) -> None:
    db.execute(
        "INSERT OR IGNORE INTO artifact_owners (artifact, owner_kind, owner, created_at) "
        "VALUES (?, ?, ?, ?)",
        (artifact, owner_kind, owner, now),
    )


def artifact_owners(db: sqlite3.Connection, artifact: str) -> list[Row]:
    rows: list[Row] = db.execute(
        "SELECT * FROM artifact_owners WHERE artifact = ? ORDER BY created_at, owner", (artifact,)
    ).fetchall()
    return rows


def remove_artifact_owner(db: sqlite3.Connection, artifact: str, owner: str | None) -> int:
    if owner is None:
        return db.execute("DELETE FROM artifact_owners WHERE artifact = ?", (artifact,)).rowcount
    return db.execute(
        "DELETE FROM artifact_owners WHERE artifact = ? AND owner = ?", (artifact, owner)
    ).rowcount


def artifacts_held_by(db: sqlite3.Connection, owners: Sequence[str]) -> list[str]:
    found: list[str] = []
    for owner in owners:
        rows = db.execute("SELECT artifact FROM artifact_owners WHERE owner = ?", (owner,))
        found.extend(str(row["artifact"]) for row in rows)
    return found


def remove_holds_of(db: sqlite3.Connection, owners: Sequence[str]) -> list[str]:
    """Drop every hold these owners have; return the artifacts that lost one."""
    touched = artifacts_held_by(db, owners)
    for owner in owners:
        db.execute("DELETE FROM artifact_owners WHERE owner = ?", (owner,))
    return touched


def release_if_unheld(db: sqlite3.Connection, artifact: str) -> bool:
    """A live artifact with no holds left becomes `released` (M-10)."""
    if db.execute("SELECT 1 FROM artifact_owners WHERE artifact = ?", (artifact,)).fetchone():
        return False
    return (
        db.execute(
            "UPDATE artifacts SET state = 'released' WHERE handle = ? AND state = 'live'",
            (artifact,),
        ).rowcount
        > 0
    )


def dead_holds(db: sqlite3.Connection, edges: Edges) -> list[Row]:
    """Holds whose owner is no longer live (M-4)."""
    params = {"grace_edge": edges.grace_edge, "idle_edge": edges.idle_edge}
    rows: list[Row] = db.execute(
        "SELECT artifact, owner FROM artifact_owners WHERE NOT ("
        f"(owner_kind = 'action' AND owner IN ({LIVE_ACTION})) OR "
        f"(owner_kind = 'operation' AND owner IN ({LIVE_OPERATION})) OR "
        f"(owner_kind = 'grant' AND owner IN ({LIVE_GRANT})) OR "
        f"(owner_kind = 'session' AND owner IN ({LIVE_SESSION})) OR "
        f"(owner_kind = 'run' AND owner IN ({LIVE_RUN})) OR "
        f"(owner_kind = 'card' AND owner IN ({LIVE_CARD})))",
        params,
    ).fetchall()
    return rows


def count_artifacts_without_live_owner(db: sqlite3.Connection, edges: Edges) -> int:
    """Live artifacts that no live owner holds (the `artifacts_without_owner` metric)."""
    params = {"grace_edge": edges.grace_edge, "idle_edge": edges.idle_edge}
    row = db.execute(
        "SELECT count(*) FROM artifacts a WHERE a.state = 'live' AND NOT EXISTS ("
        "SELECT 1 FROM artifact_owners o WHERE o.artifact = a.handle AND ("
        f"(o.owner_kind = 'action' AND o.owner IN ({LIVE_ACTION})) OR "
        f"(o.owner_kind = 'operation' AND o.owner IN ({LIVE_OPERATION})) OR "
        f"(o.owner_kind = 'grant' AND o.owner IN ({LIVE_GRANT})) OR "
        f"(o.owner_kind = 'session' AND o.owner IN ({LIVE_SESSION})) OR "
        f"(o.owner_kind = 'run' AND o.owner IN ({LIVE_RUN})) OR "
        f"(o.owner_kind = 'card' AND o.owner IN ({LIVE_CARD}))))",
        params,
    ).fetchone()
    return int(row[0])


def expirable_artifacts(db: sqlite3.Connection, now: str) -> list[str]:
    """Live artifacts past `expires_at` that no operation still holds (M-11)."""
    rows = db.execute(
        "SELECT handle FROM artifacts WHERE state = 'live' AND expires_at <= ? "
        "AND handle NOT IN (SELECT artifact FROM artifact_owners WHERE owner_kind = 'operation')",
        (now,),
    )
    return [str(row["handle"]) for row in rows]


def unheld_live_artifacts(db: sqlite3.Connection) -> list[str]:
    rows = db.execute(
        "SELECT handle FROM artifacts WHERE state = 'live' "
        "AND handle NOT IN (SELECT artifact FROM artifact_owners)"
    )
    return [str(row["handle"]) for row in rows]


def set_artifact_state(
    db: sqlite3.Connection, handle: str, state: str, deleted_at: str | None = None
) -> None:
    db.execute(
        "UPDATE artifacts SET state = ?, deleted_at = COALESCE(?, deleted_at) WHERE handle = ?",
        (state, deleted_at, handle),
    )


def renew_artifact(db: sqlite3.Connection, handle: str, expires_at: str) -> None:
    db.execute(
        "UPDATE artifacts SET expires_at = ? WHERE handle = ? AND state = 'live'",
        (expires_at, handle),
    )


def released_shas(db: sqlite3.Connection) -> list[str]:
    rows = db.execute("SELECT DISTINCT sha256 FROM artifacts WHERE state = 'released'")
    return [str(row["sha256"]) for row in rows]


def sha_has_live_row(db: sqlite3.Connection, sha256: str) -> bool:
    row = db.execute(
        "SELECT 1 FROM artifacts WHERE sha256 = ? AND state = 'live'", (sha256,)
    ).fetchone()
    return row is not None


def mark_sha_deleted(db: sqlite3.Connection, sha256: str, now: str) -> int:
    return db.execute(
        "UPDATE artifacts SET state = 'deleted', deleted_at = ? "
        "WHERE sha256 = ? AND state = 'released'",
        (now, sha256),
    ).rowcount


def live_artifacts(db: sqlite3.Connection) -> list[Row]:
    rows: list[Row] = db.execute("SELECT * FROM artifacts WHERE state = 'live'").fetchall()
    return rows


def known_shas(db: sqlite3.Connection) -> set[str]:
    """Hashes with a row that is not yet a tombstone (live or released)."""
    rows = db.execute("SELECT DISTINCT sha256 FROM artifacts WHERE state != 'deleted'")
    return {str(row["sha256"]) for row in rows}


def purge_tombstones(db: sqlite3.Connection, edge: str) -> int:
    return db.execute(
        "DELETE FROM artifacts WHERE state = 'deleted' AND deleted_at IS NOT NULL "
        "AND deleted_at <= ?",
        (edge,),
    ).rowcount


def search_artifacts(db: sqlite3.Connection, text: str | None, limit: int) -> list[Row]:
    pattern = f"%{_like(text)}%" if text else "%"
    rows: list[Row] = db.execute(
        "SELECT * FROM artifacts WHERE state = 'live' AND "
        "(reason LIKE ? ESCAPE '\\' OR COALESCE(filename, '') LIKE ? ESCAPE '\\') "
        "ORDER BY created_at DESC, handle LIMIT ?",
        (pattern, pattern, limit),
    ).fetchall()
    return rows


def total_stored_bytes(db: sqlite3.Connection) -> int:
    """Bytes on disk: each hash counted once."""
    return scalar(
        db,
        "SELECT COALESCE(SUM(size), 0) FROM "
        "(SELECT DISTINCT sha256, size FROM artifacts WHERE state IN ('live', 'released'))",
    )


def card_live_bytes(db: sqlite3.Connection, card: str) -> int:
    return scalar(
        db,
        "SELECT COALESCE(SUM(a.size), 0) FROM artifacts a WHERE a.state = 'live' AND "
        "a.handle IN (SELECT o.artifact FROM artifact_owners o WHERE "
        "(o.owner_kind = 'action' AND o.owner IN (SELECT handle FROM actions WHERE card = ?)) "
        "OR (o.owner_kind = 'card' AND o.owner = ?))",
        (card, card),
    )


def card_of_action(db: sqlite3.Connection, action: str) -> str | None:
    row = db.execute("SELECT card FROM actions WHERE handle = ?", (action,)).fetchone()
    return None if row is None else str(row["card"])


def cards_over_artifact_budget(db: sqlite3.Connection, limit: int) -> list[tuple[str, int]]:
    rows = db.execute(
        "SELECT card, SUM(size) AS n FROM ("
        "SELECT ac.card AS card, a.size AS size FROM artifacts a "
        "JOIN artifact_owners o ON o.artifact = a.handle AND o.owner_kind = 'action' "
        "JOIN actions ac ON ac.handle = o.owner WHERE a.state = 'live' "
        "UNION ALL SELECT o.owner AS card, a.size AS size FROM artifacts a "
        "JOIN artifact_owners o ON o.artifact = a.handle AND o.owner_kind = 'card' "
        "WHERE a.state = 'live') GROUP BY card HAVING SUM(size) > ?",
        (limit,),
    )
    return [(str(row["card"]), int(row["n"])) for row in rows]


def largest_live(db: sqlite3.Connection, limit: int) -> list[tuple[str, int]]:
    rows = db.execute(
        "SELECT handle, size FROM artifacts WHERE state = 'live' ORDER BY size DESC, handle "
        "LIMIT ?",
        (limit,),
    )
    return [(str(row["handle"]), int(row["size"])) for row in rows]


# --- refs ------------------------------------------------------------------------


def get_ref(db: sqlite3.Connection, handle: str) -> Row | None:
    row: Row | None = db.execute("SELECT * FROM refs WHERE handle = ?", (handle,)).fetchone()
    return row


def find_ref(db: sqlite3.Connection, source: str, kind: str, locator: str) -> Row | None:
    row: Row | None = db.execute(
        "SELECT * FROM refs WHERE source_id = ? AND kind = ? AND locator = ?",
        (source, kind, locator),
    ).fetchone()
    return row


def refs_by_locator(db: sqlite3.Connection, locator: str, source: str | None) -> list[Row]:
    if source is None:
        rows: list[Row] = db.execute("SELECT * FROM refs WHERE locator = ?", (locator,)).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM refs WHERE locator = ? AND source_id = ?", (locator, source)
        ).fetchall()
    return rows


def insert_ref(
    db: sqlite3.Connection,
    *,
    handle: str,
    kind: str,
    source: str,
    locator: str,
    internet_message_id: str | None,
    now: str,
) -> None:
    db.execute(
        "INSERT INTO refs (handle, kind, source_id, locator, internet_message_id, "
        "first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (handle, kind, source, locator, internet_message_id, now, now),
    )


def touch_ref(
    db: sqlite3.Connection, handle: str, internet_message_id: str | None, now: str
) -> None:
    db.execute(
        "UPDATE refs SET last_seen_at = ?, internet_message_id = COALESCE(?, internet_message_id) "
        "WHERE handle = ?",
        (now, internet_message_id, handle),
    )


def set_ref_locator(db: sqlite3.Connection, handle: str, locator: str, now: str) -> None:
    db.execute(
        "UPDATE refs SET locator = ?, last_seen_at = ? WHERE handle = ?", (locator, now, handle)
    )


def delete_ref(db: sqlite3.Connection, handle: str) -> None:
    db.execute("DELETE FROM refs WHERE handle = ?", (handle,))


def rewrite_ref_users(db: sqlite3.Connection, old: str, new: str) -> None:
    """Point every record that named handle `old` at handle `new` (used by repoint)."""
    db.execute("UPDATE OR IGNORE card_links SET ref = ? WHERE ref = ?", (new, old))
    db.execute("DELETE FROM card_links WHERE ref = ?", (old,))
    db.execute("UPDATE evidence SET ref = ? WHERE ref = ?", (new, old))


def delete_stale_refs(db: sqlite3.Connection, edge: str) -> int:
    """Refs unseen since `edge` and held by no live record (lifetime table)."""
    return db.execute(
        "DELETE FROM refs WHERE last_seen_at <= ? "
        "AND handle NOT IN (SELECT ref FROM card_links) "
        "AND handle NOT IN (SELECT ref FROM evidence WHERE ref IS NOT NULL) "
        "AND NOT EXISTS (SELECT 1 FROM operations o WHERE o.refs_json LIKE '%' || refs.handle "
        "|| '%' AND o.state NOT IN ('confirmed', 'failed_clean', 'confirmed_absent', "
        "'cancelled', 'abandoned', 'superseded'))",
        (edge,),
    ).rowcount


# --- cursors and leases --------------------------------------------------------------


def session_exists(db: sqlite3.Connection, session: str) -> bool:
    return db.execute("SELECT 1 FROM sessions WHERE handle = ?", (session,)).fetchone() is not None


def insert_cursor(
    db: sqlite3.Connection,
    *,
    cursor_id: str,
    session: str,
    tool: str,
    query_sha256: str,
    payload_json: str,
    now: str,
    expires_at: str,
) -> None:
    db.execute(
        "INSERT INTO cursors (id, session, tool, query_sha256, payload_json, created_at, "
        "expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (cursor_id, session, tool, query_sha256, payload_json, now, expires_at),
    )


def get_cursor(db: sqlite3.Connection, cursor_id: str) -> Row | None:
    row: Row | None = db.execute("SELECT * FROM cursors WHERE id = ?", (cursor_id,)).fetchone()
    return row


def delete_expired_cursors(db: sqlite3.Connection, now: str, idle_edge: str) -> int:
    """Expired cursors, and those of sessions that ended or went idle."""
    return db.execute(
        "DELETE FROM cursors WHERE expires_at <= ? OR session NOT IN "
        "(SELECT handle FROM sessions WHERE ended_at IS NULL AND last_seen_at > ?)",
        (now, idle_edge),
    ).rowcount


def insert_lease(
    db: sqlite3.Connection, *, lease_id: str, artifact: str, path: str, now: str, expires_at: str
) -> None:
    db.execute(
        "INSERT INTO leases (id, artifact, path, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
        (lease_id, artifact, path, now, expires_at),
    )


def expired_leases(db: sqlite3.Connection, now: str) -> list[Row]:
    rows: list[Row] = db.execute(
        "SELECT * FROM leases WHERE expires_at <= ? ORDER BY id", (now,)
    ).fetchall()
    return rows


def all_leases(db: sqlite3.Connection) -> list[Row]:
    rows: list[Row] = db.execute("SELECT * FROM leases").fetchall()
    return rows


def delete_lease(db: sqlite3.Connection, lease_id: str) -> None:
    db.execute("DELETE FROM leases WHERE id = ?", (lease_id,))


# --- quarantine ---------------------------------------------------------------------------


def insert_quarantine(
    db: sqlite3.Connection,
    *,
    original: str,
    quarantined: str,
    size: int,
    found_at: str,
    delete_after: str,
) -> None:
    db.execute(
        "INSERT INTO quarantine (original_path, quarantined_path, size, found_at, delete_after) "
        "VALUES (?, ?, ?, ?, ?)",
        (original, quarantined, size, found_at, delete_after),
    )


def due_quarantine(db: sqlite3.Connection, now: str) -> list[Row]:
    rows: list[Row] = db.execute(
        "SELECT * FROM quarantine WHERE state = 'held' AND delete_after <= ? ORDER BY id", (now,)
    ).fetchall()
    return rows


def mark_quarantine_deleted(db: sqlite3.Connection, row_id: int) -> None:
    db.execute("UPDATE quarantine SET state = 'deleted' WHERE id = ?", (row_id,))


def purge_quarantine_rows(db: sqlite3.Connection, edge: str) -> int:
    return db.execute(
        "DELETE FROM quarantine WHERE state = 'deleted' AND delete_after <= ?", (edge,)
    ).rowcount


def held_quarantine(db: sqlite3.Connection) -> list[Row]:
    rows: list[Row] = db.execute("SELECT * FROM quarantine WHERE state = 'held'").fetchall()
    return rows


# --- reading other tracks' tables, for views and metrics ----------------------------------


def get_grant(db: sqlite3.Connection, handle: str) -> Row | None:
    row: Row | None = db.execute("SELECT * FROM grants WHERE handle = ?", (handle,)).fetchone()
    return row


def get_operation(db: sqlite3.Connection, handle: str) -> Row | None:
    row: Row | None = db.execute("SELECT * FROM operations WHERE handle = ?", (handle,)).fetchone()
    return row


def resend_handle(db: sqlite3.Connection, original: str) -> str | None:
    """The live resend of an uncertain send, if one was made (W-38)."""
    row: Row | None = db.execute(
        "SELECT handle FROM operations WHERE redo_of = ? AND state <> 'cancelled'", (original,)
    ).fetchone()
    return None if row is None else str(row["handle"])


def get_event(db: sqlite3.Connection, handle: str) -> Row | None:
    row: Row | None = db.execute("SELECT * FROM events WHERE handle = ?", (handle,)).fetchone()
    return row


def list_grants(db: sqlite3.Connection, text: str | None, limit: int) -> list[Row]:
    rows: list[Row] = db.execute(
        "SELECT * FROM grants WHERE title LIKE ? ESCAPE '\\' ORDER BY proposed_at DESC, handle "
        "LIMIT ?",
        (f"%{_like(text)}%" if text else "%", limit),
    ).fetchall()
    return rows


def list_operations(db: sqlite3.Connection, text: str | None, limit: int) -> list[Row]:
    rows: list[Row] = db.execute(
        "SELECT * FROM operations WHERE kind LIKE ? ESCAPE '\\' ORDER BY created_at DESC, handle "
        "LIMIT ?",
        (f"%{_like(text)}%" if text else "%", limit),
    ).fetchall()
    return rows


def scalar(db: sqlite3.Connection, sql: str, params: Iterable[object] = ()) -> int:
    """First column of the first row as an int; 0 when there is none."""
    row = db.execute(sql, tuple(params)).fetchone()
    return 0 if row is None or row[0] is None else int(row[0])


def _like(text: str | None) -> str:
    if text is None:
        return ""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# --- event labels (the category the agent gave a calendar event; migration 0130) -----------------


def upsert_event_label(
    db: sqlite3.Connection,
    *,
    identity: str,
    category: str,
    title: str,
    last_day: str,
    now: str,
) -> None:
    db.execute(
        "INSERT INTO event_labels (identity, category, title, last_day, labelled_at) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT (identity) DO UPDATE SET category = excluded.category, "
        "title = excluded.title, last_day = excluded.last_day, labelled_at = excluded.labelled_at",
        (identity, category, title, last_day, now),
    )


def event_labels_since(db: sqlite3.Connection, day: str) -> list[Row]:
    """Labels of events that end on or after `day` (a civil date)."""
    rows: list[Row] = db.execute(
        "SELECT identity, category, title FROM event_labels WHERE last_day >= ?", (day,)
    ).fetchall()
    return rows


def delete_event_labels_before(db: sqlite3.Connection, day: str) -> int:
    return db.execute("DELETE FROM event_labels WHERE last_day < ?", (day,)).rowcount
