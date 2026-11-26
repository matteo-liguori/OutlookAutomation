"""Weekly stored-bytes history behind the 4, 12 and 26 week targets.

Contract: docs/contracts/10-targets.md (stored bytes at 4, 12, 26 weeks) and the
System line of docs/contracts/06-memory.md. One small snapshot a week is kept in
the `memory.metrics_history` system key; the oldest fall off past 30.
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import cast

from chamberlain.domain.cards import card_size
from chamberlain.domain.timefmt import parse, ts
from chamberlain.memory.cards import CardService
from chamberlain.memory.metrics import tree_bytes
from chamberlain.ports.clock import Clock
from chamberlain.schema.tools import StoredBytesHistory
from chamberlain.store.db import table_names
from chamberlain.store.schema import TABLE_CLASSES, get_state, set_state

HISTORY_KEY = "memory.metrics_history"
HISTORY_SLOTS = 30
SNAPSHOT_INTERVAL_DAYS = 7
CHECKPOINT_WEEKS = (4, 12, 26)


def load(db: sqlite3.Connection) -> list[dict[str, object]]:
    raw = get_state(db, HISTORY_KEY)
    if raw is None:
        return []
    try:
        loaded = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(loaded, list):
        return []
    items = cast(list[object], loaded)
    return [cast(dict[str, object], item) for item in items if isinstance(item, dict)]


def _rows_per_class(db: sqlite3.Connection) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in sorted(table_names(db) & set(TABLE_CLASSES)):
        name = TABLE_CLASSES[table].value
        count = int(db.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0])
        counts[name] = counts.get(name, 0) + count
    return counts


def record_snapshot(db: sqlite3.Connection, clock: Clock, data_root: Path, now: datetime) -> bool:
    """Append this week's snapshot when the last one is a week old. True when written."""
    history = load(db)
    if history:
        last = parse(str(history[-1]["at"]))
        if (now - last).days < SNAPSHOT_INTERVAL_DAYS:
            return False
    cards = CardService(db, clock)
    sizes = [
        card_size(cards.view(str(row["handle"])))
        for row in db.execute("SELECT handle FROM cards WHERE state = 'open'")
    ]
    history.append(
        {
            "at": ts(now),
            "bytes": tree_bytes(data_root),
            "rows": _rows_per_class(db),
            "cards": len(sizes),
            "card_avg": sum(sizes) // len(sizes) if sizes else 0,
        }
    )
    set_state(db, clock, HISTORY_KEY, json.dumps(history[-HISTORY_SLOTS:], separators=(",", ":")))
    return True


def stored_bytes_history(db: sqlite3.Connection) -> StoredBytesHistory:
    """Stored bytes at each checkpoint, counted in weeks from the oldest snapshot kept."""
    history = load(db)
    figures: dict[int, int | None] = dict.fromkeys(CHECKPOINT_WEEKS)
    if history:
        start = parse(str(history[0]["at"]))
        for weeks in CHECKPOINT_WEEKS:
            for item in history:
                if (parse(str(item["at"])) - start).days >= weeks * 7:
                    figures[weeks] = int(str(item["bytes"]))
                    break
    return StoredBytesHistory(
        weeks_recorded=len(history),
        week_4=figures[4],
        week_12=figures[12],
        week_26=figures[26],
    )
