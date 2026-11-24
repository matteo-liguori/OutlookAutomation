"""The numbered-migration runner.

Contract: docs/contracts/09-storage.md (S-8 to S-15).
"""

import hashlib
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from chamberlain.ports.clock import Clock
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.store.db import DB_NAME, connect, table_names, transaction

_FILE_NAME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
_TABLE = "schema_migrations"


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        normalised = self.sql.replace("\r\n", "\n")
        return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def _fail(message: str) -> ChamberlainError:
    return ChamberlainError(ErrorCode.STORE_MIGRATION_FAILED, message)


def discover(directory: Path | None = None) -> list[Migration]:
    """Migrations in ascending order. Defaults to the packaged ones (S-8)."""
    if directory is None:
        files = [
            (entry.name, entry.read_text(encoding="utf-8"))
            for entry in resources.files("chamberlain.store.migrations").iterdir()
            if entry.name.endswith(".sql")
        ]
    else:
        files = [(p.name, p.read_text(encoding="utf-8")) for p in directory.glob("*.sql")]
    found: dict[int, Migration] = {}
    for file_name, sql in files:
        match = _FILE_NAME.match(file_name)
        if match is None:
            raise _fail(f"Migration file name {file_name!r} is not NNNN_snake_name.sql.")
        version = int(match.group(1))
        if version in found:
            raise _fail(f"Two migrations are numbered {version:04d}.")
        found[version] = Migration(version, match.group(2), sql)
    return [found[version] for version in sorted(found)]


def split_statements(sql: str) -> list[str]:
    """Split a script into statements so they can run inside one transaction."""
    statements: list[str] = []
    buffer = ""
    for line in sql.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statements.append(buffer.strip())
            buffer = ""
    if _strip_comments(buffer):
        raise _fail("A migration ends with an incomplete statement.")
    return [s for s in statements if _strip_comments(s)]


def _strip_comments(sql: str) -> str:
    return "\n".join(line for line in sql.splitlines() if not line.strip().startswith("--")).strip()


def applied_versions(connection: sqlite3.Connection) -> dict[int, tuple[str, str]]:
    """`{version: (name, checksum)}` for every applied migration."""
    if _TABLE not in table_names(connection):
        return {}
    rows = connection.execute(f"SELECT version, name, checksum FROM {_TABLE}")
    return {row["version"]: (row["name"], row["checksum"]) for row in rows}


def pending(connection: sqlite3.Connection, migrations: Sequence[Migration]) -> list[Migration]:
    """What would be applied, after checking S-9 and S-11."""
    applied = applied_versions(connection)
    known = {m.version: m for m in migrations}
    for version, (name, checksum) in sorted(applied.items()):
        if version not in known:
            raise _fail(f"The database has migration {version:04d}, which this code does not.")
        if known[version].checksum != checksum:
            raise _fail(f"Applied migration {version:04d}_{name} was edited afterwards.")
    highest = max(applied, default=0)
    todo = [m for m in migrations if m.version not in applied]
    for migration in todo:
        if migration.version < highest:
            raise _fail(
                f"Migration {migration.version:04d} is numbered below applied {highest:04d}."
            )
    return todo


def migrate(
    connection: sqlite3.Connection,
    clock: Clock,
    *,
    backup_dir: Path | None,
    migrations: Sequence[Migration] | None = None,
) -> list[int]:
    """Apply pending migrations in order and return their versions.

    Each runs in one transaction with its ledger row (S-8). A database that
    already holds data is backed up first (S-10); pass `backup_dir=None` only
    for an in-memory database.
    """
    todo = pending(connection, discover() if migrations is None else migrations)
    if not todo:
        return []
    if applied_versions(connection) and backup_dir is not None:
        _backup(connection, backup_dir / f"pre-migration-{todo[0].version:04d}.sqlite3")
    connection.execute(
        f"CREATE TABLE IF NOT EXISTS {_TABLE} ("
        "version INTEGER PRIMARY KEY, name TEXT NOT NULL, "
        "checksum TEXT NOT NULL, applied_at TEXT NOT NULL) STRICT"
    )
    done: list[int] = []
    for migration in todo:
        try:
            with transaction(connection):
                for statement in split_statements(migration.sql):
                    connection.execute(statement)
                connection.execute(
                    f"INSERT INTO {_TABLE} (version, name, checksum, applied_at) "
                    "VALUES (?, ?, ?, ?)",
                    (migration.version, migration.name, migration.checksum, stamp(clock)),
                )
        except sqlite3.Error as error:
            raise ChamberlainError(
                ErrorCode.STORE_MIGRATION_FAILED,
                f"Migration {migration.version:04d}_{migration.name} failed and was rolled back.",
                detail=str(error),
            ) from error
        done.append(migration.version)
    return done


def _backup(connection: sqlite3.Connection, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    copy = sqlite3.connect(str(target))
    try:
        connection.backup(copy)
    finally:
        copy.close()


def stamp(clock: Clock) -> str:
    """The clock's time in the stored form (D-7)."""
    return clock.now().strftime("%Y-%m-%dT%H:%M:%SZ")


def open_store(data_root: Path, clock: Clock) -> sqlite3.Connection:
    """Open (creating if needed) and migrate the store under a data root (S-18)."""
    data_root.mkdir(parents=True, exist_ok=True)
    connection = connect(data_root / DB_NAME)
    try:
        migrate(connection, clock, backup_dir=data_root / "backups")
    except BaseException:
        connection.close()
        raise
    return connection
