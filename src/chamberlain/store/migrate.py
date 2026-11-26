"""The numbered-migration runner.

Contract: docs/contracts/09-storage.md (S-8 to S-15).
"""

import hashlib
import os
import re
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from importlib import resources
from itertools import count
from pathlib import Path
from tempfile import TemporaryDirectory

from chamberlain.dataroot import refuse_inside_checkout
from chamberlain.ports.clock import Clock
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.store.db import DB_NAME, connect, table_names, transaction

_FILE_NAME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
_TABLE = "schema_migrations"

LEGACY_MANIFEST: dict[int, tuple[str, str]] = {
    1: ("initial", "e84daca27fc289d3cbef7d99bdfb6256c2cbbe656113d01226796d2bd262388b"),
    30: ("writes", "b25953034d8fb7ae4f70c7437ba96576dae09433aa4ae0473f634142d372a3a9"),
    50: ("runs", "ea3daa2c1084a94e6e939d35cec939f1ccecfedff2d2d64e703719568c8c563d"),
    60: ("mail_watermark", "2a79dcd657e15dd849f6a8f8f8ee49e9a44bb92bfcfbd0a597096ff954888abc"),
    70: ("operation_abandoned", "5a3bd7369c3e8074bbeabdb3dbb5a7c9fd0ee6a3b750daf81046228a94564c33"),
    71: (
        "known_recipient_origin",
        "da412aeedf3df34fdc5672f83d49af16a77c45aff22f89aa7de42946eae036ac",
    ),
    80: (
        "card_context_and_complex",
        "63fe5b817216ea97e49a7e8f12742b91043f916dddc72f075f32f089978b4bef",
    ),
    110: (
        "uncertain_send_recheck",
        "cc436a4f917d1d1d6ee497db6077270e0406586133cd9058b7ebdf55a506ef91",
    ),
    120: (
        "mail_scan_all_folders",
        "d422f621827a7ee6a20a69c6250e58f4cac854370d7089be746c4dcc1b824c11",
    ),
    130: ("report_dashboard", "8bebc717a74daba27641e8e63ac7d681a9b77b16547f419f137c503613f24b09"),
    140: ("action_important", "52e1b1637a0724471fa2d656554656fd5ebb8007e34cc138b80abbcd4a9397e2"),
    150: ("mail_folder_sync", "5e28e2acbd0d04ab964684f92fdd1d31f673432401b77a0ef54f8c88454247c8"),
    190: ("review", "e79da369752d5e920c06751e9294fce9e346a35acf783bb6fed382a1c08df741"),
    200: ("imported_items", "cc9fad16ac4121414f2945fd6bbf174037754661b4045fdde9d03cf640757ff2"),
    201: ("read_marked", "fa718a9d5fd0fbb8c0caa2f1131601fcd960cd940801fb113d2657b28107d90e"),
    220: (
        "imported_uncertain_closed",
        "43dab83d4a75933b05519201a75f027e9a510c8a8cbdb70781fe65b53f4270b3",
    ),
    230: ("outside_actions", "8233b1a2a61bc64a00eeee140abf212bee18e1cf485258811fb33db6e5f9e4ad"),
    240: ("owl_freshness", "bdeab0e829d95571ffcfcc9ac8831571ea20f7b8224cc587819e2b37b476ffdd"),
    250: (
        "retire_mail_new_for_card",
        "c4416e489133894d0256721f2d575e4669b2c2deae4562977732f2117d88c142",
    ),
    260: ("calendar_prepared", "c98e1ef5a35bcd4fb40f34bb9bdd61860910bbb05a689d313cd9f9ebb5986176"),
}
BASELINE_VERSION = 270
BRIDGE_VERSION = 260
BRIDGE_FILE = "_upgrade_0250.sql"


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
            if entry.name.endswith(".sql") and entry.name != BRIDGE_FILE
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
    # The baseline stamp keeps the original ledger intact. Verify those preserved
    # entries against the immutable manifest before ignoring them for future steps.
    if BASELINE_VERSION in known and BASELINE_VERSION in applied and 1 in applied:
        legacy = {
            version: value for version, value in applied.items() if version < BASELINE_VERSION
        }
        if legacy != LEGACY_MANIFEST:
            raise _fail("The pre-baseline migration ledger is incomplete or changed.")
        applied = {
            version: value for version, value in applied.items() if version >= BASELINE_VERSION
        }
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


def _schema_signature(connection: sqlite3.Connection) -> list[tuple[str, str, str, str | None]]:
    """Every table, index and trigger, including implicit indexes and their owner."""
    return [
        (row[0], row[1], row[2], row[3])
        for row in connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        )
    ]


def _expected_schema(baseline: Migration) -> list[tuple[str, str, str, str | None]]:
    reference = sqlite3.connect(":memory:")
    try:
        reference.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version INTEGER PRIMARY KEY, name TEXT NOT NULL, "
            "checksum TEXT NOT NULL, applied_at TEXT NOT NULL) STRICT"
        )
        for statement in split_statements(baseline.sql):
            reference.execute(statement)
        return _schema_signature(reference)
    finally:
        reference.close()


def _adopt_baseline(
    connection: sqlite3.Connection,
    clock: Clock,
    baseline: Migration,
    backup_dir: Path | None,
) -> list[int]:
    """Stamp a known current store; optionally run the narrow 0250 -> 0260 bridge."""
    applied = applied_versions(connection)
    if not applied or BASELINE_VERSION in applied or 1 not in applied:
        return []
    prior = {v: entry for v, entry in LEGACY_MANIFEST.items() if v < BRIDGE_VERSION}
    if applied not in (prior, LEGACY_MANIFEST):
        raise _fail(
            "This store is not at the supported pre-baseline version or its ledger changed. "
            "Open it with the preceding Chamberlain version and finish its migrations first."
        )
    bridge = (
        resources.files("chamberlain.store.migrations")
        .joinpath(BRIDGE_FILE)
        .read_text(encoding="utf-8")
    )
    migration = Migration(BRIDGE_VERSION, LEGACY_MANIFEST[BRIDGE_VERSION][0], bridge)
    if migration.checksum != LEGACY_MANIFEST[BRIDGE_VERSION][1]:
        raise _fail("The calendar migration bridge does not match the recorded version.")
    if backup_dir is not None:
        _backup(connection, backup_dir / f"pre-migration-{BASELINE_VERSION:04d}.sqlite3")
    try:
        with transaction(connection):
            # Another opener may have adopted while this one made the backup.
            actual = applied_versions(connection)
            if BASELINE_VERSION in actual:
                return []
            if actual not in (prior, LEGACY_MANIFEST):
                raise _fail("The pre-baseline migration ledger changed during adoption.")
            if _schema_signature(connection) != _expected_schema(baseline):
                raise _fail("The pre-baseline schema differs from the expected current schema.")
            done: list[int] = []
            if actual == prior:
                for statement in split_statements(bridge):
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations (version, name, checksum, applied_at) "
                    "VALUES (?, ?, ?, ?)",
                    (BRIDGE_VERSION, migration.name, migration.checksum, stamp(clock)),
                )
                done.append(BRIDGE_VERSION)
            connection.execute(
                "INSERT INTO schema_migrations (version, name, checksum, applied_at) "
                "VALUES (?, ?, ?, ?)",
                (baseline.version, baseline.name, baseline.checksum, stamp(clock)),
            )
            done.append(baseline.version)
            return done
    except sqlite3.Error as error:
        raise ChamberlainError(
            ErrorCode.STORE_MIGRATION_FAILED,
            "The pre-baseline store could not be converted; nothing was changed.",
            detail=str(error),
        ) from error


def _require_recoverable_mail(connection: sqlite3.Connection) -> None:
    """Legacy move receipts lack the batch recovery facts. Leave their journal untouched."""
    if "operations" not in table_names(connection):
        return
    row = connection.execute(
        "SELECT handle, state FROM operations WHERE kind IN ('mail.move', 'mail.trash') "
        "AND state IN ('proposed','authorised','prepared','dispatched','uncertain','reconciling') "
        "AND intent_json IS NOT NULL AND ("
        "json_type(prepared_json,'$.data.destination_folder_id') IS NOT NULL "
        "OR (json_type(intent_json,'$.message') IS NOT 'text' "
        "AND json_type(intent_json,'$.messages') IS NOT 'array')) LIMIT 1"
    ).fetchone()
    if row is not None:
        raise ChamberlainError(
            ErrorCode.CONFIG_INVALID,
            f"Unsettled move or trash operation {row[0]} ({row[1]}) needs the previous bridge.",
            fix=(
                "Use the matching pre-R1 installation to reconcile the operation, or cancel it "
                "if it has never dispatched. Then retry the upgrade."
            ),
        )


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
    discovered = discover() if migrations is None else migrations
    adopted: list[int] = []
    if migrations is None:
        _require_recoverable_mail(connection)
        baseline = next((m for m in discovered if m.version == BASELINE_VERSION), None)
        if baseline is not None:
            adopted = _adopt_baseline(connection, clock, baseline, backup_dir)
    todo = pending(connection, discovered)
    if not todo:
        return adopted
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
    return [*adopted, *done]


def _backup(connection: sqlite3.Connection, target: Path) -> None:
    """Publish a complete SQLite backup without replacing an earlier attempt's backup."""
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix=".migration-", dir=target.parent) as work:
            staged = Path(work) / target.name
            with closing(sqlite3.connect(staged)) as copy:
                connection.backup(copy)
                if copy.execute("PRAGMA quick_check(1)").fetchone()[0] != "ok":
                    raise _fail("The pre-migration backup did not pass SQLite's integrity check.")
            # The staged copy is on the same filesystem. Link publishes it in one step
            # and fails on collision, so another opener's complete backup stays in place.
            for number in count(1):
                name = target if number == 1 else target.with_stem(f"{target.stem}-{number}")
                try:
                    os.link(staged, name)
                except FileExistsError:
                    continue
                return
    except (OSError, sqlite3.Error) as error:
        raise ChamberlainError(
            ErrorCode.STORE_MIGRATION_FAILED,
            "The pre-migration backup could not be saved. The next migration did not run.",
            detail=str(error)[:300],
        ) from error


def stamp(clock: Clock) -> str:
    """The clock's time in the stored form (D-7)."""
    return clock.now().strftime("%Y-%m-%dT%H:%M:%SZ")


def open_store(data_root: Path, clock: Clock) -> sqlite3.Connection:
    """Open (creating if needed) and migrate the store under a data root (S-18)."""
    refuse_inside_checkout(data_root)
    data_root.mkdir(parents=True, exist_ok=True)
    connection = connect(data_root / DB_NAME)
    try:
        migrate(connection, clock, backup_dir=data_root / "backups")
    except BaseException:
        connection.close()
        raise
    return connection
