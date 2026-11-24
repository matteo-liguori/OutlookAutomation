"""Restoring a backup: the sweeper runs before the store is used.

Contract: docs/contracts/06-memory.md (M-24). A restore can bring back records
only as old as the backup; everything past its limit is erased again at once.
"""

import os
import shutil
import sqlite3
from pathlib import Path

from chamberlain.app.context import Context, SourceRouter
from chamberlain.app.services import Services
from chamberlain.memory.artifacts import RUN_DIR, Budgets
from chamberlain.memory.sweeper import SweepReport, sweep
from chamberlain.ports.clock import Clock
from chamberlain.store.db import DB_NAME
from chamberlain.store.migrate import open_store


def restore(
    backup_file: Path,
    data_root: Path,
    clock: Clock,
    services: Services,
    *,
    budgets: Budgets | None = None,
) -> tuple[sqlite3.Connection, SweepReport]:
    """Replace the store with a backup, migrate it, then sweep before returning it."""
    run = data_root / RUN_DIR
    run.mkdir(parents=True, exist_ok=True)
    staged = run / "restore.tmp"
    shutil.copyfile(backup_file, staged)
    for suffix in ("", "-wal", "-shm"):
        stale = data_root / f"{DB_NAME}{suffix}"
        if stale.exists():
            stale.unlink()
    os.replace(staged, data_root / DB_NAME)
    db = open_store(data_root, clock)
    ctx = Context(
        db=db, clock=clock, data_root=data_root, sources=SourceRouter(), services=services
    )
    report = sweep(ctx, clock.now(), budgets=budgets)
    return db, report
