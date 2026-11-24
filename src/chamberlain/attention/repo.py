"""All SQL of the attention track (S-4): its own tables, and SELECTs on others'.

Contract: docs/contracts/09-storage.md (S-3, S-4), 07-attention.md (A-8).
Only `sessions`, `events`, `guidance_deliveries`, `source_health`, `mail_watermarks` and
`mail_seen` are written.
"""

import sqlite3
from dataclasses import dataclass

from chamberlain.attention.models import (
    ActionRow,
    BudgetRow,
    CardRow,
    DueRun,
    EventAttrs,
    EventDraft,
    EventRow,
    GrantRow,
    MailBatch,
    OpenNote,
    OperationRow,
    SessionRow,
)
from chamberlain.domain.capabilities import HealthState
from chamberlain.domain.events import EventKind, EventState
from chamberlain.limits import TOTAL_ARTIFACT_BYTES

_EVENT_COLUMNS = (
    "handle, kind, subject, dedupe_key, priority, summary, attrs_json, state, detected_at, "
    "delivered_at, delivered_to, closed_at, quiet_until"
)


def _event(row: sqlite3.Row) -> EventRow:
    return EventRow(
        handle=row["handle"],
        kind=EventKind(row["kind"]),
        subject=row["subject"],
        dedupe_key=row["dedupe_key"],
        priority=row["priority"],
        summary=row["summary"],
        attrs=EventAttrs.model_validate_json(row["attrs_json"]),
        state=EventState(row["state"]),
        detected_at=row["detected_at"],
        delivered_at=row["delivered_at"],
        delivered_to=row["delivered_to"],
        closed_at=row["closed_at"],
        quiet_until=row["quiet_until"],
    )


def _session(row: sqlite3.Row) -> SessionRow:
    return SessionRow(
        handle=row["handle"],
        agent=row["agent"],
        purpose=row["purpose"],
        started_at=row["started_at"],
        last_seen_at=row["last_seen_at"],
        ended_at=row["ended_at"],
    )


# --- sessions ---------------------------------------------------------------


def insert_session(db: sqlite3.Connection, handle: str, agent: str, purpose: str, now: str) -> None:
    db.execute(
        "INSERT INTO sessions (handle, agent, purpose, started_at, last_seen_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (handle, agent, purpose, now, now),
    )


def get_session(db: sqlite3.Connection, handle: str) -> SessionRow | None:
    row = db.execute("SELECT * FROM sessions WHERE handle = ?", (handle,)).fetchone()
    return None if row is None else _session(row)


def touch_session(db: sqlite3.Connection, handle: str, now: str) -> None:
    db.execute(
        "UPDATE sessions SET last_seen_at = ? WHERE handle = ? AND ended_at IS NULL",
        (now, handle),
    )


def active_sessions(db: sqlite3.Connection, agent: str, purpose: str) -> list[SessionRow]:
    rows = db.execute(
        "SELECT * FROM sessions WHERE agent = ? AND purpose = ? AND ended_at IS NULL",
        (agent, purpose),
    )
    return [_session(r) for r in rows]


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


def any_active_session(db: sqlite3.Connection) -> bool:
    row = db.execute("SELECT 1 FROM sessions WHERE ended_at IS NULL LIMIT 1").fetchone()
    return row is not None


def session_is_active(db: sqlite3.Connection, handle: str) -> bool:
    row = db.execute("SELECT 1 FROM sessions WHERE handle = ? AND ended_at IS NULL", (handle,))
    return row.fetchone() is not None


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
    if db.execute("SELECT 1 FROM sources WHERE id = ?", (source,)).fetchone() is None:
        return False
    db.execute(
        "INSERT INTO source_health (source_id, state, checked_at, fresh_as_of) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (source_id) DO UPDATE SET state = excluded.state, "
        "checked_at = excluded.checked_at, fresh_as_of = excluded.fresh_as_of",
        (source, state.value, checked_at, fresh_as_of),
    )
    return True


def read_health(db: sqlite3.Connection) -> dict[str, HealthState]:
    rows = db.execute("SELECT source_id, state FROM source_health")
    return {r["source_id"]: HealthState(r["state"]) for r in rows}


# --- reads of other tracks' tables (A-8) ------------------------------------


def due_actions(db: sqlite3.Connection, now: str) -> list[ActionRow]:
    """Open actions on open cards that are due, or whose snooze has ended."""
    rows = db.execute(
        "SELECT a.handle, a.card, a.due_at, a.snooze_until FROM actions a "
        "JOIN cards c ON c.handle = a.card WHERE a.state = 'open' AND c.state = 'open' "
        "AND ((a.due_at IS NOT NULL AND a.due_at <= ?) "
        "OR (a.snooze_until IS NOT NULL AND a.snooze_until <= ?))",
        (now, now),
    )
    return [ActionRow(r["handle"], r["card"], r["due_at"], r["snooze_until"]) for r in rows]


def cards_needing_review(db: sqlite3.Connection, now: str) -> list[CardRow]:
    rows = db.execute(
        "SELECT handle, title, review_at, over_cap FROM cards WHERE state = 'open' "
        "AND (over_cap = 1 OR (review_at IS NOT NULL AND review_at <= ?))",
        (now,),
    )
    return [CardRow(r["handle"], r["title"], r["review_at"], bool(r["over_cap"])) for r in rows]


def open_cards(db: sqlite3.Connection) -> list[CardRow]:
    rows = db.execute("SELECT handle, title, review_at, over_cap FROM cards WHERE state = 'open'")
    return [CardRow(r["handle"], r["title"], r["review_at"], bool(r["over_cap"])) for r in rows]


def card_links(db: sqlite3.Connection) -> dict[str, str]:
    """Linked message handle and Message-ID to card, for open cards."""
    rows = db.execute(
        "SELECT l.ref, l.internet_message_id, l.card FROM card_links l "
        "JOIN cards c ON c.handle = l.card WHERE c.state = 'open'"
    )
    found: dict[str, str] = {}
    for r in rows:
        found[r["ref"]] = r["card"]
        if r["internet_message_id"]:
            found[r["internet_message_id"]] = r["card"]
    return found


def grants_awaiting_human(db: sqlite3.Connection) -> list[GrantRow]:
    rows = db.execute(
        "SELECT handle, state, requirement, card FROM grants WHERE state = 'proposed' "
        "AND requirement = 'human_confirmation'"
    )
    return [GrantRow(r["handle"], r["state"], r["requirement"], r["card"]) for r in rows]


def grants_for_review(db: sqlite3.Connection, now: str) -> list[GrantRow]:
    rows = db.execute(
        "SELECT handle, state, requirement, card FROM grants WHERE kind = 'standing' AND "
        "((state IN ('active', 'suspended') AND review_at IS NOT NULL AND review_at <= ?) "
        "OR state = 'expired')",
        (now,),
    )
    return [GrantRow(r["handle"], r["state"], r["requirement"], r["card"]) for r in rows]


def uncertain_operations(db: sqlite3.Connection) -> list[OperationRow]:
    rows = db.execute(
        "SELECT handle, kind, state FROM operations WHERE state IN ('uncertain', 'reconciling')"
    )
    return [OperationRow(r["handle"], r["kind"], r["state"]) for r in rows]


def _system(db: sqlite3.Connection, key: str) -> str | None:
    row = db.execute("SELECT value FROM system_state WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row["value"])


def over_budget(db: sqlite3.Connection) -> list[BudgetRow]:
    """Memory writes `memory.over_budget` as "0" or "<scope>:<bytes>/<limit>[,...]"."""
    value = _system(db, "memory.over_budget")
    if value is None or value in ("", "0"):
        return []
    found: list[BudgetRow] = []
    for part in value.split(","):
        scope, _, figures = part.rpartition(":")
        used, _, limit = figures.partition("/")
        if used.isdigit() and limit.isdigit():
            card = scope if scope.startswith("card_") else None
            found.append(BudgetRow(card, int(used), int(limit)))
    if not found:
        found.append(BudgetRow(None, 0, TOTAL_ARTIFACT_BYTES))
    return found


def orphan_batch(db: sqlite3.Connection) -> tuple[str | None, int]:
    """Latest scan with held orphans, and how many are held (M-26)."""
    scan = _system(db, "memory.last_orphan_scan_at")
    if scan is None:
        return None, 0
    row = db.execute(
        "SELECT COUNT(*) AS n FROM quarantine WHERE state = 'held' AND found_at = ?", (scan,)
    ).fetchone()
    return scan, int(row["n"])


def due_runs(db: sqlite3.Connection) -> list[DueRun]:
    """Passes that are due or running; the runs track writes `runs` and `schedules`."""
    rows = db.execute(
        "SELECT r.handle, r.schedule, s.kind, r.day FROM runs r "
        "JOIN schedules s ON s.name = r.schedule WHERE r.state IN ('due', 'running') "
        "ORDER BY r.due_at, r.handle"
    )
    return [DueRun(r["handle"], r["schedule"], r["kind"], r["day"]) for r in rows]


def open_notes(db: sqlite3.Connection, since: str) -> list[OpenNote]:
    rows = db.execute(
        "SELECT id, text FROM user_notes WHERE state = 'open' AND created_at >= ? ORDER BY id",
        (since,),
    )
    return [OpenNote(int(r["id"]), r["text"]) for r in rows]


def update_tag(db: sqlite3.Connection) -> str | None:
    return _system(db, "runs.update_tag") or None


def last_tick(db: sqlite3.Connection) -> str | None:
    return _system(db, "attention.last_tick_at")


# --- mail watermark and the batch awaiting triage (0060) ----------------------------------


@dataclass(frozen=True)
class Watermark:
    covered_through: str
    watching_since: str
    scan_started_at: str | None
    scan_until: str | None


def watermark(db: sqlite3.Connection, source: str) -> Watermark | None:
    row = db.execute(
        "SELECT covered_through, watching_since, scan_started_at, scan_until "
        "FROM mail_watermarks WHERE source_id = ?",
        (source,),
    ).fetchone()
    if row is None:
        return None
    return Watermark(
        row["covered_through"], row["watching_since"], row["scan_started_at"], row["scan_until"]
    )


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
    until: str | None,
    gap: tuple[str, str] | None,
) -> None:
    db.execute(
        "UPDATE mail_watermarks SET covered_through = ?, scan_started_at = ?, scan_until = ?, "
        "gap_from = ?, gap_to = ? WHERE source_id = ?",
        (
            through,
            started,
            until,
            None if gap is None else gap[0],
            None if gap is None else gap[1],
            source,
        ),
    )


def is_seen(db: sqlite3.Connection, source: str, locator: str) -> bool:
    row = db.execute(
        "SELECT 1 FROM mail_seen WHERE source_id = ? AND locator = ?", (source, locator)
    ).fetchone()
    return row is not None


def add_seen(
    db: sqlite3.Connection,
    source: str,
    locator: str,
    handle: str,
    received: str,
    now: str,
    suggested: str | None,
) -> None:
    db.execute(
        "INSERT OR IGNORE INTO mail_seen (source_id, locator, handle, received_at, "
        "first_seen_at, suggested_card) VALUES (?, ?, ?, ?, ?, ?)",
        (source, locator, handle, received, now, suggested),
    )


def prune_seen(db: sqlite3.Connection, source: str, before: str) -> None:
    """Triaged rows older than every future scan window are no longer needed to dedupe."""
    db.execute(
        "DELETE FROM mail_seen WHERE source_id = ? AND triaged = 1 AND received_at < ?",
        (source, before),
    )


def mail_batches(db: sqlite3.Connection) -> list[MailBatch]:
    rows = db.execute(
        "SELECT s.source_id AS source_id, COUNT(*) AS n, MIN(s.seq) AS first_seq, "
        "MIN(s.received_at) AS oldest, SUM(c.handle IS NOT NULL) AS suggested "
        "FROM mail_seen s LEFT JOIN cards c ON c.handle = s.suggested_card AND c.state = 'open' "
        "WHERE s.triaged = 0 GROUP BY s.source_id ORDER BY s.source_id"
    )
    return [
        MailBatch(r["source_id"], r["n"], r["first_seq"], r["oldest"], r["suggested"] or 0)
        for r in rows
    ]


def stamp_delivered(db: sqlite3.Connection, source: str, event: str) -> None:
    """The agent is being shown the batch: remember which rows it covers."""
    db.execute(
        "UPDATE mail_seen SET delivered_event = ? "
        "WHERE source_id = ? AND triaged = 0 AND (delivered_event IS NULL OR "
        "delivered_event NOT IN (SELECT handle FROM events WHERE state IN ('new', 'delivered')))",
        (event, source),
    )


def triage_delivered(db: sqlite3.Connection, source: str, event: str) -> None:
    """The agent handled the batch it was shown; later arrivals stay pending."""
    if (
        db.execute(
            "SELECT 1 FROM mail_seen WHERE source_id = ? AND delivered_event = ?", (source, event)
        ).fetchone()
        is None
    ):
        stamp_delivered(db, source, event)
    db.execute(
        "UPDATE mail_seen SET triaged = 1 WHERE source_id = ? AND delivered_event = ?",
        (source, event),
    )


def mail_gaps(db: sqlite3.Connection) -> list[tuple[str, str, str]]:
    rows = db.execute(
        "SELECT source_id, gap_from, gap_to FROM mail_watermarks WHERE gap_from IS NOT NULL"
    )
    return [(r["source_id"], r["gap_from"], r["gap_to"]) for r in rows]


def linked_message_refs(db: sqlite3.Connection) -> list[tuple[str, str, str]]:
    """(card, source, locator) of messages linked to open cards, newest links first."""
    rows = db.execute(
        "SELECT l.card AS card, r.source_id AS source_id, r.locator AS locator "
        "FROM card_links l JOIN cards c ON c.handle = l.card AND c.state = 'open' "
        "JOIN refs r ON r.handle = l.ref AND r.kind = 'message' ORDER BY l.created_at DESC"
    )
    return [(r["card"], r["source_id"], r["locator"]) for r in rows]


def sent_recipients(db: sqlite3.Connection) -> list[tuple[str, str]]:
    """(card, intent JSON) of sends made under open cards."""
    rows = db.execute(
        "SELECT o.card AS card, o.intent_json AS intent FROM operations o "
        "JOIN cards c ON c.handle = o.card AND c.state = 'open' "
        "WHERE o.kind = 'mail.send' AND o.intent_json IS NOT NULL"
    )
    return [(r["card"], r["intent"]) for r in rows]
