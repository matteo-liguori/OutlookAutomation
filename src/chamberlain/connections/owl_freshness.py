"""Durable ownership and crash recovery for Owl's private freshness probe."""

import hashlib
import importlib
import logging
import os
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from chamberlain.domain.capabilities import HealthState, SourceHealth
from chamberlain.domain.timefmt import ts
from chamberlain.ports.clock import Clock
from chamberlain.ports.health import active_health_checks_allowed
from chamberlain.schema.safelog import log_failure
from chamberlain.store.db import connect, transaction


class Probe(Protocol):
    def __call__(self, source: str, token: str, *, cleanup_only: bool = False) -> bool: ...


_LOCK_API: Any = importlib.import_module("msvcrt" if os.name == "nt" else "fcntl")

LOG = logging.getLogger("chamberlain.thunderbird")
FRESH_FOR = timedelta(minutes=10)
LEASE_FOR = timedelta(minutes=10)


def _recent(value: str | None, now: datetime) -> bool:
    if not value:
        return False
    try:
        age = now - datetime.fromisoformat(value)
        return timedelta(0) <= age < FRESH_FOR
    except (ValueError, TypeError):
        return False


def cached(
    source: str, binding: str, clock: Clock, database: Path, health: SourceHealth
) -> SourceHealth:
    """Inspect prior evidence without creating a database, lock, or new probe."""
    if not database.is_file():
        return health
    try:
        db = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            row = db.execute(
                "SELECT calendar_id, token, verified_at FROM owl_freshness WHERE source_id = ?",
                (source,),
            ).fetchone()
        finally:
            db.close()
        if row and row[0] == binding and not row[1] and _recent(row[2], clock.now()):
            return health.model_copy(
                update={"state": HealthState.OK, "fresh_as_of": datetime.fromisoformat(row[2])}
            )
    except (OSError, sqlite3.Error, ValueError):
        return health
    return health


@contextmanager
def _source_lock(database: Path, source: str) -> Generator[bool]:
    """An OS lock fences a paused owner even if its database lease expires."""
    folder = database.parent / "run" / "owl-freshness"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (hashlib.sha256(source.encode("utf-8")).hexdigest() + ".lock")
    with path.open("a+b") as stream:
        stream.seek(0)
        if not stream.read(1):
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                _LOCK_API.locking(stream.fileno(), _LOCK_API.LK_NBLCK, 1)
            else:
                _LOCK_API.flock(stream.fileno(), _LOCK_API.LOCK_EX | _LOCK_API.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            if os.name == "nt":
                stream.seek(0)
                _LOCK_API.locking(stream.fileno(), _LOCK_API.LK_UNLCK, 1)
            else:
                _LOCK_API.flock(stream.fileno(), _LOCK_API.LOCK_UN)


def check(
    probe: Probe,
    source: str,
    binding: str,
    clock: Clock,
    database: Path,
    health: SourceHealth,
) -> SourceHealth:
    """Reserve before dispatch; recover a prior token before ever creating another."""
    try:
        with _source_lock(database, source) as locked:
            return (
                _check_locked(probe, source, binding, clock, database, health) if locked else health
            )
    except (OSError, sqlite3.Error):
        log_failure(LOG, "Could not maintain Owl freshness check for %s", source)
        return health


def _check_locked(
    probe: Probe,
    source: str,
    binding: str,
    clock: Clock,
    database: Path,
    health: SourceHealth,
) -> SourceHealth:
    db = connect(database)
    try:
        now = clock.now()
        if not 0 <= int(now.timestamp()) <= 0xFFFFFFFF:
            return health
        owner = uuid4().hex
        with transaction(db):
            row = db.execute(
                "SELECT * FROM owl_freshness WHERE source_id = ?", (source,)
            ).fetchone()
            if row and row["calendar_id"] != binding:
                if row["token"]:
                    return health  # keep the old marker; never clean in a new binding
                db.execute("DELETE FROM owl_freshness WHERE source_id = ?", (source,))
                row = None
            if row and not row["token"] and _recent(row["verified_at"], now):
                return health.model_copy(
                    update={
                        "state": HealthState.OK,
                        "fresh_as_of": datetime.fromisoformat(row["verified_at"]),
                    }
                )
            if row and row["lease_until"] and row["lease_until"] > ts(now):
                return health
            pending = bool(row and row["token"])
            token = (
                str(row["token"])
                if pending and row is not None
                else f"{int(now.timestamp()):08x}" + uuid4().hex[8:]
            )
            db.execute(
                "INSERT INTO owl_freshness(source_id, calendar_id, token, "
                "lease_owner, lease_until, verified_at) "
                "VALUES (?, ?, ?, ?, ?, NULL) ON CONFLICT(source_id) DO UPDATE SET "
                "token=excluded.token, lease_owner=excluded.lease_owner, "
                "lease_until=excluded.lease_until, verified_at=NULL",
                (source, binding, token, owner, ts(now + LEASE_FOR)),
            )
        try:
            success = probe(source, token, cleanup_only=pending)
        except Exception:
            log_failure(LOG, "Owl freshness check failed for %s", source)
            success = False
        if not success:
            return health
        finished = ts(clock.now())
        with transaction(db):
            updated = db.execute(
                "UPDATE owl_freshness SET token=NULL, lease_owner=NULL, "
                "lease_until=NULL, verified_at=? "
                "WHERE source_id=? AND calendar_id=? AND token=? AND lease_owner=?",
                (None if pending else finished, source, binding, token, owner),
            ).rowcount
        if pending or not updated:
            return health
        return health.model_copy(
            update={"state": HealthState.OK, "fresh_as_of": datetime.fromisoformat(finished)}
        )
    finally:
        db.close()


class OwlFreshnessStore:
    """Composition adapter for the Owl connector's private freshness check."""

    def __init__(self, database: Path, clock: Clock) -> None:
        self.database = database
        self.clock = clock

    def __call__(
        self, source: str, binding: str, health: SourceHealth, probe: Probe
    ) -> SourceHealth:
        if not active_health_checks_allowed():
            return cached(source, binding, self.clock, self.database, health)
        return check(probe, source, binding, self.clock, self.database, health)
