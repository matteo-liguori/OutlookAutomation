"""Preview and apply a v2 store update without opening any providers (S-8 to S-16)."""

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from tempfile import mkdtemp
from time import monotonic
from typing import Any

import yaml

from chamberlain.connections.load import CONFIG_FILE, parse_connections, profile_path
from chamberlain.dataroot import refuse_inside_checkout
from chamberlain.ports.clock import Clock, SystemClock
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.store.db import DB_NAME, MEMORY, connect
from chamberlain.store.migrate import applied_versions, migrate

_SNAPSHOT_SECONDS = 30


@dataclass(frozen=True)
class ConfigMigration:
    profile: Path | None
    store_version: int | None
    pending: tuple[int, ...] = ()
    applied: tuple[int, ...] = ()
    backup: Path | None = None
    """Directory containing both the pre-migration SQLite backup and the profile, if present."""


def migrate_config(
    data_root: Path, *, apply: bool = False, clock: Clock | None = None
) -> ConfigMigration:
    """Validate the profile and prove the migration on a consistent copy first.

    An absent store needs no migration. A store without a known v2 ledger, a store
    newer than this code, or an unsupported profile (including retired demo sources)
    requires the appropriate earlier version or a corrected profile before applying.
    A valid YAML profile is preserved verbatim. This never loads connectors or secrets.
    """
    root = refuse_inside_checkout(data_root)
    if root.exists() and not root.is_dir():
        raise ChamberlainError(
            ErrorCode.CONFIG_INVALID, "The data path must be a directory.", field="data"
        )
    profile = profile_path(root)
    original = _profile_bytes(profile)
    _validate_profile(original)
    present_profile = profile if original is not None else None
    database = root / DB_NAME
    if not database.exists():
        return ConfigMigration(present_profile, None)

    the_clock = clock if clock is not None else SystemClock()
    previous, pending = _preview_store(database, the_clock)
    if not apply or not pending:
        return ConfigMigration(present_profile, previous, pending)

    if _profile_bytes(profile) != original:
        raise ChamberlainError(
            ErrorCode.CONFIG_INVALID,
            "The connection profile changed during preview. Run config migrate again.",
        )
    parent = refuse_inside_checkout(root / "backups")
    try:
        with closing(connect(database, must_exist=True)) as store:
            if max(applied_versions(store), default=0) != previous:
                raise _store_error(
                    "The store version changed during preview. Run config migrate again."
                )
            parent.mkdir(exist_ok=True)
            backup = Path(mkdtemp(prefix="config-migration-", dir=parent))
            if original is not None:
                (backup / CONFIG_FILE).write_bytes(original)
            done = tuple(migrate(store, the_clock, backup_dir=backup))
    except (OSError, sqlite3.Error) as error:
        raise _store_error(
            "The installed store could not be backed up or migrated.", error
        ) from error
    return ConfigMigration(present_profile, previous, pending, done, backup)


def _profile_bytes(profile: Path) -> bytes | None:
    try:
        return profile.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ChamberlainError(
            ErrorCode.CONFIG_INVALID, "The connection profile cannot be read.", field="profile"
        ) from error


def _validate_profile(raw: bytes | None) -> None:
    if raw is None:
        return
    try:
        document: Any = yaml.safe_load(raw.decode("utf-8-sig"))
    except (UnicodeError, yaml.YAMLError) as error:
        raise ChamberlainError(
            ErrorCode.CONFIG_INVALID, "The connection profile cannot be read.", field="profile"
        ) from error
    parse_connections(document)


def _preview_store(path: Path, clock: Clock) -> tuple[int, tuple[int, ...]]:
    try:
        with closing(connect(MEMORY)) as scratch:
            _snapshot(path, scratch)
            check = scratch.execute("PRAGMA quick_check(1)").fetchone()
            if check[0] != "ok":
                raise _store_error("The installed SQLite store did not pass its integrity check.")
            version = max(applied_versions(scratch), default=0)
            if version == 0:
                raise _store_error(
                    "This store has no v2 migration ledger. Check the data path. "
                    "Use chamberlain migrate detect for a v1 installation."
                )
            return version, tuple(migrate(scratch, clock, backup_dir=None))
    except (OSError, sqlite3.Error) as error:
        raise _store_error("The installed SQLite store could not be previewed.", error) from error


def _snapshot(path: Path, target: sqlite3.Connection) -> None:
    wal = path.with_name(path.name + "-wal")
    shm = path.with_name(path.name + "-shm")
    sidecars = (wal.exists(), shm.exists())
    if sidecars[0] != sidecars[1]:
        raise _store_error(
            "The store has incomplete SQLite sidecars. Close other Chamberlain processes and "
            "open the store with its installed version to recover it before previewing again."
        )
    # A normal read-only SQLite open can create WAL sidecars even for a closed store.
    # With no WAL, read a stable immutable image and reject a concurrent writer.
    quiet = not sidecars[0]
    before = path.stat()
    deadline = monotonic() + _SNAPSHOT_SECONDS

    def progress(status: int, _left: int, _total: int) -> None:
        if status != sqlite3.SQLITE_DONE and monotonic() >= deadline:
            raise _store_error(
                "The SQLite snapshot timed out. Try again after other writes finish."
            )

    uri = path.resolve().as_uri() + "?mode=ro" + ("&immutable=1" if quiet else "")
    with closing(sqlite3.connect(uri, uri=True)) as source:
        source.backup(target, pages=256, progress=progress)
    if quiet:
        after = path.stat()
        if (
            (before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_ino, after.st_size, after.st_mtime_ns)
            or wal.exists()
            or shm.exists()
        ):
            raise _store_error("The store changed during preview. Run config migrate again.")


def _store_error(message: str, error: Exception | None = None) -> ChamberlainError:
    return ChamberlainError(
        ErrorCode.STORE_MIGRATION_FAILED, message, detail="" if error is None else str(error)[:300]
    )
