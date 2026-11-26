"""Queries and updates for attention's tables (S-4).

Contract: docs/contracts/09-storage.md (S-3, S-4), 07-attention.md (A-8).
Only `sessions`, `events`, `guidance_deliveries`, `source_health`, `mail_watermarks`, `mail_sync`,
`mail_sync_folders` and `mail_seen` are written.
"""

import sqlite3
from dataclasses import astuple, dataclass

from chamberlain.attention.models import (
    EventAttrs,
    EventDraft,
    EventRow,
    Held,
    MailBatch,
    MailGap,
    SessionRow,
)
from chamberlain.domain.capabilities import HealthState
from chamberlain.domain.events import EventKind, EventState
from chamberlain.store.schema import get_state

_EVENT_COLUMNS = (
    "handle, kind, subject, dedupe_key, priority, summary, attrs_json, state, detected_at, "
    "delivered_at, delivered_to, closed_at, quiet_until"
)


def _event(row: sqlite3.Row) -> EventRow:
    """`_EVENT_COLUMNS` in the order of `EventRow`'s fields."""
    handle, kind, subject, key, priority, summary, attrs, state, *times = row
    return EventRow(
        handle,
        EventKind(kind),
        subject,
        key,
        priority,
        summary,
        EventAttrs.model_validate_json(attrs),
        EventState(state),
        *times,
    )


_SESSION_COLUMNS = "handle, agent, purpose, started_at, last_seen_at, ended_at"


# --- sessions ---------------------------------------------------------------


def insert_session(db: sqlite3.Connection, handle: str, agent: str, purpose: str, now: str) -> None:
    db.execute(
        "INSERT INTO sessions (handle, agent, purpose, started_at, last_seen_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (handle, agent, purpose, now, now),
    )


def get_session(db: sqlite3.Connection, handle: str) -> SessionRow | None:
    row = db.execute(
        f"SELECT {_SESSION_COLUMNS} FROM sessions WHERE handle = ?", (handle,)
    ).fetchone()
    return None if row is None else SessionRow(*row)


def touch_session(db: sqlite3.Connection, handle: str, now: str) -> None:
    db.execute(
        "UPDATE sessions SET last_seen_at = ? WHERE handle = ? AND ended_at IS NULL",
        (now, handle),
    )


def active_sessions(db: sqlite3.Connection, agent: str, purpose: str) -> list[SessionRow]:
    rows = db.execute(
        f"SELECT {_SESSION_COLUMNS} FROM sessions "
        "WHERE agent = ? AND purpose = ? AND ended_at IS NULL",
        (agent, purpose),
    )
    return [SessionRow(*r) for r in rows]


def idle_sessions(db: sqlite3.Connection, before: str) -> list[str]:
    rows = db.execute(
        "SELECT handle FROM sessions WHERE ended_at IS NULL AND last_seen_at < ?", (before,)
    )
    return [r["handle"] for r in rows]


def end_session(db: sqlite3.Connection, handle: str, now: str) -> None:
    """End a session and drop its guidance ledger (A-10). The row stays for memory's reaper."""
    db.execute(
        "UPDATE sessions SET ended_at = ? WHERE handle = ? AND ended_at IS NULL", (now, handle)
    )
    db.execute("DELETE FROM guidance_deliveries WHERE session = ?", (handle,))


def purge_ended_sessions(db: sqlite3.Connection, before: str) -> int:
    cursor = db.execute(
        "DELETE FROM sessions WHERE ended_at IS NOT NULL AND ended_at < ?", (before,)
    )
    return cursor.rowcount


def scheduled_pass_in_progress(db: sqlite3.Connection) -> bool:
    """True while a scheduled session is still working its pass (a chat never counts).

    A scheduled session whose run already has a report (or ended without one) no longer
    holds the wake signal, so the next due pass can start without waiting for it to idle out.
    """
    row = db.execute(
        "SELECT 1 FROM sessions WHERE purpose = 'scheduled' AND ended_at IS NULL "
        "AND handle NOT IN (SELECT session FROM runs WHERE session IS NOT NULL "
        "AND state IN ('finished', 'incomplete', 'missed')) LIMIT 1"
    ).fetchone()
    return row is not None


# --- guidance ledger --------------------------------------------------------


def delivered_versions(db: sqlite3.Connection, session: str) -> dict[tuple[str, str], int]:
    rows = db.execute(
        "SELECT rule_id, scope_key, version FROM guidance_deliveries WHERE session = ?", (session,)
    )
    return {(r["rule_id"], r["scope_key"]): r["version"] for r in rows}


def record_delivery(
    db: sqlite3.Connection, session: str, rule_id: str, version: int, scope_key: str, now: str
) -> None:
    db.execute(
        "INSERT INTO guidance_deliveries (session, rule_id, version, scope_key, delivered_at) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT (session, rule_id, scope_key) "
        "DO UPDATE SET version = excluded.version, delivered_at = excluded.delivered_at",
        (session, rule_id, version, scope_key, now),
    )


# --- events -----------------------------------------------------------------


def live_events(db: sqlite3.Connection) -> list[EventRow]:
    rows = db.execute(f"SELECT {_EVENT_COLUMNS} FROM events WHERE state IN ('new', 'delivered')")
    return [_event(r) for r in rows]


def handled_by_key(db: sqlite3.Connection) -> dict[str, EventRow]:
    rows = db.execute(
        f"SELECT {_EVENT_COLUMNS} FROM events WHERE state = 'handled' ORDER BY closed_at"
    )
    return {r["dedupe_key"]: _event(r) for r in rows}


def get_event(db: sqlite3.Connection, handle: str) -> EventRow | None:
    row = db.execute(f"SELECT {_EVENT_COLUMNS} FROM events WHERE handle = ?", (handle,)).fetchone()
    return None if row is None else _event(row)


def events_by_subject(db: sqlite3.Connection, subjects: list[str]) -> list[EventRow]:
    if not subjects:
        return []
    marks = ",".join("?" * len(subjects))
    rows = db.execute(
        f"SELECT {_EVENT_COLUMNS} FROM events WHERE state IN ('new', 'delivered') "
        f"AND subject IN ({marks})",
        subjects,
    )
    return [_event(r) for r in rows]


def events_by_handles(db: sqlite3.Connection, handles: list[str]) -> list[EventRow]:
    if not handles:
        return []
    marks = ",".join("?" * len(handles))
    rows = db.execute(f"SELECT {_EVENT_COLUMNS} FROM events WHERE handle IN ({marks})", handles)
    return [_event(r) for r in rows]


def insert_event(db: sqlite3.Connection, handle: str, draft: EventDraft, now: str) -> None:
    db.execute(
        "INSERT INTO events (handle, kind, subject, dedupe_key, priority, summary, attrs_json, "
        "state, detected_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'new', ?)",
        (
            handle,
            draft.kind.value,
            draft.subject,
            draft.dedupe_key,
            draft.priority,
            draft.summary,
            draft.attrs.model_dump_json(exclude_none=True),
            now,
        ),
    )


def refresh_event(db: sqlite3.Connection, handle: str, draft: EventDraft) -> None:
    db.execute(
        "UPDATE events SET priority = ?, summary = ?, attrs_json = ? WHERE handle = ?",
        (draft.priority, draft.summary, draft.attrs.model_dump_json(exclude_none=True), handle),
    )


def expire_event(db: sqlite3.Connection, handle: str, now: str) -> None:
    db.execute(
        "UPDATE events SET state = 'expired', closed_at = ? WHERE handle = ? "
        "AND state IN ('new', 'delivered')",
        (now, handle),
    )


def delete_event(db: sqlite3.Connection, handle: str) -> None:
    db.execute("DELETE FROM events WHERE handle = ?", (handle,))


def mark_handled(db: sqlite3.Connection, handle: str, now: str, quiet_until: str | None) -> None:
    db.execute(
        "UPDATE events SET state = 'handled', closed_at = ?, quiet_until = ? WHERE handle = ? "
        "AND state IN ('new', 'delivered')",
        (now, quiet_until, handle),
    )


def mark_delivered(db: sqlite3.Connection, handle: str, session: str, now: str) -> None:
    db.execute(
        "UPDATE events SET state = 'delivered', delivered_at = ?, delivered_to = ? "
        "WHERE handle = ? AND state IN ('new', 'delivered')",
        (now, session, handle),
    )


def candidates(
    db: sqlite3.Connection, session: str, limit: int, *, min_priority: int = 0
) -> list[EventRow]:
    """Live events this session has not been shown, best first (A-9, A-15)."""
    rows = db.execute(
        f"SELECT {_EVENT_COLUMNS} FROM events WHERE state IN ('new', 'delivered') "
        "AND (delivered_to IS NULL OR delivered_to <> ?) AND priority >= ? "
        "ORDER BY priority DESC, detected_at, dedupe_key LIMIT ?",
        (session, min_priority, limit),
    )
    return [_event(r) for r in rows]


def count_candidates(db: sqlite3.Connection, session: str) -> int:
    row = db.execute(
        "SELECT COUNT(*) AS n FROM events WHERE state IN ('new', 'delivered') "
        "AND (delivered_to IS NULL OR delivered_to <> ?)",
        (session,),
    ).fetchone()
    return int(row["n"])


def count_undelivered(db: sqlite3.Connection) -> int:
    row = db.execute("SELECT COUNT(*) AS n FROM events WHERE state = 'new'").fetchone()
    return int(row["n"])


def trim_terminal(db: sqlite3.Connection, before: str) -> None:
    """Drop old terminal rows. A handled row that still suppresses (A-3) is kept."""
    db.execute("DELETE FROM events WHERE state = 'expired' AND closed_at < ?", (before,))
    db.execute(
        "DELETE FROM events WHERE state = 'handled' AND quiet_until IS NOT NULL "
        "AND quiet_until < ?",
        (before,),
    )


# --- source health ----------------------------------------------------------


def write_health(
    db: sqlite3.Connection,
    source: str,
    state: HealthState,
    checked_at: str,
    fresh_as_of: str | None,
) -> bool:
    """Record health for a source that has a `sources` row; False when it has none."""
    return bool(
        db.execute(
            "INSERT INTO source_health (source_id, state, checked_at, fresh_as_of) "
            "SELECT id, ?, ?, ? FROM sources WHERE id = ? "
            "ON CONFLICT (source_id) DO UPDATE SET state = excluded.state, "
            "checked_at = excluded.checked_at, fresh_as_of = excluded.fresh_as_of",
            (state.value, checked_at, fresh_as_of, source),
        ).rowcount
    )


def read_health(db: sqlite3.Connection) -> dict[str, HealthState]:
    rows = db.execute("SELECT source_id, state FROM source_health")
    return {r["source_id"]: HealthState(r["state"]) for r in rows}


def last_tick(db: sqlite3.Connection) -> str | None:
    return get_state(db, "attention.last_tick_at")


# --- mail watermark and the batch awaiting triage (0060, 0120) ----------------------------


@dataclass(frozen=True)
class Watermark:
    covered_through: str
    watching_since: str
    scan_started_at: str | None
    """Set while a scan is unfinished: the instant it began, which becomes the watermark."""
    scan_from: str | None
    """Oldest-first frontier of an unfinished scan: mail dated before it has been seen."""
    scan_width: int | None
    """Seconds per slice of an unfinished scan that had to be narrowed."""
    held: Held | None


def watermark(db: sqlite3.Connection, source: str) -> Watermark | None:
    row = db.execute(
        "SELECT covered_through, watching_since, scan_started_at, scan_from, scan_width, "
        "held_from, held_to, held_state, held_estimate, held_width "
        "FROM mail_watermarks WHERE source_id = ?",
        (source,),
    ).fetchone()
    if row is None:
        return None
    *scan, held_from, held_to, held_state, held_estimate, held_width = row
    held = (
        None
        if held_from is None
        else Held(held_from, held_to, held_state, held_estimate, held_width)
    )
    return Watermark(scan[0], scan[1], scan[2], scan[3], scan[4], held)


def begin_watching(db: sqlite3.Connection, source: str, through: str, now: str) -> None:
    db.execute(
        "INSERT OR IGNORE INTO mail_watermarks (source_id, covered_through, watching_since) "
        "VALUES (?, ?, ?)",
        (source, through, now),
    )


def save_scan(
    db: sqlite3.Connection,
    source: str,
    *,
    through: str,
    started: str | None,
    frontier: str | None,
    width: int | None,
    gap: tuple[str, str] | None,
) -> None:
    db.execute(
        "UPDATE mail_watermarks SET covered_through = ?, scan_started_at = ?, scan_from = ?, "
        "scan_width = ?, gap_from = ?, gap_to = ? WHERE source_id = ?",
        (through, started, frontier, width, *(gap or (None, None)), source),
    )


def save_held(db: sqlite3.Connection, source: str, held: Held | None) -> None:
    db.execute(
        "UPDATE mail_watermarks SET held_from = ?, held_to = ?, held_state = ?, "
        "held_estimate = ?, held_width = ? WHERE source_id = ?",
        (*(astuple(held) if held else (None,) * 5), source),
    )


def held_state(db: sqlite3.Connection, source: str, state: str) -> None:
    db.execute("UPDATE mail_watermarks SET held_state = ? WHERE source_id = ?", (state, source))


def seen_keys(db: sqlite3.Connection, source: str, keys: list[str]) -> set[str]:
    found: set[str] = set()
    for start in range(0, len(keys), 200):
        chunk = keys[start : start + 200]
        marks = ",".join("?" * len(chunk))
        rows = db.execute(
            f"SELECT message_key FROM mail_seen WHERE source_id = ? AND message_key IN ({marks})",
            (source, *chunk),
        )
        found.update(r["message_key"] for r in rows)
    return found


def add_seen(
    db: sqlite3.Connection,
    source: str,
    key: str,
    locator: str,
    handle: str,
    received: str,
    now: str,
    suggested: str | None,
    *,
    sent: bool,
) -> None:
    """One row per message. Sent mail with no suggested card needs no triage."""
    db.execute(
        "INSERT OR IGNORE INTO mail_seen (source_id, locator, handle, received_at, "
        "first_seen_at, suggested_card, message_key, sent, triaged) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            source,
            locator,
            handle,
            received,
            now,
            suggested,
            key,
            int(sent),
            int(sent and suggested is None),
        ),
    )


def prune_seen(db: sqlite3.Connection, source: str, before: str, sent_before: str) -> None:
    """Rows older than every future scan window are no longer needed to dedupe.

    Only triaged rows go. Waiting mail is kept, except a sent line nobody handled for a week:
    that age is counted from when it was first seen, not from its date, which may be days old.
    """
    db.execute(
        "DELETE FROM mail_seen WHERE source_id = ? AND received_at < ? "
        "AND (triaged = 1 OR (sent = 1 AND first_seen_at < ?))",
        (source, before, sent_before),
    )


def mail_batches(db: sqlite3.Connection) -> list[MailBatch]:
    rows = db.execute(
        "SELECT s.source_id, COUNT(*), MIN(s.seq), MIN(s.received_at), "
        "COALESCE(SUM(c.handle IS NOT NULL), 0), s.sent "
        "FROM mail_seen s LEFT JOIN cards c ON c.handle = s.suggested_card AND c.state = 'open' "
        "WHERE s.triaged = 0 GROUP BY s.source_id, s.sent ORDER BY s.source_id, s.sent"
    )
    return [
        MailBatch(source, n, seq, oldest, suggested, bool(sent))
        for source, n, seq, oldest, suggested, sent in rows
    ]


def stamp_delivered(
    db: sqlite3.Connection, source: str, event: str, *, sent: bool, session: str | None = None
) -> None:
    """The agent is being shown the batch: remember which rows it covers, and to whom."""
    db.execute(
        "UPDATE mail_seen SET delivered_event = ?, delivered_session = ? "
        "WHERE source_id = ? AND sent = ? AND triaged = 0 AND (delivered_event IS NULL OR "
        "delivered_event NOT IN (SELECT handle FROM events WHERE state IN ('new', 'delivered')))",
        (event, session, source, int(sent)),
    )


def triage_delivered(db: sqlite3.Connection, source: str, event: str, *, sent: bool) -> None:
    """The agent handled the batch it was shown; later arrivals stay pending."""
    if (
        db.execute(
            "SELECT 1 FROM mail_seen WHERE source_id = ? AND delivered_event = ?", (source, event)
        ).fetchone()
        is None
    ):
        stamp_delivered(db, source, event, sent=sent)
    db.execute(
        "UPDATE mail_seen SET triaged = 1 WHERE source_id = ? AND delivered_event = ?",
        (source, event),
    )


def mail_gaps(db: sqlite3.Connection) -> list[MailGap]:
    rows = db.execute(
        "SELECT source_id, gap_from, gap_to FROM mail_watermarks WHERE gap_from IS NOT NULL"
    )
    return [MailGap(*r) for r in rows]


def mail_held(db: sqlite3.Connection) -> list[tuple[str, Held]]:
    rows = db.execute(
        "SELECT source_id, held_from, held_to, held_state, held_estimate, held_width "
        "FROM mail_watermarks WHERE held_from IS NOT NULL ORDER BY source_id"
    )
    return [(r[0], Held(*r[1:])) for r in rows]


# --- mail folder sync (0150) ---------------------------------------------------------------


@dataclass(frozen=True)
class SyncFolder:
    """What the harness knows about one folder the scan reads."""

    folder_id: str
    name: str
    role: str
    synced_at: str | None
    """When the client last said this folder was in sync; None if it never did."""
    attempted_at: str
    failure: str
    """Empty, or why the last attempt did not finish."""
    catchup_from: str | None
    """Set when the folder failed to sync: mail in it dated from here on is read on its own once
    it syncs again, because the scan's watermark may have moved past it meanwhile."""


def sync_folder_rows(db: sqlite3.Connection, source: str) -> list[SyncFolder]:
    rows = db.execute(
        "SELECT folder_id, name, role, synced_at, attempted_at, failure, catchup_from "
        "FROM mail_sync_folders WHERE source_id = ? ORDER BY folder_id",
        (source,),
    )
    return [SyncFolder(*r) for r in rows]


def save_sync_folder(db: sqlite3.Connection, source: str, row: SyncFolder) -> None:
    db.execute(
        "INSERT INTO mail_sync_folders (source_id, folder_id, name, role, synced_at, "
        "attempted_at, failure, catchup_from) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (source_id, folder_id) DO UPDATE SET name = excluded.name, "
        "role = excluded.role, synced_at = excluded.synced_at, "
        "attempted_at = excluded.attempted_at, failure = excluded.failure, "
        "catchup_from = excluded.catchup_from",
        (source, *astuple(row)),
    )


def drop_sync_folders(db: sqlite3.Connection, source: str, keep: list[str]) -> None:
    """Forget folders the scan no longer reads (deleted, or now a skipped role)."""
    marks = ",".join("?" for _ in keep)
    db.execute(
        f"DELETE FROM mail_sync_folders WHERE source_id = ? AND folder_id NOT IN ({marks})",
        (source, *keep),
    )


def set_catchup(
    db: sqlite3.Connection, source: str, folder_id: str, catchup_from: str | None
) -> None:
    db.execute(
        "UPDATE mail_sync_folders SET catchup_from = ? WHERE source_id = ? AND folder_id = ?",
        (catchup_from, source, folder_id),
    )


def sync_state(db: sqlite3.Connection, source: str) -> tuple[str | None, str] | None:
    """`(complete_at, attempted_at)`: the last time every folder was in sync, and the last try."""
    row = db.execute(
        "SELECT complete_at, attempted_at FROM mail_sync WHERE source_id = ?", (source,)
    ).fetchone()
    return None if row is None else (row["complete_at"], row["attempted_at"])


def save_sync_state(
    db: sqlite3.Connection, source: str, complete_at: str | None, attempted_at: str
) -> None:
    db.execute(
        "INSERT INTO mail_sync (source_id, complete_at, attempted_at) VALUES (?, ?, ?) "
        "ON CONFLICT (source_id) DO UPDATE SET complete_at = excluded.complete_at, "
        "attempted_at = excluded.attempted_at",
        (source, complete_at, attempted_at),
    )


def sync_failures(db: sqlite3.Connection) -> list[tuple[str, SyncFolder]]:
    """Every folder whose last sync did not finish, by source."""
    rows = db.execute(
        "SELECT source_id, folder_id, name, role, synced_at, attempted_at, failure, "
        "catchup_from FROM mail_sync_folders WHERE failure <> '' ORDER BY source_id, folder_id"
    )
    return [(r[0], SyncFolder(*r[1:])) for r in rows]


def count_live_events(db: sqlite3.Connection) -> int:
    return int(
        db.execute("SELECT count(*) FROM events WHERE state IN ('new', 'delivered')").fetchone()[0]
    )
