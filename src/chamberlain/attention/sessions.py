"""Sessions: start, resume, supersede, idle end, clean-up, and the wrong-day guard.

Contract: docs/contracts/07-attention.md (A-10) and board ruling R10: a session
belongs to one agent AND one purpose, so a scheduled run and a live chat never
supersede each other. A scheduled session from an earlier local day, resumed
without a live user turn, must stop (port of the old scheduled guard).
"""

import sqlite3
from datetime import date, datetime, timedelta, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from chamberlain.attention import repo
from chamberlain.attention.models import SessionRow
from chamberlain.attention.tuning import SESSIONS_KEEP_ENDED
from chamberlain.domain.handles import HandleKind, new_handle
from chamberlain.limits import SESSION_IDLE_HOURS
from chamberlain.ports.clock import Clock
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.store.db import transaction
from chamberlain.store.migrate import stamp


def _local_day(moment: datetime, zone: tzinfo | None = None) -> date:
    """The calendar day in `zone`; the operating system zone when none is configured."""
    return moment.astimezone(zone).date()


def configured_zone(data_root: Path) -> ZoneInfo | None:
    """The `timezone` of `config/connections.yaml`, the zone schedules are written in.

    Read directly: importing the config loader here would make attention depend on the
    package that builds the app. None when there is no profile or it has no usable
    timezone: the guard then uses the operating system zone, and the tools that need the
    profile report its error.
    """
    path = data_root / "config" / "connections.yaml"
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
        return ZoneInfo(str(document["timezone"]))
    except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError, ZoneInfoNotFoundError):
        return None


def is_stale_scheduled(session: SessionRow, clock: Clock, zone: tzinfo | None = None) -> bool:
    """A scheduled run whose start day, in the schedule's zone, is not today (old guard, R10)."""
    if session.purpose != "scheduled":
        return False
    started = datetime.fromisoformat(session.started_at)
    return _local_day(started, zone) != _local_day(clock.now(), zone)


def is_stale_day(scheduled_day: str, clock: Clock, zone: tzinfo | None = None) -> bool:
    """A scheduled start meant for a day that is not today in the schedule's zone."""
    return scheduled_day != _local_day(clock.now(), zone).isoformat()


def stop_error() -> ChamberlainError:
    """The stop decision for a stale scheduled run."""
    return ChamberlainError(
        ErrorCode.SESSION_STALE,
        "This scheduled run belongs to an earlier day and no user is present. Stop now.",
        fix="Stop. Do not do this run's work. Only a live user turn may start a new session.",
    )


def end_idle(db: sqlite3.Connection, clock: Clock) -> list[str]:
    """End sessions idle for `SESSION_IDLE_HOURS` and clean up old rows (A-10)."""
    cutoff = (clock.now() - timedelta(hours=SESSION_IDLE_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    keep = (clock.now() - SESSIONS_KEEP_ENDED).strftime("%Y-%m-%dT%H:%M:%SZ")
    with transaction(db):
        idle = repo.idle_sessions(db, cutoff)
        for handle in idle:
            repo.end_session(db, handle, stamp(clock))
        repo.purge_ended_sessions(db, keep)
    return idle


def touch(db: sqlite3.Connection, clock: Clock, handle: str) -> None:
    repo.touch_session(db, handle, stamp(clock))


def start(
    db: sqlite3.Connection,
    clock: Clock,
    *,
    agent: str,
    purpose: str,
    resume: str | None,
    zone: tzinfo | None = None,
) -> SessionRow:
    """Open a session, or resume a live one. Supersedes the same agent and purpose."""
    with transaction(db):
        end_idle(db, clock)
        if resume is not None:
            row = repo.get_session(db, resume)
            if row is not None and is_stale_scheduled(row, clock, zone):
                raise stop_error()
            if row is not None and row.ended_at is None:
                repo.touch_session(db, row.handle, stamp(clock))
                return row
        for other in repo.active_sessions(db, agent, purpose):
            repo.end_session(db, other.handle, stamp(clock))
        handle = new_handle(HandleKind.SESSION)
        repo.insert_session(db, handle, agent, purpose, stamp(clock))
        created = repo.get_session(db, handle)
        assert created is not None
        return created
