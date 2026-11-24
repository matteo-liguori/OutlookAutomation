"""All SQL of the runs track (S-4). Tables written here: schedules, runs, user_notes.

Contract: docs/contracts/09-storage.md. Tables of other tracks (sessions,
artifacts, artifact_owners) are only read (S-3). Callers wrap writes in
`store.db.transaction`; timestamps arrive as D-7 strings.
"""

import sqlite3

from chamberlain.domain.artifacts import REPORT_OUTLINE_FILENAME
from chamberlain.runs.models import NoteRow, PassKind, RunRow, RunState, ScheduleRow


def _schedule(row: sqlite3.Row) -> ScheduleRow:
    return ScheduleRow(
        name=row["name"],
        kind=PassKind(row["kind"]),
        at_local=row["at_local"],
        weekdays=frozenset(int(day) for day in str(row["weekdays"]).split(",")),
        zone=row["zone"],
        enabled=bool(row["enabled"]),
        presync_minutes=int(row["presync_minutes"]),
    )


def _run(row: sqlite3.Row) -> RunRow:
    return RunRow(
        handle=row["handle"],
        schedule=row["schedule"],
        day=row["day"],
        state=RunState(row["state"]),
        due_at=row["due_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        session=row["session"],
    )


def _note(row: sqlite3.Row) -> NoteRow:
    return NoteRow(id=row["id"], text=row["text"], state=row["state"], created_at=row["created_at"])


# --- schedules -----------------------------------------------------------------------------


def upsert_schedule(
    db: sqlite3.Connection,
    *,
    name: str,
    kind: str,
    at_local: str,
    weekdays: str,
    zone: str,
    enabled: bool,
    presync_minutes: int,
    now: str,
) -> None:
    db.execute(
        "INSERT INTO schedules (name, kind, at_local, weekdays, zone, enabled, presync_minutes, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (name) DO UPDATE "
        "SET kind = excluded.kind, at_local = excluded.at_local, weekdays = excluded.weekdays, "
        "zone = excluded.zone, enabled = excluded.enabled, "
        "presync_minutes = excluded.presync_minutes, updated_at = excluded.updated_at",
        (name, kind, at_local, weekdays, zone, int(enabled), presync_minutes, now, now),
    )


def delete_schedule(db: sqlite3.Connection, name: str) -> int:
    return db.execute("DELETE FROM schedules WHERE name = ?", (name,)).rowcount


def schedules(db: sqlite3.Connection) -> list[ScheduleRow]:
    return [_schedule(row) for row in db.execute("SELECT * FROM schedules ORDER BY name")]


# --- runs ----------------------------------------------------------------------------------


def insert_run(
    db: sqlite3.Connection,
    *,
    handle: str,
    schedule: str,
    day: str,
    state: str,
    due_at: str,
    finished_at: str | None,
) -> bool:
    """Insert one run; False when this schedule already has a run for the day."""
    cursor = db.execute(
        "INSERT OR IGNORE INTO runs (handle, schedule, day, state, due_at, finished_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (handle, schedule, day, state, due_at, finished_at),
    )
    return cursor.rowcount == 1


def run_for_day(db: sqlite3.Connection, schedule: str, day: str) -> RunRow | None:
    row = db.execute(
        "SELECT * FROM runs WHERE schedule = ? AND day = ?", (schedule, day)
    ).fetchone()
    return None if row is None else _run(row)


def get_run(db: sqlite3.Connection, handle: str) -> RunRow | None:
    row = db.execute("SELECT * FROM runs WHERE handle = ?", (handle,)).fetchone()
    return None if row is None else _run(row)


def runs_in_states(db: sqlite3.Connection, states: tuple[str, ...]) -> list[RunRow]:
    marks = ", ".join("?" for _ in states)
    rows = db.execute(f"SELECT * FROM runs WHERE state IN ({marks}) ORDER BY due_at", states)
    return [_run(row) for row in rows]


def recent_runs(db: sqlite3.Connection, limit: int) -> list[RunRow]:
    rows = db.execute("SELECT * FROM runs ORDER BY due_at DESC, handle LIMIT ?", (limit,))
    return [_run(row) for row in rows]


def set_run_state(
    db: sqlite3.Connection,
    handle: str,
    state: str,
    *,
    started_at: str | None = None,
    finished_at: str | None = None,
    session: str | None = None,
) -> None:
    db.execute(
        "UPDATE runs SET state = ?, started_at = COALESCE(?, started_at), "
        "finished_at = COALESCE(?, finished_at), session = COALESCE(?, session) "
        "WHERE handle = ?",
        (state, started_at, finished_at, session, handle),
    )


def delete_runs_before(db: sqlite3.Connection, day: str) -> int:
    return db.execute("DELETE FROM runs WHERE day < ?", (day,)).rowcount


def scheduled_session_between(db: sqlite3.Connection, since: str, before: str) -> str | None:
    """The first scheduled session started in `[since, before)` (a read of attention's table)."""
    row = db.execute(
        "SELECT handle FROM sessions WHERE purpose = 'scheduled' AND started_at >= ? "
        "AND started_at < ? ORDER BY started_at, handle LIMIT 1",
        (since, before),
    ).fetchone()
    return None if row is None else str(row["handle"])


def session_has_ended(db: sqlite3.Connection, handle: str, idle_edge: str) -> bool:
    row = db.execute(
        "SELECT ended_at, last_seen_at FROM sessions WHERE handle = ?", (handle,)
    ).fetchone()
    if row is None:
        return True
    return row["ended_at"] is not None or str(row["last_seen_at"]) <= idle_edge


# --- reports (artifacts held by a run; a read of memory's tables) --------------------------------


def report_handles(db: sqlite3.Connection, run: str) -> list[str]:
    """The run's live stored items that are reports (not the outline the harness keeps)."""
    rows = db.execute(
        "SELECT a.handle FROM artifacts a JOIN artifact_owners o ON o.artifact = a.handle "
        "WHERE o.owner_kind = 'run' AND o.owner = ? AND a.state = 'live' "
        "AND a.filename IS NOT ? ORDER BY a.created_at, a.handle",
        (run, REPORT_OUTLINE_FILENAME),
    )
    return [str(row["handle"]) for row in rows]


def outline_handle(db: sqlite3.Connection, run: str) -> str | None:
    row = db.execute(
        "SELECT a.handle FROM artifacts a JOIN artifact_owners o ON o.artifact = a.handle "
        "WHERE o.owner_kind = 'run' AND o.owner = ? AND a.state = 'live' AND a.filename = ? "
        "ORDER BY a.created_at DESC LIMIT 1",
        (run, REPORT_OUTLINE_FILENAME),
    ).fetchone()
    return None if row is None else str(row["handle"])


# --- notes ---------------------------------------------------------------------------------


def insert_note(db: sqlite3.Connection, text: str, now: str) -> int:
    cursor = db.execute("INSERT INTO user_notes (text, created_at) VALUES (?, ?)", (text, now))
    return int(cursor.lastrowid or 0)


def set_note_state(db: sqlite3.Connection, note: int, state: str) -> int:
    return db.execute("UPDATE user_notes SET state = ? WHERE id = ?", (state, note)).rowcount


def notes_since(db: sqlite3.Connection, since: str) -> list[NoteRow]:
    rows = db.execute("SELECT * FROM user_notes WHERE created_at >= ? ORDER BY id DESC", (since,))
    return [_note(row) for row in rows]


def delete_notes_before(db: sqlite3.Connection, before: str) -> int:
    return db.execute("DELETE FROM user_notes WHERE created_at < ?", (before,)).rowcount
