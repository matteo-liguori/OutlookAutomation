"""Plain helpers for the memory tests (imported by name; conftest adds this folder to the path)."""

import re
import sqlite3
from pathlib import Path

from chamberlain.app.context import Context, Services, SourceRouter
from chamberlain.domain.artifacts import ArtifactKind
from chamberlain.domain.timefmt import minus, ts
from chamberlain.memory.artifacts import ArtifactService
from chamberlain.ports.clock import ManualClock
from chamberlain.store.db import MEMORY, connect
from chamberlain.store.migrate import migrate
from chamberlain.writes.models import WritePolicy

SESSION = "ses_aaaaaaaa0001"
OTHER_SESSION = "ses_aaaaaaaa0002"
SHA = "1" * 64


def add_session(db: sqlite3.Connection, clock: ManualClock, handle: str = SESSION) -> str:
    stamp = ts(clock.now())
    db.execute(
        "INSERT INTO sessions (handle, purpose, started_at, last_seen_at) "
        "VALUES (?, 'interactive', ?, ?)",
        (handle, stamp, stamp),
    )
    return handle


def add_operation(
    db: sqlite3.Connection, clock: ManualClock, handle: str, state: str = "authorised"
) -> str:
    stamp = ts(clock.now())
    db.execute(
        "INSERT INTO operations (handle, idempotency_key, kind, source_id, state, intent_sha256, "
        "created_at, updated_at) VALUES (?, ?, 'mail.send', 'mail:test', ?, ?, ?, ?)",
        (handle, handle, state, SHA, stamp, stamp),
    )
    return handle


def settle_operation(
    db: sqlite3.Connection, clock: ManualClock, handle: str, state: str = "confirmed"
) -> None:
    stamp = ts(clock.now())
    db.execute(
        "UPDATE operations SET state = ?, settled_at = ?, updated_at = ? WHERE handle = ?",
        (state, stamp, stamp, handle),
    )


def add_grant(
    db: sqlite3.Connection, clock: ManualClock, handle: str, state: str = "active"
) -> str:
    stamp = ts(clock.now())
    db.execute(
        "INSERT INTO grants (handle, kind, state, source_id, title, requirement, content_sha256, "
        "proposed_at) VALUES (?, 'one_off', ?, 'mail:test', 'Send a note', 'agent_evidence', ?, ?)",
        (handle, state, SHA, stamp),
    )
    return handle


def keep_text(
    store: ArtifactService,
    owner: str,
    text: str = "hello",
    *,
    filename: str | None = None,
    expires_in_hours: int | None = None,
) -> str:
    kept = store.keep_bytes(
        data=text.encode(),
        kind=ArtifactKind.TEXT,
        owner=owner,
        reason="test",
        filename=filename,
        expires_in_hours=expires_in_hours,
    )
    return kept.handle


def blobs(data_root: Path) -> set[str]:
    folder = data_root / "artifacts"
    if not folder.exists():
        return set()
    return {p.name for p in folder.rglob("*") if p.is_file()}


def table_dump(db: sqlite3.Connection) -> dict[str, list[tuple[object, ...]]]:
    """Every content table as plain tuples, for change detection across sweeps."""
    names = [
        str(r["name"])
        for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
            "AND name NOT IN ('system_state', 'schema_migrations', 'audit_log') ORDER BY name"
        )
    ]
    return {
        name: [tuple(row) for row in db.execute(f"SELECT * FROM {name} ORDER BY 1, 2")]
        for name in names
    }


_HANDLE = re.compile(r"\b(card|act|note|evd|art|grant|op|evt|ses|msg|cev|con|cur)_[a-z0-9]{8,32}")


def normalise(text: str) -> str:
    """Replace random handles by stable numbered ones, for golden comparison."""
    seen: dict[str, str] = {}

    def swap(match: re.Match[str]) -> str:
        found = match.group(0)
        if found not in seen:
            seen[found] = f"{match.group(1)}_#{len(seen) + 1}"
        return seen[found]

    return _HANDLE.sub(swap, text)


def ago(clock: ManualClock, **kw: float) -> str:
    return minus(clock.now(), **kw)


def make_context(root: Path, clock: ManualClock) -> Context:
    """A fresh in-memory store over a data root, for tests that build their own."""
    db = connect(MEMORY)
    migrate(db, clock, backup_dir=None)
    return Context(
        db=db,
        clock=clock,
        data_root=root,
        sources=SourceRouter(),
        services=Services.bind(db, clock, root, policy=WritePolicy()),
    )
