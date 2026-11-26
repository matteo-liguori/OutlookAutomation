"""All SQL of the writes track (S-4): grants, known recipients, operations, audit log.

Contract: docs/contracts/09-storage.md. Functions take the connection and
plain strings; timestamps are the stored D-7 text. Nothing here decides
anything: rules live in `authorise.py`, `grants.py` and `engine.py`.
"""

import contextlib
import json
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, fields
from datetime import datetime
from typing import Any, cast

from chamberlain.domain.grant_rules import parse_ts
from chamberlain.domain.grants import GrantKind, GrantState, Requirement, StandingScope
from chamberlain.domain.intents import IntentKind, MailSend
from chamberlain.domain.operations import (
    RELEASING_STATES,
    TERMINAL_STATES,
    OperationState,
    outside_details,
)
from chamberlain.domain.senders import AuthorisedSenders
from chamberlain.store.schema import AUTHORISED_SENDERS_KEY, get_state
from chamberlain.writes.models import GrantRow, OperationRow

_RELEASING = tuple(state.value for state in RELEASING_STATES)
_TERMINAL = tuple(state.value for state in TERMINAL_STATES)


_GRANT_COLUMNS = tuple(f.name for f in fields(GrantRow) if f.name != "scope")
_OPERATION_COLUMNS = tuple(f.name for f in fields(OperationRow))


def _row_values(row: sqlite3.Row, columns: tuple[str, ...]) -> dict[str, Any]:
    """Storage timestamps share one UTC format. Plain columns retain their SQLite values."""
    values = {column: row[column] for column in columns}
    for key, value in values.items():
        if key.endswith("_at") and value is not None:
            values[key] = parse_ts(value)
    return values


def _marks(items: Sequence[object]) -> str:
    return ", ".join("?" for _ in items)


# --- grants ---------------------------------------------------------------------


def _grant(row: sqlite3.Row) -> GrantRow:
    values = _row_values(row, _GRANT_COLUMNS)
    scope = row["scope_json"]
    values.update(
        kind=GrantKind(row["kind"]),
        state=GrantState(row["state"]),
        requirement=Requirement(row["requirement"]),
        scope=None if scope is None else StandingScope.model_validate_json(scope),
    )
    return GrantRow(**values)


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
        # A grant is for its own source, or for any source its scope lists (G-26).
        sql += (
            " AND (source_id = ? OR (scope_json IS NOT NULL AND EXISTS "
            "(SELECT 1 FROM json_each(scope_json, '$.sources') WHERE value = ?)))"
        )
        args += [source_id, source_id]
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


def card_state(db: sqlite3.Connection, card: str) -> str | None:
    row = db.execute("SELECT state FROM cards WHERE handle = ?", (card,)).fetchone()
    return None if row is None else str(row["state"])


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
        f"AND o.state NOT IN ({_marks(_TERMINAL)}))",
        (before, *_TERMINAL),
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
    `owner` list of `authorised_senders` (G-12a). Lower-cased."""
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


def known_recipients_anywhere(db: sqlite3.Connection) -> frozenset[str]:
    """Known recipients of every source (G-12a: a calendar invitation looks across all mail)."""
    rows = db.execute("SELECT address FROM known_recipients").fetchall()
    return frozenset(row["address"] for row in rows)


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
    values = _row_values(row, _OPERATION_COLUMNS)
    values.update(
        kind=IntentKind(row["kind"]),
        state=OperationState(row["state"]),
        client_verified=bool(row["client_verified"]),
        server_verified=bool(row["server_verified"]),
    )
    return OperationRow(**values)


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
    redo_of: str | None = None,
) -> None:
    db.execute(
        "INSERT INTO operations (handle, idempotency_key, kind, source_id, state, card, "
        "session, intent_sha256, intent_json, refs_json, created_at, updated_at, redo_of) "
        "VALUES (?, ?, ?, ?, 'proposed', ?, ?, ?, ?, ?, ?, ?, ?)",
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
            redo_of,
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


def _but(other_than: str | tuple[str, ...]) -> tuple[str, ...]:
    return (other_than,) if isinstance(other_than, str) else other_than


@dataclass(frozen=True)
class GrantUsage:
    reserved_or_spent: dict[str, int]
    recent: dict[str, int]


def grant_usage(
    db: sqlite3.Connection, grants: Iterable[str], since: str, other_than: str | tuple[str, ...]
) -> GrantUsage:
    """Count uses in one snapshot. Reserved uses count toward the daily limit until dispatched."""
    wanted = list(grants)
    if not wanted:
        return GrantUsage({}, {})
    skip = _but(other_than)
    rows = db.execute(
        "SELECT grant, COUNT(*) AS total, "
        "COUNT(*) FILTER (WHERE dispatched_at IS NULL OR dispatched_at >= ?) AS recent "
        f"FROM operations WHERE grant IN ({_marks(wanted)}) AND handle NOT IN ({_marks(skip)}) "
        f"AND state NOT IN ({_marks(_RELEASING)}) AND state <> 'proposed' GROUP BY grant",
        (since, *wanted, *skip, *_RELEASING),
    ).fetchall()
    return GrantUsage(
        {row["grant"]: row["total"] for row in rows},
        {row["grant"]: row["recent"] for row in rows if row["recent"]},
    )


def live_uses(
    db: sqlite3.Connection, grants: Iterable[str], other_than: str | tuple[str, ...]
) -> dict[str, int]:
    return grant_usage(db, grants, "", other_than).reserved_or_spent


def sends_since(db: sqlite3.Connection, source_id: str, since: str, other_than: str) -> int:
    """Sends dispatched since `since`, plus reserved ones not yet dispatched (Q3).

    A send that failed clean is left out: the client proved nothing left (found live,
    2026-11-25: refused attempts used up the hourly cap before anything was sent).
    """
    row = db.execute(
        "SELECT COUNT(*) FROM operations WHERE source_id = ? AND kind = 'mail.send' "
        "AND state <> 'failed_clean' AND handle <> ? AND (dispatched_at >= ? "
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
    )
    return [_operation(row) for row in rows]


def stale_uncertain(db: sqlite3.Connection, before: str) -> list[OperationRow]:
    """Uncertain since `before` with no way left to send them again (owner ruling).

    A mail send whose text the harness holds is never listed: it stays visible until the
    agent or the owner settles it (W-40). Imported sends (no text) are still closed.
    """
    rows = db.execute(
        "SELECT o.* FROM operations o LEFT JOIN grants g ON g.handle = o.grant "
        "WHERE o.state = 'uncertain' AND o.updated_at <= ? "
        "AND NOT (o.kind = 'mail.send' AND o.intent_json IS NOT NULL) "
        "AND (o.intent_json IS NULL "
        "OR o.grant IS NULL OR g.state IN ('spent', 'expired', 'revoked', 'lapsed', 'declined')) "
        "ORDER BY o.created_at, o.handle",
        (before,),
    ).fetchall()
    return [_operation(row) for row in rows]


def sends_due_recheck(db: sqlite3.Connection, before: str) -> list[OperationRow]:
    """Uncertain writes dispatched at or before `before` that have not had their re-check (W-37)."""
    rows = db.execute(
        "SELECT * FROM operations WHERE state = 'uncertain' "
        "AND intent_json IS NOT NULL AND rechecked_at IS NULL AND dispatched_at <= ? "
        "ORDER BY dispatched_at, handle",
        (before,),
    ).fetchall()
    return [_operation(row) for row in rows]


def record_recheck(
    db: sqlite3.Connection, handle: str, *, result: str, looked_json: str, at: str
) -> bool:
    """Store the outcome of the re-check. A later look may overwrite it (a queued copy)."""
    return (
        db.execute(
            "UPDATE operations SET rechecked_at = COALESCE(rechecked_at, ?), recheck_result = ?, "
            "recheck_json = ? WHERE handle = ? AND state = 'uncertain'",
            (at, result, looked_json, handle),
        ).rowcount
        == 1
    )


def resend_of(db: sqlite3.Connection, original: str) -> OperationRow | None:
    """The live resend of `original`, if any (a cancelled proposal does not count, W-38)."""
    row = db.execute(
        "SELECT * FROM operations WHERE redo_of = ? AND state <> 'cancelled' "
        "ORDER BY created_at DESC LIMIT 1",
        (original,),
    ).fetchone()
    return None if row is None else _operation(row)


def operations_for_grant(db: sqlite3.Connection, grant: str, limit: int) -> list[OperationRow]:
    """Operations that took a use of `grant`, newest first (W-47)."""
    rows = db.execute(
        "SELECT * FROM operations WHERE grant = ? ORDER BY created_at DESC, handle LIMIT ?",
        (grant, limit),
    ).fetchall()
    return [_operation(row) for row in rows]


def stale_outside(db: sqlite3.Connection, before: str) -> list[OperationRow]:
    """Outside actions allowed before `before` and still not reported (W-46)."""
    rows = db.execute(
        "SELECT * FROM operations WHERE kind = 'outside.action' AND state = 'authorised' "
        "AND updated_at <= ?",
        (before,),
    ).fetchall()
    return [_operation(row) for row in rows]


def compactable_operations(db: sqlite3.Connection, before: str) -> list[OperationRow]:
    rows = db.execute(
        "SELECT * FROM operations WHERE compacted_at IS NULL AND settled_at IS NOT NULL "
        f"AND settled_at <= ? AND state IN ({_marks(_TERMINAL)})",
        (before, *_TERMINAL),
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
    return AuditRow(**{f.name: row[f.name] for f in fields(AuditRow)})


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


@dataclass(frozen=True)
class GrantAlert:
    handle: str
    state: str
    requirement: str
    card: str | None


@dataclass(frozen=True)
class OperationAlert:
    handle: str
    kind: str
    state: str


@dataclass(frozen=True)
class SendRow:
    """An uncertain mail send whose one 24-hour look found it missing or stuck (W-37)."""

    handle: str
    result: str
    """`missing` or `queued` (waiting in the Outbox or Drafts)."""
    card: str | None
    recipients: tuple[str, ...]
    subject: str
    dispatched_at: datetime
    source: str


_RECHECKED = "(o.kind = 'mail.send' AND o.state = 'uncertain' AND o.recheck_result IS NOT NULL)"


def _alerts(db: sqlite3.Connection, where: str, args: tuple[str, ...] = ()) -> list[GrantAlert]:
    rows = db.execute(f"SELECT handle, state, requirement, card FROM grants WHERE {where}", args)
    return [GrantAlert(r["handle"], r["state"], r["requirement"], r["card"]) for r in rows]


def grants_awaiting_human(db: sqlite3.Connection) -> list[GrantAlert]:
    return _alerts(db, "state = 'proposed' AND requirement = 'human_confirmation'")


def grants_for_review(db: sqlite3.Connection, now: str) -> list[GrantAlert]:
    return _alerts(
        db,
        "kind = 'standing' AND ((state IN ('active', 'suspended') "
        "AND review_at IS NOT NULL AND review_at <= ?) OR state = 'expired')",
        (now,),
    )


def uncertain_operations(db: sqlite3.Connection, before: str) -> list[OperationAlert]:
    """Writes the harness could not settle in 24 hours: the agent is told once (A-4).

    `before` is the dispatch time they must be older than. A write from the old harness (no
    stored request) is never listed: it is closed at import. A send the look found missing or
    stuck is listed by `rechecked_sends` instead.
    """
    rows = db.execute(
        "SELECT o.handle, o.kind, o.state FROM operations o WHERE o.state = 'uncertain' "
        "AND o.intent_json IS NOT NULL AND o.dispatched_at <= ? "
        f"AND NOT {_RECHECKED} ORDER BY o.created_at, o.handle",
        (before,),
    )
    return [OperationAlert(r["handle"], r["kind"], r["state"]) for r in rows]


def rechecked_sends(db: sqlite3.Connection) -> list[SendRow]:
    """Uncertain sends whose 24-hour look found them missing or queued: the agent decides.

    They stay in this list until settled.
    """
    rows = db.execute(
        f"SELECT o.* FROM operations o WHERE {_RECHECKED} "
        "AND o.recheck_result IN ('missing', 'queued') ORDER BY o.created_at, o.handle"
    ).fetchall()
    found: list[SendRow] = []
    for row in rows:
        op = _operation(row)
        intent = op.intent()
        assert isinstance(intent, MailSend)
        found.append(
            SendRow(
                op.handle,
                str(op.recheck_result),
                op.card,
                (*intent.to, *intent.cc, *intent.bcc),
                intent.subject,
                op.dispatched_at or op.created_at,
                op.source_id,
            )
        )
    return found


def sent_recipients(db: sqlite3.Connection) -> list[tuple[str, str]]:
    """(card, intent JSON) of sends made under open cards."""
    rows = db.execute(
        "SELECT o.card AS card, o.intent_json AS intent FROM operations o "
        "JOIN cards c ON c.handle = o.card AND c.state = 'open' "
        "WHERE o.kind = 'mail.send' AND o.intent_json IS NOT NULL"
    )
    return [(r["card"], r["intent"]) for r in rows]


def audit_between(db: sqlite3.Connection, start: str, end: str, limit: int) -> list[AuditRow]:
    """Audit rows from `start` up to (not including) `end`, newest first, excluding tool calls."""
    rows = db.execute(
        "SELECT * FROM audit_log "
        "WHERE at >= ? AND at < ? AND kind != 'tool.call' ORDER BY seq DESC LIMIT ?",
        (start, end, limit),
    )
    return [_audit(r) for r in rows]


def tool_call_summaries(db: sqlite3.Connection, start: str, end: str | None = None) -> list[str]:
    sql = "SELECT summary FROM audit_log WHERE kind = 'tool.call' AND at >= ?"
    params = (start,)
    if end is not None:
        sql += " AND at < ?"
        params = (start, end)
    return [str(row["summary"]) for row in db.execute(sql, params)]


def permission_metric_counts(db: sqlite3.Connection) -> tuple[int, int, int]:
    row = db.execute(
        "SELECT "
        "(SELECT count(*) FROM operations WHERE state IN ('uncertain', 'reconciling')), "
        "(SELECT count(*) FROM grants WHERE state = 'active'), "
        "(SELECT count(*) FROM grants WHERE state = 'proposed' "
        " AND requirement = 'human_confirmation')"
    ).fetchone()
    return int(row[0]), int(row[1]), int(row[2])


@dataclass(frozen=True)
class OutsideLine:
    at: str
    state: str
    where: str
    what: str
    reported: str
    card_title: str | None


def outside_between(db: sqlite3.Connection, start: str, end: str, limit: int) -> list[OutsideLine]:
    """Outside actions allowed between `start` and `end`, with the card they belong to (W-47)."""
    rows = db.execute(
        "SELECT o.created_at, o.state, o.intent_json, o.receipt_json, c.title AS card_title "
        "FROM operations o LEFT JOIN cards c ON c.handle = o.card "
        "WHERE o.kind = 'outside.action' AND o.state <> 'proposed' "
        "AND o.created_at >= ? AND o.created_at < ? ORDER BY o.created_at DESC LIMIT ?",
        (start, end, limit),
    )
    found: list[OutsideLine] = []
    for r in rows:
        where, what, reported = outside_details(r["intent_json"], r["receipt_json"])
        found.append(
            OutsideLine(r["created_at"], r["state"], where, what, reported, r["card_title"])
        )
    return found
