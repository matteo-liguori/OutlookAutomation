"""The `sources` table (writer: app, 09-storage.md). SQL for this package lives here (S-4)."""

import sqlite3

from chamberlain.connections.model import Connections

_CONNECTOR = {
    "thunderbird": "thunderbird",
    "thunderbird_contacts": "thunderbird",
    "owl": "thunderbird",
    "google": "google_calendar",
    "caldav": "caldav",
}


def sync_sources(db: sqlite3.Connection, connections: Connections, now: str) -> int:
    """Make the table mirror the profile. Rows no longer in it become disabled, never deleted.

    Returns the number of rows inserted or changed; a second call with the same profile is 0.
    """
    changed = 0
    wanted: set[str] = set()
    for source_id, source in connections.sources.items():
        wanted.add(source_id)
        values = (
            source_id.partition(":")[0],
            _CONNECTOR[source.kind],
            source.label,
            source.model_dump_json(exclude_none=True),
            "enabled" if source.enabled else "disabled",
        )
        row = db.execute(
            "SELECT kind, connector, label, binding_json, state FROM sources WHERE id = ?",
            (source_id,),
        ).fetchone()
        if row is None:
            db.execute(
                "INSERT INTO sources (id, kind, connector, label, binding_json, state, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (source_id, *values, now, now),
            )
            changed += 1
        elif tuple(row) != values:
            db.execute(
                "UPDATE sources SET kind = ?, connector = ?, label = ?, binding_json = ?, "
                "state = ?, updated_at = ? WHERE id = ?",
                (*values, now, source_id),
            )
            changed += 1
    for row in db.execute("SELECT id FROM sources WHERE state = 'enabled'").fetchall():
        if row["id"] not in wanted:
            db.execute(
                "UPDATE sources SET state = 'disabled', updated_at = ? WHERE id = ?",
                (now, row["id"]),
            )
            changed += 1
    return changed
