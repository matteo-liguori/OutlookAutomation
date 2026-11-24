"""All SQL of the writes track (S-4): grants, known recipients, operations, audit log.

Contract: docs/contracts/09-storage.md. Functions take the connection and
plain strings; timestamps are the stored D-7 text. Nothing here decides
anything: rules live in `authorise.py`, `grants.py` and `engine.py`.
"""

import contextlib
import json
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from chamberlain.domain.grant_rules import format_ts, parse_ts
from chamberlain.domain.grants import GrantKind, GrantState, Requirement, StandingScope
from chamberlain.domain.intents import IntentKind
from chamberlain.domain.operations import RELEASING_STATES, OperationState
from chamberlain.domain.senders import AuthorisedSenders
from chamberlain.store.schema import AUTHORISED_SENDERS_KEY, get_state
from chamberlain.writes.models import GrantRow, OperationRow

_RELEASING = tuple(state.value for state in RELEASING_STATES)


def _ts(value: str | None) -> datetime | None:
    return None if value is None else parse_ts(value)


def _marks(items: Sequence[object]) -> str:
    return ", ".join("?" for _ in items)


# --- grants ---------------------------------------------------------------------


def _grant(row: sqlite3.Row) -> GrantRow:
    scope_json: str | None = row["scope_json"]
    return GrantRow(
        handle=row["handle"],
        kind=GrantKind(row["kind"]),
        state=GrantState(row["state"]),
        source_id=row["source_id"],
        title=row["title"],
        summary=row["summary"],
        requirement=Requirement(row["requirement"]),
        content_sha256=row["content_sha256"],
        scope=None if scope_json is None else StandingScope.model_validate_json(scope_json),
        card=row["card"],
        supersedes=row["supersedes"],
        proposed_by=row["proposed_by"],
        proposed_at=parse_ts(row["proposed_at"]),
        confirmed_via=row["confirmed_via"],
        granted_at=_ts(row["granted_at"]),
        expires_at=_ts(row["expires_at"]),
        review_at=_ts(row["review_at"]),
        last_reviewed_at=_ts(row["last_reviewed_at"]),
        ended_at=_ts(row["ended_at"]),
        compacted_at=_ts(row["compacted_at"]),
    )


def insert_grant(
    db: sqlite3.Connection,
    *,
    handle: str,
    kind: GrantKind,
    source_id: str,
    title: str,
    summary: str,
    requirement: Requirement,
    content_sha256: str,
    scope_json: str | None,
    card: str | None,
    supersedes: str | None,
    proposed_by: str | None,
    proposed_at: str,
) -> None:
    db.execute(
        "INSERT INTO grants (handle, kind, state, source_id, title, summary, requirement, "
        "content_sha256, scope_json, card, supersedes, proposed_by, proposed_at) "
        "VALUES (?, ?, 'proposed', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            handle,
            kind.value,
            source_id,
            title,
            summary,
            requirement.value,
            content_sha256,
            scope_json,
            card,
            supersedes,
            proposed_by,
            proposed_at,
        ),
    )


def get_grant(db: sqlite3.Connection, handle: str) -> GrantRow | None:
    row = db.execute("SELECT * FROM grants WHERE handle = ?", (handle,)).fetchone()
    return None if row is None else _grant(row)


def grants_in_states(
    db: sqlite3.Connection, states: Iterable[GrantState], source_id: str | None = None
) -> list[GrantRow]:
    wanted = [state.value for state in states]
    sql = f"SELECT * FROM grants WHERE state IN ({_marks(wanted)})"
    args: list[Any] = list(wanted)
    if source_id is not None:
        sql += " AND source_id = ?"
        args.append(source_id)
    rows = db.execute(sql + " ORDER BY proposed_at, handle", args).fetchall()
    return [_grant(row) for row in rows]


def count_grants(db: sqlite3.Connection) -> int:
    return int(db.execute("SELECT COUNT(*) FROM grants").fetchone()[0])


def update_grant(
    db: sqlite3.Connection, handle: str, expected: GrantState, **columns: str | None
) -> bool:
    """Set columns on a grant still in `expected`. False means someone else moved it."""
    names = ", ".join(f"{name} = ?" for name in columns)
    cursor = db.execute(
        f"UPDATE grants SET {names} WHERE handle = ? AND state = ?",
        (*columns.values(), handle, expected.value),
    )
    return cursor.rowcount == 1


def proposed_one_off(
    db: sqlite3.Connection, source_id: str, content_sha256: str
) -> GrantRow | None:
    row = db.execute(
        "SELECT * FROM grants WHERE kind = 'one_off' AND state = 'proposed' "
        "AND source_id = ? AND content_sha256 = ? ORDER BY proposed_at DESC LIMIT 1",
        (source_id, content_sha256),
    ).fetchone()
    return None if row is None else _grant(row)


def card_is_closed(db: sqlite3.Connection, card: str) -> bool:
    row = db.execute("SELECT state FROM cards WHERE handle = ?", (card,)).fetchone()
    return row is not None and row["state"] == "closed"


def evidence_facts(db: sqlite3.Connection, handle: str) -> tuple[str | None, str | None] | None:
    """`(session, ref)` of an evidence row, or None if it does not exist (G-13)."""
    row = db.execute("SELECT session, ref FROM evidence WHERE handle = ?", (handle,)).fetchone()
    return None if row is None else (row["session"], row["ref"])


def compactable_grants(db: sqlite3.Connection, before: str) -> list[str]:
    """Terminal grants ended before `before` with no non-terminal operation holding them (G-10)."""
    rows = db.execute(
        "SELECT handle FROM grants WHERE compacted_at IS NULL "
        "AND state IN ('spent', 'revoked', 'expired', 'declined', 'lapsed') "
        "AND ended_at IS NOT NULL AND ended_at <= ? "
        "AND NOT EXISTS (SELECT 1 FROM operations o WHERE o.grant = grants.handle "
        "AND o.state NOT IN ('confirmed', 'failed_clean', 'confirmed_absent', 'cancelled', "
        "'abandoned'))",
        (before,),
    ).fetchall()
    return [row["handle"] for row in rows]


def compact_grant(db: sqlite3.Connection, handle: str, at: str, compact_scope: str) -> None:
    db.execute(
        "UPDATE grants SET scope_json = NULL, summary = '', compacted_at = ?, "
        "title = ? WHERE handle = ?",
        (at, compact_scope, handle),
    )


def delete_compacted_grants(db: sqlite3.Connection, before: str) -> int:
    cursor = db.execute(
        "DELETE FROM grants WHERE compacted_at IS NOT NULL AND ended_at <= ? "
        "AND NOT EXISTS (SELECT 1 FROM operations o WHERE o.grant = grants.handle)",
        (before,),
    )
    return cursor.rowcount


# --- known recipients -------------------------------------------------------------


_ADDRESS_KEYS = ("mailbox", "address", "addresses", "account", "username")
"""Binding keys that may hold the address a source belongs to."""


def _binding_addresses(binding_json: str) -> frozenset[str]:
    try:
        binding = json.loads(binding_json)
    except ValueError:
        return frozenset()
    found: set[str] = set()
    if isinstance(binding, dict):
        values = cast(dict[str, object], binding)
        for key in _ADDRESS_KEYS:
            value = values.get(key)
            if isinstance(value, str):
                found.add(value.strip().lower())
            elif isinstance(value, list):
                found.update(str(v).strip().lower() for v in cast(list[object], value))
    return frozenset(a for a in found if "@" in a)


def own_addresses(db: sqlite3.Connection, source_id: str) -> frozenset[str]:
    """The mailbox address(es) a source is bound to: always known recipients."""
    row = db.execute("SELECT binding_json FROM sources WHERE id = ?", (source_id,)).fetchone()
    return frozenset() if row is None else _binding_addresses(str(row["binding_json"]))


def owner_addresses(db: sqlite3.Connection) -> frozenset[str]:
    """Every address that is the owner's: any configured source's own address, and the
    `owner` list of `authorised_senders` (G-12b). Lower-cased."""
    found: set[str] = set()
    for row in db.execute("SELECT binding_json FROM sources").fetchall():
        found |= _binding_addresses(str(row["binding_json"]))
    raw = get_state(db, AUTHORISED_SENDERS_KEY)
    if raw is not None:
        with contextlib.suppress(ValueError):
            found.update(AuthorisedSenders.model_validate_json(raw).owner)
    return frozenset(found)


def known_recipients(db: sqlite3.Connection, source_id: str) -> frozenset[str]:
    """Confirmed, seeded from Sent history, or the source's own address."""
    rows = db.execute(
        "SELECT address FROM known_recipients WHERE source_id = ?", (source_id,)
    ).fetchall()
    return frozenset(row["address"] for row in rows) | own_addresses(db, source_id)


def add_known_recipient(
    db: sqlite3.Connection, source_id: str, address: str, grant: str, at: str
) -> None:
    db.execute(
        "INSERT OR IGNORE INTO known_recipients (source_id, address, grant, origin, confirmed_at) "
        "VALUES (?, ?, ?, 'confirmed-grant', ?)",
        (source_id, address.lower(), grant, at),
    )


def seed_known_recipients(
    db: sqlite3.Connection, source_id: str, addresses: Iterable[str], at: str
) -> int:
    """Add Sent-history recipients. Existing rows stay as they are. Returns rows added."""
    added = 0
    for address in sorted({a.strip().lower() for a in addresses if "@" in a}):
        cursor = db.execute(
            "INSERT OR IGNORE INTO known_recipients (source_id, address, grant, origin, "
            "confirmed_at) VALUES (?, ?, NULL, 'sent-history', ?)",
            (source_id, address, at),
        )
        added += cursor.rowcount
    return added


def recipient_counts(db: sqlite3.Connection, source_id: str) -> dict[str, int]:
    """Known recipients by origin, plus the source's own addresses."""
    counts = {"confirmed-grant": 0, "sent-history": 0}
    for row in db.execute(
        "SELECT origin, count(*) AS n FROM known_recipients WHERE source_id = ? GROUP BY origin",
        (source_id,),
    ):
        counts[str(row["origin"])] = int(row["n"])
    counts["own"] = len(own_addresses(db, source_id))
    return counts


# --- operations -------------------------------------------------------------------


def _operation(row: sqlite3.Row) -> OperationRow:
    return OperationRow(
        handle=row["handle"],
        idempotency_key=row["idempotency_key"],
        kind=IntentKind(row["kind"]),
        source_id=row["source_id"],
        state=OperationState(row["state"]),
        grant=row["grant"],
        card=row["card"],
        session=row["session"],
        intent_sha256=row["intent_sha256"],
        intent_json=row["intent_json"],
        reconcile_token=row["reconcile_token"],
        refs_json=row["refs_json"],
        prepared_json=row["prepared_json"],
        receipt_json=row["receipt_json"],
        client_verified=bool(row["client_verified"]),
        server_verified=bool(row["server_verified"]),
        error_code=row["error_code"],
        lease_owner=row["lease_owner"],
        lease_expires_at=_ts(row["lease_expires_at"]),
        created_at=parse_ts(row["created_at"]),
        dispatched_at=_ts(row["dispatched_at"]),
        settled_at=_ts(row["settled_at"]),
        compacted_at=_ts(row["compacted_at"]),
        updated_at=parse_ts(row["updated_at"]),
        summary_json=row["summary_json"],
        attempted_at=_ts(row["attempted_at"]),
        redo_at=_ts(row["redo_at"]),
    )


def insert_operation(
    db: sqlite3.Connection,
    *,
    handle: str,
    idempotency_key: str,
    kind: IntentKind,
    source_id: str,
    card: str | None,
    session: str | None,
    intent_sha256: str,
    intent_json: str,
    refs_json: str,
    at: str,
) -> None:
    db.execute(
        "INSERT INTO operations (handle, idempotency_key, kind, source_id, state, card, "
        "session, intent_sha256, intent_json, refs_json, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'proposed', ?, ?, ?, ?, ?, ?, ?)",
        (
            handle,
            idempotency_key,
            kind.value,
            source_id,
            card,
            session,
            intent_sha256,
            intent_json,
            refs_json,
            at,
            at,
        ),
    )


def set_operation_summary(
    db: sqlite3.Connection, handle: str, summary_json: str, attempted_at: str | None
) -> bool:
    """Give an imported operation its display summary once. False when it already has one."""
    return (
        db.execute(
            "UPDATE operations SET summary_json = ?, attempted_at = ? "
            "WHERE handle = ? AND summary_json IS NULL AND intent_json IS NULL",
            (summary_json, attempted_at, handle),
        ).rowcount
        == 1
    )


def mark_redo(db: sqlite3.Connection, handle: str, at: str) -> None:
    db.execute("UPDATE operations SET redo_at = ? WHERE handle = ?", (at, handle))


def get_operation(db: sqlite3.Connection, handle: str) -> OperationRow | None:
    row = db.execute("SELECT * FROM operations WHERE handle = ?", (handle,)).fetchone()
    return None if row is None else _operation(row)


def update_operation(
    db: sqlite3.Connection,
    handle: str,
    *,
    expected_state: OperationState,
    expected_lease: str | bool | None = False,
    **columns: str | int | None,
) -> bool:
    """Compare-and-set on the state, and on the lease owner when `expected_lease` is given.

    A process that lost its lease finds the row moved and the update refused (R5).
    """
    names = ", ".join(f"{name} = ?" for name in columns)
    sql = f"UPDATE operations SET {names} WHERE handle = ? AND state = ?"
    args: list[Any] = [*columns.values(), handle, expected_state.value]
    if expected_lease is not False:
        if expected_lease is None:
            sql += " AND lease_owner IS NULL"
        else:
            sql += " AND lease_owner = ?"
            args.append(expected_lease)
    return db.execute(sql, args).rowcount == 1


def operations_in_states(
    db: sqlite3.Connection, states: Iterable[OperationState]
) -> list[OperationRow]:
    wanted = [state.value for state in states]
    rows = db.execute(
        f"SELECT * FROM operations WHERE state IN ({_marks(wanted)}) ORDER BY created_at, handle",
        wanted,
    ).fetchall()
    return [_operation(row) for row in rows]


def operations_by_hash(
    db: sqlite3.Connection, source_id: str, intent_sha256: str
) -> list[OperationRow]:
    rows = db.execute(
        "SELECT * FROM operations WHERE source_id = ? AND intent_sha256 = ? "
        "ORDER BY created_at, handle",
        (source_id, intent_sha256),
    ).fetchall()
    return [_operation(row) for row in rows]


def operation_for_grant_hash(
    db: sqlite3.Connection, source_id: str, intent_sha256: str
) -> OperationRow | None:
    """The proposed operation a one-off grant was made for."""
    row = db.execute(
        "SELECT * FROM operations WHERE source_id = ? AND intent_sha256 = ? "
        "AND state = 'proposed' ORDER BY created_at DESC LIMIT 1",
        (source_id, intent_sha256),
    ).fetchone()
    return None if row is None else _operation(row)


def attempts_under(db: sqlite3.Connection, grant: str, intent_sha256: str, other_than: str) -> int:
    """Earlier operations under (grant, hash) in any state: the attempt number (Q4)."""
    row = db.execute(
        "SELECT COUNT(*) FROM operations WHERE grant = ? AND intent_sha256 = ? AND handle <> ?",
        (grant, intent_sha256, other_than),
    ).fetchone()
    return int(row[0])


def live_uses(db: sqlite3.Connection, grants: Iterable[str], other_than: str) -> dict[str, int]:
    """Reserved or spent uses per grant, derived from operations (G-11)."""
    wanted = list(grants)
    if not wanted:
        return {}
    rows = db.execute(
        f"SELECT grant, COUNT(*) AS n FROM operations WHERE grant IN ({_marks(wanted)}) "
        f"AND handle <> ? AND state NOT IN ({_marks(_RELEASING)}) AND state <> 'proposed' "
        "GROUP BY grant",
        (*wanted, other_than, *_RELEASING),
    ).fetchall()
    return {row["grant"]: int(row["n"]) for row in rows}


def recent_uses(
    db: sqlite3.Connection, grants: Iterable[str], since: str, other_than: str
) -> dict[str, int]:
    """Uses that count against `max_per_day`: dispatched since, or reserved and waiting."""
    wanted = list(grants)
    if not wanted:
        return {}
    rows = db.execute(
        f"SELECT grant, COUNT(*) AS n FROM operations WHERE grant IN ({_marks(wanted)}) "
        f"AND handle <> ? AND state NOT IN ({_marks(_RELEASING)}) AND state <> 'proposed' "
        "AND (dispatched_at IS NULL OR dispatched_at >= ?) GROUP BY grant",
        (*wanted, other_than, *_RELEASING, since),
    ).fetchall()
    return {row["grant"]: int(row["n"]) for row in rows}


def sends_since(db: sqlite3.Connection, source_id: str, since: str, other_than: str) -> int:
    """Sends dispatched since `since` in any state, plus reserved ones not yet dispatched (Q3)."""
    row = db.execute(
        "SELECT COUNT(*) FROM operations WHERE source_id = ? AND kind = 'mail.send' "
        "AND handle <> ? AND (dispatched_at >= ? "
        "OR (dispatched_at IS NULL AND state IN ('authorised', 'prepared')))",
        (source_id, other_than, since),
    ).fetchone()
    return int(row[0])


def live_by_hash(db: sqlite3.Connection, source_id: str, intent_sha256: str) -> list[OperationRow]:
    """Operations for this content that are not released (W-18, W-19)."""
    rows = db.execute(
        "SELECT * FROM operations WHERE source_id = ? AND intent_sha256 = ? "
        f"AND state NOT IN ({_marks(_RELEASING)}) ORDER BY created_at, handle",
        (source_id, intent_sha256, *_RELEASING),
    ).fetchall()
    return [_operation(row) for row in rows]


def stale_uncertain(db: sqlite3.Connection, before: str) -> list[OperationRow]:
    """Uncertain since `before` with no way left to send them again (owner ruling)."""
    rows = db.execute(
        "SELECT o.* FROM operations o LEFT JOIN grants g ON g.handle = o.grant "
        "WHERE o.state = 'uncertain' AND o.updated_at <= ? AND (o.intent_json IS NULL "
        "OR o.grant IS NULL OR g.state IN ('spent', 'expired', 'revoked', 'lapsed', 'declined')) "
        "ORDER BY o.created_at, o.handle",
        (before,),
    ).fetchall()
    return [_operation(row) for row in rows]


def compactable_operations(db: sqlite3.Connection, before: str) -> list[OperationRow]:
    rows = db.execute(
        "SELECT * FROM operations WHERE compacted_at IS NULL AND settled_at IS NOT NULL "
        "AND settled_at <= ? AND state IN ('confirmed', 'failed_clean', 'confirmed_absent', "
        "'cancelled', 'abandoned')",
        (before,),
    ).fetchall()
    return [_operation(row) for row in rows]


def compact_operation(db: sqlite3.Connection, handle: str, at: str, receipt_json: str) -> None:
    db.execute(
        "UPDATE operations SET intent_json = NULL, prepared_json = NULL, refs_json = NULL, "
        "receipt_json = ?, compacted_at = ? WHERE handle = ?",
        (receipt_json, at, handle),
    )


def delete_compacted_operations(db: sqlite3.Connection, before: str) -> int:
    cursor = db.execute(
        "DELETE FROM operations WHERE compacted_at IS NOT NULL AND settled_at <= ?", (before,)
    )
    return cursor.rowcount


def count_operations(db: sqlite3.Connection) -> int:
    return int(db.execute("SELECT COUNT(*) FROM operations").fetchone()[0])


# --- audit log --------------------------------------------------------------------


@dataclass(frozen=True)
class AuditRow:
    seq: int
    at: str
    actor: str
    kind: str
    subject: str | None
    summary: str
    payload_sha256: str | None
    prev_hash: str
    hash: str


def _audit(row: sqlite3.Row) -> AuditRow:
    return AuditRow(
        seq=row["seq"],
        at=row["at"],
        actor=row["actor"],
        kind=row["kind"],
        subject=row["subject"],
        summary=row["summary"],
        payload_sha256=row["payload_sha256"],
        prev_hash=row["prev_hash"],
        hash=row["hash"],
    )


def audit_last(db: sqlite3.Connection) -> AuditRow | None:
    row = db.execute("SELECT * FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
    return None if row is None else _audit(row)


def audit_next_seq(db: sqlite3.Connection) -> int:
    row = db.execute("SELECT seq FROM sqlite_sequence WHERE name = 'audit_log'").fetchone()
    return 1 if row is None else int(row["seq"]) + 1


def audit_insert(db: sqlite3.Connection, row: AuditRow) -> None:
    db.execute(
        "INSERT INTO audit_log (seq, at, actor, kind, subject, summary, payload_sha256, "
        "prev_hash, hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            row.seq,
            row.at,
            row.actor,
            row.kind,
            row.subject,
            row.summary,
            row.payload_sha256,
            row.prev_hash,
            row.hash,
        ),
    )


def audit_rows(db: sqlite3.Connection) -> list[AuditRow]:
    return [_audit(row) for row in db.execute("SELECT * FROM audit_log ORDER BY seq")]


def audit_rows_before(db: sqlite3.Connection, cutoff: str) -> list[AuditRow]:
    """The leading run of rows older than `cutoff` (the log is in time order)."""
    found: list[AuditRow] = []
    for row in db.execute("SELECT * FROM audit_log ORDER BY seq"):
        if row["at"] >= cutoff:
            break
        found.append(_audit(row))
    return found


def audit_delete_through(db: sqlite3.Connection, seq: int) -> int:
    return db.execute("DELETE FROM audit_log WHERE seq <= ?", (seq,)).rowcount


def audit_count(db: sqlite3.Connection) -> int:
    return int(db.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0])


def grants_by_card(db: sqlite3.Connection, card: str) -> Mapping[str, GrantState]:
    rows = db.execute("SELECT handle, state FROM grants WHERE card = ?", (card,)).fetchall()
    return {row["handle"]: GrantState(row["state"]) for row in rows}


def format_stamp(moment: datetime) -> str:
    return format_ts(moment)
