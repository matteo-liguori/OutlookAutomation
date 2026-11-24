"""Opening the database and running transactions.

Contract: docs/contracts/09-storage.md (S-1, S-5, S-18).
"""

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

DB_NAME = "chamberlain.sqlite3"
MEMORY = ":memory:"


def connect(path: Path | str) -> sqlite3.Connection:
    """Open the store with the pragmas every caller relies on (S-1).

    The connection is in autocommit mode; use `transaction` for writes.
    """
    connection = sqlite3.connect(str(path), isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    if str(path) != MEMORY:
        connection.execute("PRAGMA journal_mode = WAL")
    return connection


@contextmanager
def transaction(connection: sqlite3.Connection) -> Generator[sqlite3.Connection]:
    """One logical change, all or nothing (S-5). Nested use joins the outer one."""
    if connection.in_transaction:
        yield connection
        return
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    connection.execute("COMMIT")


def table_names(connection: sqlite3.Connection) -> set[str]:
    """User tables, without SQLite's own."""
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    )
    return {row["name"] for row in rows}
