"""All SQL of the runs track (S-4). Tables written here: schedules, runs, user_notes.

Contract: docs/contracts/09-storage.md. Tables of other tracks (sessions,
artifacts, artifact_owners) are only read (S-3). Callers wrap writes in
`store.db.transaction`; timestamps arrive as D-7 strings.
"""

import sqlite3
from dataclasses import dataclass

from chamberlain.domain.artifacts import REPORT_OUTLINE_FILENAME
from chamberlain.runs.models import NoteRow, PassKind, RunRow, RunState, ScheduleRow
from chamberlain.store.schema import get_state


def _schedule(row: sqlite3.Row) -> ScheduleRow:
    name, kind, at_local, weekdays, zone, enabled, presync, created_at = row
    days = frozenset(int(day) for day in weekdays.split(","))
    return ScheduleRow(
        name, PassKind(kind), at_local, days, zone, bool(enabled), presync, created_at
    )


_RUN_COLUMNS = "handle, schedule, day, state, due_at, started_at, finished_at, session"


def _run(row: sqlite3.Row) -> RunRow:
    handle, schedule, day, state, *rest = row
    return RunRow(handle, schedule, day, RunState(state), *rest)


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
    rows = db.execute(
        "SELECT name, kind, at_local, weekdays, zone, enabled, presync_minutes, created_at "
        "FROM schedules ORDER BY name"
    )
    return [_schedule(row) for row in rows]


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
        f"SELECT {_RUN_COLUMNS} FROM runs WHERE schedule = ? AND day = ?", (schedule, day)
    ).fetchone()
    return None if row is None else _run(row)


def get_run(db: sqlite3.Connection, handle: str) -> RunRow | None:
    row = db.execute(f"SELECT {_RUN_COLUMNS} FROM runs WHERE handle = ?", (handle,)).fetchone()
    return None if row is None else _run(row)


def runs_in_states(db: sqlite3.Connection, states: tuple[str, ...]) -> list[RunRow]:
    marks = ", ".join("?" for _ in states)
    rows = db.execute(
        f"SELECT {_RUN_COLUMNS} FROM runs WHERE state IN ({marks}) ORDER BY due_at, handle", states
    )
    return [_run(row) for row in rows]


def recent_runs(db: sqlite3.Connection, limit: int) -> list[RunRow]:
    rows = db.execute(
        f"SELECT {_RUN_COLUMNS} FROM runs ORDER BY due_at DESC, handle LIMIT ?", (limit,)
    )
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


def live_run(db: sqlite3.Connection) -> RunRow | None:
    """The pass whose report is live: the newest pass worked, once it has settled, until the next
    one starts. None while a pass is being worked, or when the newest report is frozen."""
    row = db.execute(
        f"SELECT {_RUN_COLUMNS} FROM runs WHERE state IN (?, ?, ?) "
        "ORDER BY due_at DESC, handle DESC LIMIT 1",
        (RunState.RUNNING.value, RunState.FINISHED.value, RunState.INCOMPLETE.value),
    ).fetchone()
    if row is None:
        return None
    run = _run(row)
    if run.state is RunState.RUNNING:
        return None
    frozen = db.execute(
        "SELECT 1 FROM run_reports WHERE run = ? AND frozen_at IS NOT NULL", (run.handle,)
    ).fetchone()
    return None if frozen else run


def delete_runs_before(db: sqlite3.Connection, day: str) -> int:
    return db.execute("DELETE FROM runs WHERE day < ?", (day,)).rowcount


def scheduled_session_between(db: sqlite3.Connection, since: str, before: str) -> str | None:
    """The first scheduled session in `[since, before)` that no run owns yet.

    A read of attention's table. One session works one pass, so a session already attached
    to a run is never given to a second one.
    """
    row = db.execute(
        "SELECT handle FROM sessions WHERE purpose = 'scheduled' AND started_at >= ? "
        "AND started_at < ? AND handle NOT IN (SELECT session FROM runs WHERE session IS NOT NULL) "
        "ORDER BY started_at, handle LIMIT 1",
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


def _reports(db: sqlite3.Connection, run: str) -> list[sqlite3.Row]:
    """The run's live stored items that are reports (not the outline the harness keeps)."""
    return db.execute(
        "SELECT a.handle, a.created_at FROM artifacts a JOIN artifact_owners o "
        "ON o.artifact = a.handle WHERE o.owner_kind = 'run' AND o.owner = ? "
        "AND a.state = 'live' AND a.filename IS NOT ? ORDER BY a.created_at, a.handle",
        (run, REPORT_OUTLINE_FILENAME),
    ).fetchall()


def report_handles(db: sqlite3.Connection, run: str) -> list[str]:
    return [str(row["handle"]) for row in _reports(db, run)]


def report_kept_at(db: sqlite3.Connection, run: str) -> str | None:
    """When the run's first report was kept."""
    rows = _reports(db, run)
    return str(rows[0]["created_at"]) if rows else None


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
    rows = db.execute(
        "SELECT id, text, state, created_at FROM user_notes WHERE created_at >= ? ORDER BY id DESC",
        (since,),
    )
    return [NoteRow(*r) for r in rows]


def delete_notes_before(db: sqlite3.Connection, before: str) -> int:
    return db.execute("DELETE FROM user_notes WHERE created_at < ?", (before,)).rowcount


@dataclass(frozen=True)
class DueRun:
    """A scheduled pass that is due or in progress (the `runs` table, runs track)."""

    handle: str
    schedule: str
    kind: str
    day: str
    outline: str | None = None
    """The stored item that tells the agent what to write; set once a session works the pass."""
    state: str = "due"
    has_report: bool = False


def due_runs(db: sqlite3.Connection) -> list[DueRun]:
    """Passes that are due or running, oldest first."""
    kinds = {s.name: s.kind.value for s in schedules(db)}
    return [
        DueRun(
            r.handle,
            r.schedule,
            kinds[r.schedule],
            r.day,
            outline_handle(db, r.handle),
            r.state.value,
            bool(report_handles(db, r.handle)),
        )
        for r in runs_in_states(db, (RunState.DUE.value, RunState.RUNNING.value))
    ]


def open_notes(db: sqlite3.Connection, since: str) -> list[NoteRow]:
    rows = db.execute(
        "SELECT id, text, state, created_at FROM user_notes "
        "WHERE state = 'open' AND created_at >= ? ORDER BY id",
        (since,),
    )
    return [NoteRow(*r) for r in rows]


def update_tag(db: sqlite3.Connection) -> str | None:
    return get_state(db, "runs.update_tag") or None
