"""The one sweeper: applies the whole lifetime table (M-3, M-21 to M-27).

Contract: docs/contracts/06-memory.md. `sweep(ctx, now)` is idempotent and safe
to interrupt at any point: each rule commits on its own, marks the database
before it touches a file, and a second run finishes what a first began. Only
this module deletes rows or files for lifetime reasons.
"""

import os
import shutil
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from pydantic import Field

from chamberlain.app.context import Context
from chamberlain.domain.canonical import sha256_text
from chamberlain.limits import (
    LOG_DAYS,
    LOG_MAX_BYTES,
    NOTE_IDLE_DAYS,
    OPERATION_OWNER_GRACE_HOURS,
    QUARANTINE_DAYS,
    REF_DAYS,
    SESSION_IDLE_HOURS,
    SUMMARY_MAX,
)
from chamberlain.memory import backups, repo
from chamberlain.memory.artifacts import (
    ARTIFACTS_DIR,
    LEASES_DIR,
    RUN_DIR,
    Budgets,
    blob_path,
)
from chamberlain.memory.history import record_snapshot
from chamberlain.memory.timeutil import minus, plus, ts
from chamberlain.schema.base import Model
from chamberlain.store.db import DB_NAME, transaction
from chamberlain.store.schema import set_state

TOMBSTONE_DAYS = 7
"""How long the row of a deleted artifact stays, so `open` can say it expired."""
RUN_TEMP_MINUTES = 60
DATA_ROOT_LAYOUT = frozenset(
    {
        DB_NAME,
        f"{DB_NAME}-wal",
        f"{DB_NAME}-shm",
        "config",
        ARTIFACTS_DIR,
        LEASES_DIR,
        "backups",
        "logs",
        "quarantine",
        RUN_DIR,
        "bridge",
    }
)
"""The fixed layout of the data root (M-25, board ruling R9). Anything else is an orphan."""
NEVER_SCANNED = frozenset({"config", "backups", "logs", "quarantine", RUN_DIR, "bridge"})


class SweepCrash(Exception):
    """Raised by a test hook to simulate the process dying mid-sweep."""


def _unlink(path: Path) -> None:
    path.unlink()


Compactor = Callable[[Context, datetime], int]
"""A lifetime rule for a table another track writes (operations, grants, events, audit).

The owning track supplies the function; the sweeper runs it inside the `records` step so
the whole lifetime table is applied in one ordered pass and reported in one audit row.
It returns how many rows it compacted or deleted. Memory never writes those tables.
"""


@dataclass(frozen=True)
class SweepHooks:
    """Seams for tests: how a file is removed, and a chance to die between rules."""

    unlink: Callable[[Path], None] = _unlink
    after_rule: Callable[[str], None] | None = None


class RuleCount(Model):
    count: int = 0
    bytes: int = 0


class SweepReport(Model):
    """Counts and bytes per rule, audited as one `sweep` row (M-21)."""

    at: str
    rules: dict[str, RuleCount] = Field(default_factory=dict)
    failed_deletions: int = 0
    orphans_found: int = 0
    over_budget: bool = False

    def total(self, rule: str) -> int:
        found = self.rules.get(rule)
        return 0 if found is None else found.count

    def changed(self) -> bool:
        """Whether any rule removed or moved something (the idle-session count is only a view)."""
        return any(c.count for name, c in self.rules.items() if name != "sessions_idle")


@dataclass
class _Run:
    ctx: Context
    now: datetime
    budgets: Budgets
    hooks: SweepHooks
    compactors: Sequence[tuple[str, Compactor]] = ()
    counts: dict[str, RuleCount] = field(default_factory=dict[str, RuleCount])
    failed: int = 0
    orphans: int = 0
    over_budget: bool = False

    @property
    def db(self) -> sqlite3.Connection:
        return self.ctx.db

    @property
    def root(self) -> Path:
        return self.ctx.data_root

    def add(self, rule: str, count: int, size: int = 0) -> None:
        previous = self.counts.get(rule, RuleCount())
        self.counts[rule] = RuleCount(count=previous.count + count, bytes=previous.bytes + size)

    def edges(self) -> repo.Edges:
        return repo.Edges(
            grace_edge=minus(self.now, hours=OPERATION_OWNER_GRACE_HOURS),
            idle_edge=minus(self.now, hours=SESSION_IDLE_HOURS),
        )


def sweep(
    ctx: Context,
    now: datetime,
    *,
    budgets: Budgets | None = None,
    hooks: SweepHooks | None = None,
    compactors: Sequence[tuple[str, Compactor]] = (),
) -> SweepReport:
    """Apply the lifetime table once (M-21). Order follows M-22.

    `compactors` are the other tracks' rules for their own tables (operation and grant
    compaction, event expiry, audit windows), supplied at wiring and run in `records`.
    """
    run = _Run(ctx, now, budgets or Budgets(), hooks or SweepHooks(), compactors)
    for name, rule in _RULES:
        rule(run)
        if run.hooks.after_rule is not None:
            run.hooks.after_rule(name)
    with transaction(ctx.db):
        set_state(ctx.db, ctx.clock, "memory.pending_deletions", str(run.failed))
        set_state(ctx.db, ctx.clock, "memory.last_sweep_at", ts(now))
        record_snapshot(ctx.db, ctx.clock, ctx.data_root, now)
        report = SweepReport(
            at=ts(now),
            rules=dict(run.counts),
            failed_deletions=run.failed,
            orphans_found=run.orphans,
            over_budget=run.over_budget,
        )
        summary = "; ".join(f"{k}={v.count}" for k, v in sorted(run.counts.items()) if v.count)
        ctx.services.audit.append(
            actor="harness:sweeper",
            kind="sweep",
            subject=None,
            summary=(summary or "nothing to remove")[:SUMMARY_MAX],
            payload_sha256=sha256_text(report.model_dump_json()),
        )
    return report


# --- the rules, in the order of M-22 ---------------------------------------------------------


def _idle_sessions(run: _Run) -> None:
    """Sessions belong to attention, which ends them. Here they only stop being live owners."""
    count = repo.scalar(
        run.db,
        "SELECT count(*) FROM sessions WHERE ended_at IS NULL AND last_seen_at <= ?",
        (run.edges().idle_edge,),
    )
    run.add("sessions_idle", count)


def _release_dead_holds(run: _Run) -> None:
    released = 0
    with transaction(run.db):
        for row in repo.dead_holds(run.db, run.edges()):
            repo.remove_artifact_owner(run.db, str(row["artifact"]), str(row["owner"]))
            repo.release_if_unheld(run.db, str(row["artifact"]))
            released += 1
    run.add("holds_released", released)


def _expire_artifacts(run: _Run) -> None:
    expired = 0
    with transaction(run.db):
        for handle in repo.expirable_artifacts(run.db, ts(run.now)):
            repo.remove_artifact_owner(run.db, handle, None)
            repo.release_if_unheld(run.db, handle)
            expired += 1
        for handle in repo.unheld_live_artifacts(run.db):
            repo.release_if_unheld(run.db, handle)
            expired += 1
    run.add("artifacts_expired", expired)


def _delete_evidence(run: _Run) -> None:
    with transaction(run.db):
        repo.delete_dead_evidence_refs(run.db)
        count = repo.delete_unreferenced_evidence(run.db)
    run.add("evidence_deleted", count)


def _delete_bytes(run: _Run) -> None:
    """Delete bytes with no live row; the database is marked only after the file is gone."""
    deleted = 0
    freed = 0
    lost = 0
    for row in repo.live_artifacts(run.db):
        if not blob_path(run.root, str(row["sha256"])).exists():
            with transaction(run.db):
                repo.set_artifact_state(run.db, str(row["handle"]), "deleted", ts(run.now))
                repo.remove_artifact_owner(run.db, str(row["handle"]), None)
            lost += 1
    for sha in repo.released_shas(run.db):
        if repo.sha_has_live_row(run.db, sha):
            with transaction(run.db):
                repo.mark_sha_deleted(run.db, sha, ts(run.now))
            continue
        path = blob_path(run.root, sha)
        size = 0
        try:
            size = path.stat().st_size if path.exists() else 0
            run.hooks.unlink(path)
        except FileNotFoundError:
            pass
        except OSError:
            run.failed += 1
            continue
        with transaction(run.db):
            deleted += repo.mark_sha_deleted(run.db, sha, ts(run.now))
        freed += size
        _remove_empty(path.parent)
    run.add("bytes_deleted", deleted, freed)
    run.add("rows_without_file", lost)


def _remove_empty(folder: Path) -> None:
    try:
        folder.rmdir()
    except OSError:
        return


def _compact_records(run: _Run) -> None:
    now = ts(run.now)
    with transaction(run.db):
        run.add("cards_purged", repo.purge_closed_cards(run.db, now))
        run.add("actions_purged", repo.purge_ended_actions(run.db, now))
        run.add("notes_with_ended_action", repo.delete_notes_of_ended_actions(run.db))
        run.add("notes_idle", repo.delete_idle_notes(run.db, minus(run.now, days=NOTE_IDLE_DAYS)))
        run.add("refs_stale", repo.delete_stale_refs(run.db, minus(run.now, days=REF_DAYS)))
        run.add(
            "artifact_rows_purged",
            repo.purge_tombstones(run.db, minus(run.now, days=TOMBSTONE_DAYS)),
        )
    for name, compactor in run.compactors:
        run.add(name, compactor(run.ctx, run.now))


def _trim_ephemeral(run: _Run) -> None:
    with transaction(run.db):
        run.add(
            "cursors_expired",
            repo.delete_expired_cursors(run.db, ts(run.now), run.edges().idle_edge),
        )
    leases = 0
    for row in repo.expired_leases(run.db, ts(run.now)):
        folder = Path(str(row["path"])).parent
        if _inside(run.root / LEASES_DIR, folder) and folder.exists():
            try:
                shutil.rmtree(folder)
            except OSError:
                run.failed += 1
                continue
        with transaction(run.db):
            repo.delete_lease(run.db, str(row["id"]))
        leases += 1
    run.add("leases_expired", leases)


def _is_link(path: Path) -> bool:
    """A symlink or, on Windows, a directory junction. Neither is ever followed."""
    return path.is_symlink() or path.is_junction()


def _inside(parent: Path, child: Path) -> bool:
    """Whether the entry itself sits inside `parent`; a link is judged by where it lives."""
    try:
        return child.parent.resolve().is_relative_to(parent.resolve())
    except OSError:
        return False


def _scan_orphans(run: _Run) -> None:
    """Move strays to quarantine and delete what has waited out its grace period (M-25, M-26)."""
    found = 0
    for path in _find_orphans(run):
        if _quarantine(run, path):
            found += 1
    run.orphans = found
    with transaction(run.db):
        set_state(run.db, run.ctx.clock, "memory.last_orphan_scan_at", ts(run.now))
    run.add("orphans_quarantined", found)
    deleted = 0
    freed = 0
    for row in repo.due_quarantine(run.db, ts(run.now)):
        target = Path(str(row["quarantined_path"]))
        if _inside(run.root / "quarantine", target) and (target.exists() or _is_link(target)):
            try:
                _remove_any(target)
            except OSError:
                run.failed += 1
                continue
        with transaction(run.db):
            repo.mark_quarantine_deleted(run.db, int(row["id"]))
        deleted += 1
        freed += int(row["size"])
    run.add("quarantine_deleted", deleted, freed)
    with transaction(run.db):
        repo.purge_quarantine_rows(run.db, minus(run.now, days=QUARANTINE_DAYS))


def _find_orphans(run: _Run) -> list[Path]:
    orphans: list[Path] = []
    root = run.root
    if not root.exists():
        return orphans
    for entry in sorted(root.iterdir()):
        if entry.name not in DATA_ROOT_LAYOUT:
            orphans.append(entry)
    known = repo.known_shas(run.db)
    artifacts = root / ARTIFACTS_DIR
    if artifacts.is_dir() and not _is_link(artifacts):
        for entry in sorted(artifacts.iterdir()):
            if _is_link(entry) or not entry.is_dir() or len(entry.name) != 2:
                orphans.append(entry)
                continue
            for blob in sorted(entry.iterdir()):
                ok = (
                    not _is_link(blob)
                    and blob.is_file()
                    and blob.name in known
                    and blob.name.startswith(entry.name)
                )
                if not ok:
                    orphans.append(blob)
    leases = root / LEASES_DIR
    if leases.is_dir() and not _is_link(leases):
        ids = {str(row["id"]) for row in repo.all_leases(run.db)}
        for entry in sorted(leases.iterdir()):
            if _is_link(entry) or not entry.is_dir() or entry.name not in ids:
                orphans.append(entry)
    return orphans


def _size_of(path: Path) -> int:
    if _is_link(path) or path.is_file():
        try:
            return path.lstat().st_size
        except OSError:
            return 0
    total = 0
    for base, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            try:
                total += (Path(base) / name).lstat().st_size
            except OSError:
                continue
    return total


def _remove_any(path: Path) -> None:
    """Remove a file, a link (never its target) or a folder tree."""
    if path.is_junction():
        os.rmdir(path)
    elif path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _quarantine(run: _Run, path: Path) -> bool:
    day = run.now.strftime("%Y-%m-%d")
    folder = run.root / "quarantine" / day
    try:
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / path.name
        counter = 1
        while target.exists() or _is_link(target):
            counter += 1
            target = folder / f"{counter}-{path.name}"
        size = _size_of(path)
        with transaction(run.db):
            repo.insert_quarantine(
                run.db,
                original=str(path),
                quarantined=str(target),
                size=size,
                found_at=ts(run.now),
                delete_after=plus(run.now, days=QUARANTINE_DAYS),
            )
            os.replace(path, target)
    except OSError:
        run.failed += 1
        return False
    return True


def _rotate_system_files(run: _Run) -> None:
    removed = 0
    freed = 0
    cutoff = run.now.timestamp() - LOG_DAYS * 86400
    logs = run.root / "logs"
    files: list[tuple[float, int, Path]] = []
    if logs.is_dir() and not logs.is_symlink():
        for entry in logs.iterdir():
            if entry.is_file() and not entry.is_symlink():
                info = entry.stat()
                files.append((info.st_mtime, info.st_size, entry))
    files.sort()
    total = sum(size for _, size, _ in files)
    for mtime, size, entry in files:
        if mtime < cutoff or total > LOG_MAX_BYTES:
            try:
                entry.unlink()
            except OSError:
                run.failed += 1
                continue
            total -= size
            removed += 1
            freed += size
    temp_cutoff = run.now.timestamp() - RUN_TEMP_MINUTES * 60
    temp_dir = run.root / RUN_DIR
    if temp_dir.is_dir() and not temp_dir.is_symlink():
        for entry in temp_dir.glob("*.tmp"):
            if entry.is_file() and not entry.is_symlink() and entry.stat().st_mtime < temp_cutoff:
                try:
                    entry.unlink()
                except OSError:
                    run.failed += 1
                    continue
                removed += 1
    removed += backups.rotate(run.root)
    run.add("system_files_rotated", removed, freed)


def _check_budgets(run: _Run) -> None:
    total = repo.total_stored_bytes(run.db)
    parts: list[str] = []
    if total > run.budgets.total_bytes:
        parts.append(f"total:{total}/{run.budgets.total_bytes}")
    for card, used in repo.cards_over_artifact_budget(run.db, run.budgets.per_card_bytes):
        parts.append(f"{card}:{used}/{run.budgets.per_card_bytes}")
    state = ",".join(parts) if parts else "0"
    run.over_budget = state != "0"
    with transaction(run.db):
        set_state(run.db, run.ctx.clock, "memory.over_budget", state)


_RULES: tuple[tuple[str, Callable[[_Run], None]], ...] = (
    ("sessions", _idle_sessions),
    ("holds", _release_dead_holds),
    ("expiry", _expire_artifacts),
    ("evidence", _delete_evidence),
    ("bytes", _delete_bytes),
    ("records", _compact_records),
    ("ephemeral", _trim_ephemeral),
    ("orphans", _scan_orphans),
    ("rotate", _rotate_system_files),
    ("budgets", _check_budgets),
)
"""The rule table of M-22: one named function per step, in order."""


def rule_names() -> tuple[str, ...]:
    return tuple(name for name, _ in _RULES)
