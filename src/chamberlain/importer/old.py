"""Reading an old installation, strictly read-only.

The old harness takes backups and prunes at start-up, so it is never run and its code is
never imported. The database is opened by URI with `immutable=1`, which makes SQLite
create nothing (a plain `mode=ro` open of a WAL database creates `-shm` and `-wal` files
next to it). `immutable=1` ignores a write-ahead log, so a non-empty `-wal` is refused:
the old harness is still open or crashed, and the owner must work from a copy.
"""

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from chamberlain.schema.errors import ChamberlainError, ErrorCode

SUPPORTED_SCHEMA = 9
DB_RELATIVE = Path("monitor") / "monitor.db"
PROFILE_RELATIVE = Path("monitor") / "profile.json"
CUSTOMISATIONS = "customisations.yaml"
SKIPPED_TABLES = (
    "runs",
    "sources",
    "candidates",
    "checkpoints",
    "issues",
    "roundup",
    "reported_cases",
    "calendar_state",
    "metrics",
    "message_reviews",
    "improvements",
    "reflections",
    "calendar_reports",
    "message_versions",
    "retention_activity",
    "maintenance_files",
    "mutations",
    "decisions",
    "context",
)
"""Old tables whose rows are deliberately left behind (rebuild plan, "Not imported")."""

Json = dict[str, Any]


@dataclass(frozen=True)
class OldCase:
    id: str
    title: str
    position: str
    status: str
    updated_at: str
    details: Json


@dataclass(frozen=True)
class OldAction:
    id: str
    case_id: str
    status: str
    next_review: str | None
    details: Json


@dataclass(frozen=True)
class OldEvidence:
    id: str
    metadata: Json
    body: str


@dataclass(frozen=True)
class OldApproval:
    id: str
    kind: str
    """`one-off` or `standing`."""
    case_id: str | None
    details: Json


@dataclass(frozen=True)
class OldJournal:
    key: str
    state: Json


@dataclass
class OldInstallation:
    root: Path
    schema_version: int
    profile: Json
    customisations: Json
    cases: list[OldCase] = field(default_factory=list[OldCase])
    actions: list[OldAction] = field(default_factory=list[OldAction])
    evidence: dict[str, OldEvidence] = field(default_factory=dict[str, OldEvidence])
    approvals: list[OldApproval] = field(default_factory=list[OldApproval])
    journals: list[OldJournal] = field(default_factory=list[OldJournal])
    skipped_rows: dict[str, int] = field(default_factory=dict[str, int])


def _refuse(message: str, fix: str = "") -> ChamberlainError:
    return ChamberlainError(
        ErrorCode.CONFIG_INVALID,
        message,
        fix=fix or "Point the import at a copy of the old data folder that the old harness closed.",
    )


def _json(text: str, what: str) -> Json:
    try:
        value: Any = json.loads(text)
    except ValueError:
        raise _refuse(f"{what} is not valid JSON.") from None
    if not isinstance(value, dict):
        raise _refuse(f"{what} is not a JSON object.")
    return value  # pyright: ignore[reportUnknownVariableType]


def _check_wal(db_path: Path) -> None:
    wal = db_path.with_name(db_path.name + "-wal")
    if wal.is_file() and wal.stat().st_size > 0:
        raise _refuse(
            "The old database has unmerged write-ahead log content.",
            "Close the old harness and copy the whole old data folder, then import the copy.",
        )


def open_readonly(db_path: Path) -> sqlite3.Connection:
    """The old database, opened so that nothing next to it can change."""
    if not db_path.is_file():
        raise _refuse("There is no old database in that folder.")
    _check_wal(db_path)
    uri = db_path.resolve().as_uri() + "?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _load_profile(root: Path) -> Json:
    path = root / PROFILE_RELATIVE
    if not path.is_file():
        raise _refuse("There is no profile.json in the old folder.")
    return _json(path.read_text(encoding="utf-8-sig"), "profile.json")


def _load_customisations(root: Path) -> Json:
    path = root / CUSTOMISATIONS
    if not path.is_file():
        return {}
    try:
        value: Any = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except yaml.YAMLError:
        raise _refuse("customisations.yaml cannot be read.") from None
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise _refuse("customisations.yaml is not a mapping.")
    return value  # pyright: ignore[reportUnknownVariableType]


def _count(db: sqlite3.Connection, table: str) -> int:
    try:
        return int(db.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
    except sqlite3.Error:
        return 0


def read_installation(root: Path) -> OldInstallation:
    """Everything the importer needs from the old root. Nothing is written anywhere."""
    db = open_readonly(root / DB_RELATIVE)
    try:
        version = int(db.execute("PRAGMA user_version").fetchone()[0])
        if version != SUPPORTED_SCHEMA:
            raise _refuse(
                f"The old database is schema {version}; only {SUPPORTED_SCHEMA} can be imported.",
                "Open the old harness once to bring it to the current schema, on a copy.",
            )
        old = OldInstallation(
            root=root,
            schema_version=version,
            profile=_load_profile(root),
            customisations=_load_customisations(root),
        )
        for row in db.execute("SELECT id, title, position, status, updated_at, details FROM cases"):
            old.cases.append(
                OldCase(
                    row["id"],
                    row["title"],
                    row["position"],
                    row["status"],
                    row["updated_at"],
                    _json(row["details"], f"case {row['id']}"),
                )
            )
        for row in db.execute("SELECT id, case_id, status, next_review, details FROM actions"):
            old.actions.append(
                OldAction(
                    row["id"],
                    row["case_id"],
                    row["status"],
                    row["next_review"],
                    _json(row["details"], f"action {row['id']}"),
                )
            )
        for row in db.execute("SELECT id, metadata, body FROM evidence"):
            old.evidence[row["id"]] = OldEvidence(
                row["id"], _json(row["metadata"], f"evidence {row['id']}"), row["body"]
            )
        for kind, table in (("one-off", "approvals"), ("standing", "standing_approvals")):
            for row in db.execute(f"SELECT id, case_id, details FROM {table}"):
                old.approvals.append(
                    OldApproval(
                        row["id"],
                        kind,
                        row["case_id"],
                        _json(row["details"], f"approval {row['id']}"),
                    )
                )
        for row in db.execute("SELECT key, value FROM meta"):
            key: str = row["key"]
            if key.startswith(("thunderbird:", "native-", "outlook-")):
                try:
                    state: Any = json.loads(row["value"])
                except ValueError:
                    state = {}
                old.journals.append(
                    OldJournal(key, state if isinstance(state, dict) else {})  # pyright: ignore[reportUnknownArgumentType]
                )
        old.skipped_rows = {table: _count(db, table) for table in SKIPPED_TABLES}
        old.skipped_rows = {t: n for t, n in old.skipped_rows.items() if n}
        return old
    finally:
        db.close()
