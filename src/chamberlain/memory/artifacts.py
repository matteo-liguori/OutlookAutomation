"""The artifact store: content-addressed bytes, owners, leases and budgets.

Contract: docs/contracts/06-memory.md (M-4 to M-11, M-27). Bytes live in
`<data root>/artifacts/<aa>/<sha256>`; the agent never sees a path there (M-6).
Only the sweeper deletes bytes for lifetime reasons (M-3).
"""

import contextlib
import mimetypes
import os
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from chamberlain.domain.artifacts import ArtifactKind, OwnerKind, owner_kind_of
from chamberlain.domain.canonical import sha256_bytes
from chamberlain.domain.handles import HandleKind, new_handle
from chamberlain.domain.timefmt import minus, plus, ts
from chamberlain.limits import (
    ARTIFACT_DEFAULT_DAYS,
    ARTIFACT_MAX_DAYS,
    CARD_ARTIFACT_BYTES,
    IMPORT_MAX_BYTES,
    LEASE_MINUTES,
    OPERATION_OWNER_GRACE_HOURS,
    SESSION_ARTIFACT_HOURS,
    SESSION_IDLE_HOURS,
    TOTAL_ARTIFACT_BYTES,
)
from chamberlain.memory import repo
from chamberlain.ports.clock import Clock
from chamberlain.schema.errors import ChamberlainError, ErrorCode
from chamberlain.store.db import transaction

ARTIFACTS_DIR = "artifacts"
LEASES_DIR = "leases"
RUN_DIR = "run"
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._ -]")


@dataclass(frozen=True)
class Budgets:
    """Byte limits, configurable in private settings (M-27)."""

    per_card_bytes: int = CARD_ARTIFACT_BYTES
    total_bytes: int = TOTAL_ARTIFACT_BYTES


@dataclass(frozen=True)
class Kept:
    handle: str
    size: int
    sha256: str
    expires_at: str


@dataclass(frozen=True)
class Leased:
    path: str
    expires_at: str


def blob_path(data_root: Path, sha256: str) -> Path:
    return data_root / ARTIFACTS_DIR / sha256[:2] / sha256


def write_blob(data_root: Path, data: bytes, sha256: str) -> None:
    """Temporary file under `run/`, hash verified, then an atomic rename (M-6, M-7).

    A failure at any point leaves neither a partial nor a final file.
    """
    target = blob_path(data_root, sha256)
    if target.exists() and read_blob(data_root, sha256) is not None:
        with contextlib.suppress(OSError):
            os.utime(target)  # fresh again: a sweep must not take it before the row exists
        return  # identical content is stored once; a damaged copy is replaced below
    run = data_root / RUN_DIR
    run.mkdir(parents=True, exist_ok=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = run / f"write-{secrets.token_hex(8)}.tmp"
    try:
        with temp.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if sha256_bytes(temp.read_bytes()) != sha256:
            raise ChamberlainError(
                ErrorCode.ARTIFACT_HASH_MISMATCH, "The bytes changed while they were being stored."
            )
        os.replace(temp, target)
    finally:
        if temp.exists():
            temp.unlink()


def read_blob(data_root: Path, sha256: str) -> bytes | None:
    """The bytes if present and matching their hash, else None (M-7)."""
    path = blob_path(data_root, sha256)
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return data if sha256_bytes(data) == sha256 else None


def safe_filename(name: str | None, fallback: str) -> str:
    cleaned = _SAFE_NAME.sub("_", (name or "").strip())[:100].strip(". ")
    return cleaned or fallback


def read_import(raw_path: str) -> tuple[bytes, str]:
    """The bytes and name of a file the agent asked to import (M-8, T-6).

    The file must exist, be a regular file, not be a symlink and fit the limit.
    """
    path = Path(raw_path)
    if not path.is_absolute():
        raise _bad_import("Give the full path of the file.")
    if path.is_symlink() or not path.is_file():
        raise _bad_import("The path must be a regular file, not a link or folder.")
    if path.resolve().parent != path.parent.resolve():
        raise _bad_import("The path leaves its folder through a link.")
    size = path.stat().st_size
    if size > IMPORT_MAX_BYTES:
        raise ChamberlainError(
            ErrorCode.REQUEST_TOO_LARGE,
            f"The file is {size} bytes; the limit is {IMPORT_MAX_BYTES}.",
            field="import_path",
        )
    with path.open("rb") as handle:
        data = handle.read(IMPORT_MAX_BYTES + 1)
    if len(data) > IMPORT_MAX_BYTES:
        raise ChamberlainError(ErrorCode.REQUEST_TOO_LARGE, "The file is too large.")
    return data, path.name


def refuse_data_root(data_root: Path, raw_path: str) -> None:
    """A file under the data root is never imported: it holds the viewer token (G-18), the
    store and the bridge secrets, and `open` would hand the copy back to the agent."""
    try:
        inside = Path(raw_path).resolve().is_relative_to(data_root.resolve())
    except (OSError, ValueError):
        inside = True
    if inside:
        raise _bad_import("Files in the harness's own data folder cannot be imported.")


def _bad_import(message: str) -> ChamberlainError:
    return ChamberlainError(ErrorCode.REQUEST_INVALID, message, field="import_path")


@dataclass(frozen=True)
class ArtifactInfo:
    handle: str
    sha256: str
    size: int
    media_type: str
    filename: str | None
    live: bool


class ArtifactService:
    """Artifacts bound to a store, clock and configured storage budget."""

    def __init__(
        self,
        db: sqlite3.Connection,
        clock: Clock,
        data_root: Path,
        budgets: Budgets | None = None,
    ) -> None:
        self._db = db
        self._clock = clock
        self._root = data_root
        self.budgets = budgets or Budgets()

    # --- owners (M-4) -----------------------------------------------------------

    def edges(self) -> repo.Edges:
        now = self._clock.now()
        return repo.Edges(
            grace_edge=minus(now, hours=OPERATION_OWNER_GRACE_HOURS),
            idle_edge=minus(now, hours=SESSION_IDLE_HOURS),
        )

    def require_owner(self, owner: str, *, internal: bool = False) -> OwnerKind:
        """The owner's kind, or `artifact.no_owner` / `artifact.owner_ended` (M-4).

        A card may own an item only for the harness itself (`internal`, M-35): the agent keeps
        things under actions, never directly under a card.
        """
        kind = owner_kind_of(owner)
        if kind is OwnerKind.CARD and not internal:
            kind = None
        if kind is None or not repo.owner_exists(self._db, kind.value, owner):
            raise ChamberlainError(
                ErrorCode.ARTIFACT_NO_OWNER,
                "That handle is not something that can hold stored items.",
                field="owner",
            )
        if not repo.owner_is_live(self._db, kind.value, owner, self.edges()):
            raise ChamberlainError(
                ErrorCode.ARTIFACT_OWNER_ENDED, "That owner has finished.", field="owner"
            )
        return kind

    # --- storing ------------------------------------------------------------------

    def keep_bytes(
        self,
        *,
        data: bytes,
        kind: ArtifactKind,
        owner: str,
        reason: str,
        filename: str | None = None,
        media_type: str | None = None,
        expires_in_hours: int | None = None,
        internal: bool = False,
    ) -> Kept:
        """Store bytes under a live owner. No owner, no storage (M-4)."""
        owner_kind = self.require_owner(owner, internal=internal)
        sha = sha256_bytes(data)
        self._check_budget(owner, owner_kind, len(data), sha)
        write_blob(self._root, data, sha)
        now = self._clock.now()
        expires_at = self._expiry(now, owner_kind, expires_in_hours)
        handle = new_handle(HandleKind.ARTIFACT)
        guessed = mimetypes.guess_type(filename or "")[0]
        with transaction(self._db):
            repo.insert_artifact(
                self._db,
                handle=handle,
                kind=kind.value,
                media_type=media_type or guessed or "application/octet-stream",
                size=len(data),
                sha256=sha,
                filename=filename,
                reason=reason,
                now=ts(now),
                expires_at=expires_at,
            )
            repo.add_artifact_owner(self._db, handle, owner_kind.value, owner, ts(now))
        return Kept(handle=handle, size=len(data), sha256=sha, expires_at=expires_at)

    def _expiry(self, now: datetime, owner_kind: OwnerKind, hours: int | None) -> str:
        limit = ARTIFACT_MAX_DAYS * 24
        wanted = ARTIFACT_DEFAULT_DAYS * 24 if hours is None else min(hours, limit)
        if owner_kind is OwnerKind.SESSION:
            wanted = min(wanted, SESSION_ARTIFACT_HOURS)
        return plus(now, hours=wanted)

    def _check_budget(self, owner: str, kind: OwnerKind, size: int, sha: str) -> None:
        """Refuse beyond the global or per-card limit, naming what could be freed (M-27)."""
        already = repo.scalar(
            self._db,
            "SELECT count(*) FROM artifacts WHERE sha256 = ? AND state != 'deleted'",
            (sha,),
        )
        added = 0 if already else size
        if repo.total_stored_bytes(self._db) + added > self.budgets.total_bytes:
            raise self._over_budget("The store is full.")
        if kind in (OwnerKind.ACTION, OwnerKind.CARD):
            card = owner if kind is OwnerKind.CARD else repo.card_of_action(self._db, owner)
            if card and repo.card_live_bytes(self._db, card) + size > self.budgets.per_card_bytes:
                raise self._over_budget("This card holds as much as it may.")

    def _over_budget(self, message: str) -> ChamberlainError:
        largest = ", ".join(handle for handle, _ in repo.largest_live(self._db, 3))
        fix = "Forget stored items you no longer need"
        fix += f", for example {largest}." if largest else "."
        return ChamberlainError(ErrorCode.STORAGE_OVER_BUDGET, message, fix=fix)

    def renew(self, handle: str, *, days: int = ARTIFACT_DEFAULT_DAYS) -> str:
        """Push `expires_at` out on review; never automatic (lifetime table)."""
        row = self._row(handle)
        now = self._clock.now()
        expires = plus(now, days=min(days, ARTIFACT_MAX_DAYS))
        if expires > str(row["expires_at"]):
            with transaction(self._db):
                repo.renew_artifact(self._db, handle, expires)
        return max(expires, str(row["expires_at"]))

    # --- reading ---------------------------------------------------------------------

    def _row(self, handle: str) -> repo.Row:
        row = repo.get(self._db, "artifacts", handle)
        if row is None:
            raise ChamberlainError(ErrorCode.REQUEST_UNKNOWN_HANDLE, "No such stored item.")
        return row

    def is_readable(self, row: repo.Row) -> bool:
        if str(row["state"]) != "live":
            return False
        if str(row["expires_at"]) > ts(self._clock.now()):
            return True
        owners = repo.artifact_owners(self._db, str(row["handle"]))
        return any(str(o["owner_kind"]) == "operation" for o in owners)

    def info(self, handle: str) -> ArtifactInfo:
        row = self._row(handle)
        return ArtifactInfo(
            handle=handle,
            sha256=str(row["sha256"]),
            size=int(row["size"]),
            media_type=str(row["media_type"]),
            filename=None if row["filename"] is None else str(row["filename"]),
            live=self.is_readable(row),
        )

    def read_bytes(self, handle: str) -> bytes:
        """The verified bytes. A mismatch deletes the row and returns nothing (M-7)."""
        row = self._row(handle)
        if not self.is_readable(row):
            raise ChamberlainError(ErrorCode.ARTIFACT_EXPIRED, "That stored item has expired.")
        data = read_blob(self._root, str(row["sha256"]))
        if data is not None:
            return data
        present = blob_path(self._root, str(row["sha256"])).exists()
        with transaction(self._db):
            repo.set_artifact_state(self._db, handle, "deleted", ts(self._clock.now()))
            repo.release_hold(self._db, "artifact_owners", handle, None)
        if present:
            raise ChamberlainError(
                ErrorCode.ARTIFACT_HASH_MISMATCH, "The stored content no longer matches its hash."
            )
        raise ChamberlainError(ErrorCode.ARTIFACT_EXPIRED, "The stored bytes are gone.")

    # --- holds ----------------------------------------------------------------------------

    def add_owner(self, handle: str, owner: str) -> None:
        """Add a hold, for example an operation holding its attachments (M-11)."""
        row = self._row(handle)
        if str(row["state"]) != "live":
            raise ChamberlainError(ErrorCode.ARTIFACT_EXPIRED, "That stored item has expired.")
        kind = self.require_owner(owner)
        with transaction(self._db):
            repo.add_artifact_owner(self._db, handle, kind.value, owner, ts(self._clock.now()))

    def release_owner(self, handle: str, owner: str) -> None:
        self.release(handle, owner)

    def release(self, handle: str, owner: str | None) -> tuple[bool, int]:
        """Drop one hold, or all. Returns `(no longer readable, holds remaining)` (M-10)."""
        self._row(handle)
        with transaction(self._db):
            repo.release_hold(self._db, "artifact_owners", handle, owner)
            repo.release_if_unheld(self._db, handle)
            gone = str(self._row(handle)["state"]) != "live"
        remaining = len(repo.artifact_owners(self._db, handle))
        return gone, remaining

    # --- leases (M-9) -------------------------------------------------------------------------

    def lease(self, handle: str) -> Leased:
        """Copy to `leases/<random>/<filename>`; the sweeper removes it after 30 minutes."""
        data = self.read_bytes(handle)
        row = self._row(handle)
        name = safe_filename(None if row["filename"] is None else str(row["filename"]), handle)
        lease_id = secrets.token_hex(8)
        folder = self._root / LEASES_DIR / lease_id
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_bytes(data)
        now = self._clock.now()
        expires = plus(now, minutes=LEASE_MINUTES)
        with transaction(self._db):
            repo.insert_lease(
                self._db,
                lease_id=lease_id,
                artifact=handle,
                path=str(path),
                now=ts(now),
                expires_at=expires,
            )
        return Leased(path=str(path), expires_at=expires)
