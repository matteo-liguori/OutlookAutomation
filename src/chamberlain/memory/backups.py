"""Database backups in fixed slots (2 daily, 2 weekly), without any artifact bytes.

Contract: docs/contracts/06-memory.md (M-24 and the lifetime table). A backup is
the SQLite file only: the artifact folder is never copied, so a backup cannot
revive an expired artifact's bytes. `memory.restore.restore` replays the sweeper.
"""

import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from chamberlain.limits import BACKUP_DAILY_SLOTS, BACKUP_WEEKLY_SLOTS
from chamberlain.memory.artifacts import RUN_DIR
from chamberlain.memory.timeutil import ts
from chamberlain.ports.clock import Clock
from chamberlain.store.db import transaction
from chamberlain.store.schema import set_state

BACKUPS_DIR = "backups"
_SLOT = re.compile(r"^(daily|weekly)-(\d+)\.sqlite3$")


def slot_names(now: datetime) -> list[str]:
    """The slots a backup taken at `now` overwrites: one daily, plus one on Mondays."""
    names = [f"daily-{now.toordinal() % BACKUP_DAILY_SLOTS}.sqlite3"]
    if now.isoweekday() == 1:
        names.append(f"weekly-{(now.toordinal() // 7) % BACKUP_WEEKLY_SLOTS}.sqlite3")
    return names


def backup(db: sqlite3.Connection, clock: Clock, data_root: Path) -> list[Path]:
    """Write the store with SQLite's online backup API into this day's slots."""
    now = clock.now()
    folder = data_root / BACKUPS_DIR
    folder.mkdir(parents=True, exist_ok=True)
    (data_root / RUN_DIR).mkdir(parents=True, exist_ok=True)
    temp = data_root / RUN_DIR / "backup.tmp"
    if temp.exists():
        temp.unlink()
    copy = sqlite3.connect(str(temp))
    try:
        db.backup(copy)
    finally:
        copy.close()
    written: list[Path] = []
    for name in slot_names(now):
        target = folder / name
        _replace_copy(temp, target)
        written.append(target)
    temp.unlink()
    with transaction(db):
        set_state(db, clock, "memory.last_backup_at", ts(now))
    return written


def _replace_copy(source: Path, target: Path) -> None:
    staged = target.with_suffix(".staged")
    staged.write_bytes(source.read_bytes())
    os.replace(staged, target)


def rotate(data_root: Path) -> int:
    """Remove slot files beyond the fixed counts. Other backups are left alone."""
    folder = data_root / BACKUPS_DIR
    if not folder.is_dir() or folder.is_symlink():
        return 0
    removed = 0
    for entry in folder.iterdir():
        match = _SLOT.match(entry.name)
        if match is None or not entry.is_file() or entry.is_symlink():
            continue
        limit = BACKUP_DAILY_SLOTS if match.group(1) == "daily" else BACKUP_WEEKLY_SLOTS
        if int(match.group(2)) >= limit:
            entry.unlink()
            removed += 1
    return removed
