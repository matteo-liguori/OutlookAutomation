"""Conservative local retention. Unknown ownership or references always retain data."""

import hashlib
import json
import re
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .model import LONDON, dumps, digest, now, stamp
from .backups import LIMITS as BACKUP_LIMITS

POLICY = dict(
    cache_days=1,
    temporary_days=7,
    development_days=30,
    collector_cache_hours=24,
    daily_backups=BACKUP_LIMITS['daily'],
    weekly_backups=BACKUP_LIMITS['weekly'],
    named_backups=BACKUP_LIMITS['special'],
    closed_thread_days=365,
    small_attachment_bytes=10*1024*1024,
)


def strings(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from strings(item)
    elif isinstance(value, str):
        yield value


def references(values, identifiers):
    if not identifiers:
        return set()
    pattern = re.compile(
        r"(?<![\w-])(?:"
        + "|".join(re.escape(i) for i in sorted(identifiers, key=len, reverse=True))
        + r")(?![\w-])"
    )
    return {
        match.group()
        for value in values
        for text in strings(value)
        for match in pattern.finditer(text)
    }


def database_values(h, skip=()):
    """Read JSON structurally for reference checks, never as assistant context."""
    for table, columns in {
        "cases": ("details",),
        "actions": ("details",),
        "decisions": ("details",),
        "context": ("details",),
        "approvals": ("details",),
        "standing_approvals": ("details",),
        "mutations": ("result",),
        "issues": ("details",),
        "roundup": ("details",),
        "pending_work": ("details",),
        "calendar_state": ("details",),
        "calendar_reports": ("details",),
        "metrics": ("details",),
        "message_reviews": ("details",),
        "improvements": ("details",),
        "reflections": ("details",),
        "runs": ("details",),
        "sources": ("details",),
        "candidates": ("details",),
        "evidence": ("metadata", "body"),
        "meta": ("value",),
    }.items():
        if table in skip:
            continue
        where = " WHERE key NOT LIKE 'maintenance_%'" if table == "meta" else ""
        for row in h.db.execute(f"SELECT {','.join(columns)} FROM {table}" + where):
            for raw in row:
                try:
                    yield json.loads(raw)
                except (ValueError, TypeError):
                    # Plain bodies/positions may contain locators or evidence IDs.
                    yield str(raw)


def safe_file(root, path):
    root = root.resolve()
    raw = Path(path).absolute()
    resolved = raw.resolve()
    if not resolved.is_relative_to(root) or resolved == root:
        raise ValueError("Cleanup path is outside the monitoring workspace")
    for parent in (raw, *raw.parents):
        if parent.is_symlink() or (hasattr(parent, "is_junction") and parent.is_junction()):
            raise ValueError("Cleanup refuses symbolic links and junctions")
        if parent == root:
            break
    if not resolved.is_file():
        raise ValueError("Cleanup target is not a regular file")
    return resolved


def file_hash(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verified_backup(path):
    from .storage import CURRENT_SCHEMA
    from .backups import embedded_profile
    try:
        embedded_profile(path)  # Legacy DB-only backups remain usable; corrupt profile snapshots do not.
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
        try:
            return (
                db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
                and not db.execute("PRAGMA foreign_key_check").fetchall()
                and db.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA
                and db.execute(
                    "SELECT count(*) FROM sqlite_master WHERE type='table' AND name IN ('cases','actions','evidence')"
                ).fetchone()[0]
                == 3
            )
        finally:
            db.close()
    except (OSError, sqlite3.Error, ValueError):
        return False


def register_file(
    h, path, kind="temporary", *, run_id=None, source=None, validated_at=None, recovery_backup=None
):
    """Register ownership once; changed files require explicit re-registration."""
    if kind not in {"temporary", "preview", "development", "recovery"}:
        raise ValueError("Unknown disposable file category")
    target = safe_file(h.path.parent, path)
    root = h.path.parent.resolve()
    if target.parent == root or target.is_relative_to(root / "backups"):
        raise ValueError(
            "Live database, configuration and managed backups are not disposable working files"
        )
    if target.suffix.lower() in {".pdf", ".docx", ".msg", ".eml", ".xlsx"}:
        raise ValueError("Documents and original messages cannot be registered as disposable")
    if kind == "preview" and not source:
        raise ValueError("Disposable previews require their retained source")
    if kind == "temporary" and not target.is_relative_to((h.path.parent / "scratch").resolve()):
        raise ValueError("New temporary files belong in the designated scratch directory")
    if source:
        source = safe_file(h.path.parent, source)
    if recovery_backup:
        recovery_backup = safe_file(h.path.parent, recovery_backup)
        if not recovery_backup.is_relative_to(root / "backups"):
            raise ValueError("Recovery requires a managed backup")
    details = dict(
        kind=kind,
        at=now(),
        sha256=file_hash(target),
        run_id=run_id,
        source=str(source) if source else None,
        source_sha256=file_hash(source) if source else None,
        validated_at=stamp(validated_at),
        recovery_backup=str(recovery_backup) if recovery_backup else None,
    )
    with h.write():
        if run_id:
            h.run(run_id)
            h.touch("run", run_id)
        h.db.execute(
            "INSERT INTO maintenance_files VALUES(?,?) ON CONFLICT(path) DO UPDATE SET details=excluded.details",
            (str(target), dumps(details)),
        )


def file_plan(h, at):
    root, current = h.path.parent.resolve(), datetime.fromisoformat(at)
    values = list(database_values(h))
    texts = [s.replace("\\", "/").lower() for value in values for s in strings(value)]
    files, retained = {}, Counter()
    registered = [
        (r[0], json.loads(r[1])) for r in h.db.execute("SELECT path,details FROM maintenance_files")
    ]
    dependencies = {
        p for _, d in registered for p in (d.get("source"), d.get("recovery_backup")) if p
    }

    def consider(path, days, role, details=None):
        details = details or {}
        try:
            target = safe_file(root, path)
            key = str(target)
            if target == h.path.resolve() or key in {
                str(h.path.resolve()) + suffix for suffix in ("-wal", "-shm", "-journal")
            }:
                retained["live database"] += 1
                return
            if key in dependencies:
                retained["retained source or recovery dependency"] += 1
                return
            aliases = [
                key,
                str(target.relative_to(root)),
                ".local/monitor/" + target.relative_to(root).as_posix(),
                target.name,
            ]
            aliases = [name.replace("\\", "/").lower() for name in aliases]
            if any(alias in text for alias in aliases for text in texts):
                retained["referenced file"] += 1
                return
            modified = datetime.fromtimestamp(target.stat().st_mtime, timezone.utc)
            age_from = max(
                modified, datetime.fromisoformat(details.get("at") or modified.isoformat())
            )
            if details.get("run_id"):
                row = h.db.execute(
                    "SELECT at FROM retention_activity WHERE kind='run' AND id=?",
                    (details["run_id"],),
                ).fetchone()
                if not row:
                    retained["unknown run activity"] += 1
                    return
                age_from = max(age_from, datetime.fromisoformat(row[0]))
            if age_from >= current - timedelta(days=days):
                retained["within retention period"] += 1
                return
            if details.get("sha256") and file_hash(target) != details["sha256"]:
                retained["modified since registration"] += 1
                return
            if role in {"development", "recovery"}:
                backup = details.get("recovery_backup")
                if (
                    not details.get("validated_at")
                    or not backup
                    or not verified_backup(safe_file(root, backup))
                ):
                    retained["recovery not verified"] += 1
                    return
                if datetime.fromisoformat(details["validated_at"]) >= current - timedelta(days=30):
                    retained["validation recovery period"] += 1
                    return
                if role == "recovery" and file_hash(target) != file_hash(Path(backup)):
                    retained["recovery copy differs from retained backup"] += 1
                    return
            if role == "preview":
                source = safe_file(root, details.get("source") or "")
                if file_hash(source) != details.get("source_sha256"):
                    retained["preview source changed"] += 1
                    return
            files[key] = dict(
                path=key, bytes=target.stat().st_size, sha256=file_hash(target), role=role
            )
        except (OSError, ValueError):
            retained["unsafe or unavailable path"] += 1

    for folder in [root / "transport-temp", root / "scratch" / "transport"]:
        if folder.is_dir():
            for path in folder.iterdir():
                if re.fullmatch(r"[a-f0-9]{32}", path.name):
                    consider(path, 7, "temporary")
    for path, details in registered:
        if not Path(path).exists():
            continue
        days = 30 if details["kind"] in {"development", "recovery"} else 7
        consider(Path(path), days, details["kind"], details)
    from .backups import plan as backup_plan

    rotation = backup_plan(root)
    files.update({item['path']: item for item in rotation['files']})
    retained['backup rotation blocked'] = len(rotation['failures'])
    retained["unregistered working folders"] = len(
        [
            p
            for p in root.iterdir()
            if p.is_dir() and p.name not in {"backups", "scratch", "threads", "test-temp"}
        ]
    )
    return list(files.values()), dict(retained)


def status(h):
    last = h.db.execute("SELECT value FROM meta WHERE key='maintenance_result'").fetchone()
    warning = h.db.execute("SELECT value FROM meta WHERE key='maintenance_warning'").fetchone()
    lifecycle = h.db.execute("SELECT value FROM meta WHERE key='thread_cache_maintenance'").fetchone()
    rotation = h.db.execute("SELECT value FROM meta WHERE key='backup_rotation'").fetchone()
    return dict(
        policy=POLICY,
        last_result=json.loads(last[0]) if last else None,
        warning=json.loads(warning[0]) if warning else None,
        thread_cache=json.loads(lifecycle[0]) if lifecycle else None,
        backup_rotation=json.loads(rotation[0]) if rotation else None,
        source_health=__import__('monitor.live',fromlist=['items']).items(h),
        storage={
            table: h.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("message_versions", "candidates", "message_reviews", "thread_messages", "thread_attachments", "mail_snapshots", "thread_cleanup")
        },
        scratch=str(h.path.parent / "scratch"),
    )


def clean(h, dry_run=False, at=None):
    at = stamp(at or now())
    from .threadcache import sweep

    lifecycle_result = None if dry_run else sweep(h, at)
    working=None
    if not dry_run:
        from .live import prune
        with h.write():
            existing_problems=set(h.doctor()['problems'])
            working=prune(h,at=at)
            if set(h.doctor()['problems'])-existing_problems:
                raise ValueError('Maintenance validation failed; database cleanup rolled back')
    with h.read():
        files, retained = file_plan(h, at)
    result = dict(
        at=at,
        dry_run=dry_run,
        working_data=working,
        files_eligible=len(files),
        file_bytes_eligible=sum(p["bytes"] for p in files),
        files_deleted=0,
        bytes_reclaimed=0,
        retained=retained,
        failures=[],
        reliability_affected=False,
        thread_cache=lifecycle_result,
    )
    if dry_run:
        result["files"] = files
        return result
    # Keep the DB writer lock while rechecking references and deleting each file.
    with h.write():
        current_files, _ = file_plan(h, at)
        current_by_path = {p["path"]: p for p in current_files}
        for item in files:
            if current_by_path.get(item["path"]) != item:
                continue
            try:
                target = safe_file(h.path.parent, item["path"])
                if file_hash(target) != item["sha256"]:
                    continue
                target.unlink()
                h.db.execute("DELETE FROM maintenance_files WHERE path=?", (item["path"],))
                result["files_deleted"] += 1
                result["bytes_reclaimed"] += item["bytes"]
            except (OSError, ValueError) as exc:
                result["failures"].append(dict(path=item["path"], error=str(exc)))
    with h.write():
        h.db.execute(
            "INSERT INTO meta VALUES('maintenance_result',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (dumps(result),),
        )
        day = datetime.fromisoformat(at).astimezone(LONDON).date().isoformat()
        h.db.execute(
            "INSERT INTO meta VALUES('maintenance_day',?) ON CONFLICT(key) DO UPDATE SET value=max(value,excluded.value)",
            (day,),
        )
        h.db.execute("DELETE FROM meta WHERE key='maintenance_warning'")
        if result["failures"]:
            h.db.execute(
                "INSERT INTO meta VALUES('maintenance_warning',?)",
                (dumps(dict(at=at, failures=result["failures"], reliability_affected=False)),),
            )
    return result


def record_warning(h, exc):
    warning = dict(
        at=now(), error=str(exc)[:1000], reliability_affected="validation failed" in str(exc)
    )
    try:
        with h.write():
            h.db.execute(
                "INSERT INTO meta VALUES('maintenance_warning',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (dumps(warning),),
            )
    except (OSError, ValueError, sqlite3.Error):
        import warnings

        warnings.warn("Maintenance failed: " + str(exc), RuntimeWarning)


def daily(h):
    """Claim a London day before work; failures never change a pass's outcome."""
    day = datetime.fromisoformat(now()).astimezone(LONDON).date().isoformat()
    try:
        from .threadcache import sweep

        sweep(h)
        with h.write():
            previous = h.db.execute("SELECT value FROM meta WHERE key='maintenance_day'").fetchone()
            if previous and previous[0] >= day:
                return
            h.db.execute(
                "INSERT INTO meta VALUES('maintenance_day',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (day,),
            )
        clean(h)
    except (OSError, ValueError, sqlite3.Error) as exc:
        record_warning(h, exc)
