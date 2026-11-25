"""SQL of the pass report, the important appointments and the local events (S-4).

Contract: docs/contracts/09-storage.md. Tables written here: `run_reports`, `local_events`. All
else is read only (S-3): sources, source health, mail watermarks, operations, grants, cards and
actions belong to other tracks. Callers wrap writes in `store.db.transaction`.
"""

import sqlite3
from dataclasses import dataclass

from chamberlain.domain.operations import outside_details

Row = sqlite3.Row


# --- the harness part of a report, kept when the pass settled ------------------------------------


def put_run_report(db: sqlite3.Connection, run: str, composed_at: str, report_json: str) -> None:
    db.execute(
        "INSERT INTO run_reports (run, composed_at, report_json) VALUES (?, ?, ?) "
        "ON CONFLICT (run) DO NOTHING",
        (run, composed_at, report_json),
    )


def get_run_report(db: sqlite3.Connection, run: str) -> str | None:
    row = db.execute("SELECT report_json FROM run_reports WHERE run = ?", (run,)).fetchone()
    return None if row is None else str(row["report_json"])


# --- what the harness writes about a pass: other tracks' tables, read only -----------------------


@dataclass(frozen=True)
class SourceFact:
    id: str
    kind: str
    label: str
    health: str | None
    checked_at: str | None
    fresh_as_of: str | None
    covered_through: str | None
    gap_from: str | None
    gap_to: str | None
    newest_mail: str | None


def source_facts(db: sqlite3.Connection) -> list[SourceFact]:
    rows = db.execute(
        "SELECT s.id, s.kind, s.label, h.state AS health, h.checked_at, h.fresh_as_of, "
        "w.covered_through, w.gap_from, w.gap_to, "
        "(SELECT max(m.received_at) FROM mail_seen m WHERE m.source_id = s.id) AS newest_mail "
        "FROM sources s LEFT JOIN source_health h ON h.source_id = s.id "
        "LEFT JOIN mail_watermarks w ON w.source_id = s.id "
        "WHERE s.state = 'enabled' AND s.kind IN ('mail', 'calendar') ORDER BY s.kind DESC, s.id"
    )
    return [
        SourceFact(
            r["id"],
            r["kind"],
            r["label"],
            r["health"],
            r["checked_at"],
            r["fresh_as_of"],
            r["covered_through"],
            r["gap_from"],
            r["gap_to"],
            r["newest_mail"],
        )
        for r in rows
    ]


def sync_failures(db: sqlite3.Connection) -> dict[str, list[str]]:
    """Mail folders that would not sync at the last try, by source (attention's table)."""
    found: dict[str, list[str]] = {}
    for r in db.execute(
        "SELECT source_id, name FROM mail_sync_folders WHERE failure <> '' ORDER BY source_id, name"
    ):
        found.setdefault(r["source_id"], []).append(r["name"])
    return found


@dataclass(frozen=True)
class FilingFact:
    source: str
    kind: str
    state: str
    verified: bool
    count: int


def filing_facts(db: sqlite3.Connection, since: str) -> list[FilingFact]:
    """Mail filing operations since `since`, by source, kind, state and whether verified."""
    rows = db.execute(
        "SELECT source_id, kind, state, (client_verified OR server_verified) AS verified, "
        "count(*) AS n FROM operations WHERE kind IN ('mail.move', 'mail.trash', "
        "'mail.mark_read') AND created_at >= ? GROUP BY source_id, kind, state, verified "
        "ORDER BY source_id, kind",
        (since,),
    )
    return [
        FilingFact(r["source_id"], r["kind"], r["state"], bool(r["verified"]), int(r["n"]))
        for r in rows
    ]


@dataclass(frozen=True)
class OutsideFact:
    state: str
    where: str
    what: str
    card_title: str | None
    reported: str


def outside_facts(db: sqlite3.Connection, since: str) -> list[OutsideFact]:
    """Outside actions of the pass: what the assistant was allowed to do outside (W-47)."""
    rows = db.execute(
        "SELECT o.state, o.intent_json, o.receipt_json, c.title AS card_title FROM operations o "
        "LEFT JOIN cards c ON c.handle = o.card WHERE o.kind = 'outside.action' "
        "AND o.state <> 'proposed' AND COALESCE(o.settled_at, o.created_at) >= ? "
        "ORDER BY o.created_at, o.handle",
        (since,),
    )
    found: list[OutsideFact] = []
    for r in rows:
        where, what, reported = outside_details(r["intent_json"], r["receipt_json"])
        found.append(OutsideFact(r["state"], where, what, r["card_title"], reported))
    return found


@dataclass(frozen=True)
class ReviewGrant:
    handle: str
    title: str
    source: str
    review_at: str


def grants_due_for_review(db: sqlite3.Connection, now: str) -> list[ReviewGrant]:
    rows = db.execute(
        "SELECT handle, title, source_id, review_at FROM grants WHERE state IN "
        "('active', 'suspended') AND review_at IS NOT NULL AND review_at <= ? "
        "ORDER BY review_at, handle",
        (now,),
    )
    return [ReviewGrant(r["handle"], r["title"], r["source_id"], r["review_at"]) for r in rows]


def count_grants_in_state(db: sqlite3.Connection, state: str) -> int:
    return int(db.execute("SELECT count(*) FROM grants WHERE state = ?", (state,)).fetchone()[0])


# --- important appointments: open actions whose title starts with "Important" -------------------


@dataclass(frozen=True)
class AppointmentRow:
    action: str
    card: str
    card_title: str
    title: str
    start: str
    snooze_until: str | None


def appointment_rows(db: sqlite3.Connection, now: str) -> list[AppointmentRow]:
    """Open dated actions on open cards, marked important, that have not started yet."""
    rows = db.execute(
        "SELECT a.handle, a.card, c.title AS card_title, a.title, a.due_at, a.snooze_until "
        "FROM actions a JOIN cards c ON c.handle = a.card "
        "WHERE a.state = 'open' AND c.state = 'open' AND a.due_at IS NOT NULL AND a.due_at > ? "
        "AND a.important = 1 ORDER BY a.due_at, a.handle",
        (now,),
    )
    return [
        AppointmentRow(
            r["handle"], r["card"], r["card_title"], r["title"], r["due_at"], r["snooze_until"]
        )
        for r in rows
    ]


# --- local events the owner adds in the viewer ---------------------------------------------------


@dataclass(frozen=True)
class LocalEventRow:
    id: str
    title: str
    location: str
    all_day: bool
    start_at: str
    end_at: str
    start_day: str
    end_day: str


def _local(row: sqlite3.Row) -> LocalEventRow:
    return LocalEventRow(
        row["id"],
        row["title"],
        row["location"],
        bool(row["all_day"]),
        row["start_at"],
        row["end_at"],
        row["start_day"],
        row["end_day"],
    )


def get_local_event(db: sqlite3.Connection, event_id: str) -> LocalEventRow | None:
    row = db.execute("SELECT * FROM local_events WHERE id = ?", (event_id,)).fetchone()
    return None if row is None else _local(row)


def insert_local_event(db: sqlite3.Connection, event: LocalEventRow, now: str) -> bool:
    cursor = db.execute(
        "INSERT OR IGNORE INTO local_events (id, title, location, all_day, start_at, end_at, "
        "start_day, end_day, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            event.id,
            event.title,
            event.location,
            int(event.all_day),
            event.start_at,
            event.end_at,
            event.start_day,
            event.end_day,
            now,
        ),
    )
    return cursor.rowcount == 1


def delete_local_event(db: sqlite3.Connection, event_id: str) -> int:
    return db.execute("DELETE FROM local_events WHERE id = ?", (event_id,)).rowcount


def local_events_between(
    db: sqlite3.Connection, first_day: str, until_day: str
) -> list[LocalEventRow]:
    """Events touching the days `[first_day, until_day)`."""
    rows = db.execute(
        "SELECT * FROM local_events WHERE start_day < ? AND end_day >= ? ORDER BY start_at, id",
        (until_day, first_day),
    )
    return [_local(r) for r in rows]


def delete_local_events_before(db: sqlite3.Connection, day: str) -> int:
    return db.execute("DELETE FROM local_events WHERE end_day < ?", (day,)).rowcount
