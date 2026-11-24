"""The append-only, hash-chained audit log (G-20 to G-23). Implements `AuditLog`.

Each row's hash covers the previous row's hash and the canonical row, so
editing or deleting any row breaks the chain from that point. The newest
`seq:hash` is also kept in `system_state` so removing rows from the tail is
seen too. Monthly pruning leaves a checkpoint that anchors the remaining
chain (G-22).
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import timedelta

from chamberlain.domain.canonical import sha256_text
from chamberlain.domain.grant_rules import format_ts
from chamberlain.limits import AUDIT_DAYS
from chamberlain.ports.clock import Clock
from chamberlain.store.db import transaction
from chamberlain.store.migrate import stamp
from chamberlain.store.schema import get_state, set_state
from chamberlain.writes import repo
from chamberlain.writes.repo import AuditRow

GENESIS = "0" * 64
SUMMARY_MAX = 240
HEAD_KEY = "writes.audit_head"
CHECKPOINT_KEY = "writes.audit_checkpoint_hash"


def row_hash(
    prev_hash: str,
    *,
    seq: int,
    at: str,
    actor: str,
    kind: str,
    subject: str | None,
    summary: str,
    payload_sha256: str | None,
) -> str:
    """`sha256(prev_hash + canonical row)` (G-20)."""
    canonical = json.dumps(
        [seq, at, actor, kind, subject, summary, payload_sha256],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return sha256_text(prev_hash + canonical)


def _split(value: str | None) -> tuple[int, str] | None:
    if not value:
        return None
    seq, _, digest = value.partition(":")
    return (int(seq), digest) if seq.isdigit() and digest else None


class AuditChain:
    """The `AuditLog` service bound to one connection and clock."""

    def __init__(self, db: sqlite3.Connection, clock: Clock) -> None:
        self._db = db
        self._clock = clock

    def append(
        self,
        *,
        actor: str,
        kind: str,
        subject: str | None,
        summary: str,
        payload_sha256: str | None = None,
    ) -> int:
        """Append one row inside the caller's transaction and return its `seq`."""
        db = self._db
        with transaction(db):
            last = repo.audit_last(db)
            if last is not None:
                prev = last.hash
            else:
                checkpoint = _split(get_state(db, CHECKPOINT_KEY))
                prev = checkpoint[1] if checkpoint else GENESIS
            seq = repo.audit_next_seq(db)
            text = " ".join(summary.split())[:SUMMARY_MAX]
            at = stamp(self._clock)
            digest = row_hash(
                prev,
                seq=seq,
                at=at,
                actor=actor,
                kind=kind,
                subject=subject,
                summary=text,
                payload_sha256=payload_sha256,
            )
            repo.audit_insert(
                db, AuditRow(seq, at, actor, kind, subject, text, payload_sha256, prev, digest)
            )
            set_state(db, self._clock, HEAD_KEY, f"{seq}:{digest}")
        return seq


@dataclass(frozen=True)
class ChainReport:
    ok: bool
    rows: int
    first_bad_seq: int | None = None
    problem: str = ""


def verify(db: sqlite3.Connection) -> ChainReport:
    """Walk the chain; report the first broken `seq` (G-21)."""
    rows = repo.audit_rows(db)
    checkpoint = _split(get_state(db, CHECKPOINT_KEY))
    head = _split(get_state(db, HEAD_KEY))
    expected_prev = checkpoint[1] if checkpoint else GENESIS
    expected_seq = checkpoint[0] + 1 if checkpoint else 1
    for row in rows:
        if row.seq != expected_seq:
            return ChainReport(False, len(rows), expected_seq, "a row is missing")
        if row.prev_hash != expected_prev:
            return ChainReport(False, len(rows), row.seq, "the link to the previous row is broken")
        recomputed = row_hash(
            row.prev_hash,
            seq=row.seq,
            at=row.at,
            actor=row.actor,
            kind=row.kind,
            subject=row.subject,
            summary=row.summary,
            payload_sha256=row.payload_sha256,
        )
        if recomputed != row.hash:
            return ChainReport(False, len(rows), row.seq, "a row was edited")
        expected_prev = row.hash
        expected_seq = row.seq + 1
    if head is not None:
        newest = (rows[-1].seq, rows[-1].hash) if rows else checkpoint
        if newest != head:
            return ChainReport(False, len(rows), head[0], "rows are missing at the end")
    return ChainReport(True, len(rows))


def prune(db: sqlite3.Connection, clock: Clock, audit: AuditChain) -> int:
    """Remove whole months older than the audit window, leaving a checkpoint (G-22)."""
    horizon = clock.now() - timedelta(days=AUDIT_DAYS)
    cutoff = format_ts(horizon.replace(day=1, hour=0, minute=0, second=0))
    with transaction(db):
        old = repo.audit_rows_before(db, cutoff)
        last_row = repo.audit_last(db)
        if last_row is not None:
            old = [row for row in old if row.seq < last_row.seq]
        if not old:
            return 0
        removed = old[-1]
        set_state(db, clock, CHECKPOINT_KEY, f"{removed.seq}:{removed.hash}")
        count = repo.audit_delete_through(db, removed.seq)
        audit.append(
            actor="system:tick",
            kind="sweep",
            subject=None,
            summary=f"Pruned {count} audit rows through seq {removed.seq}.",
            payload_sha256=removed.hash,
        )
    return count
