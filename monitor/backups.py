"""Bounded recovery snapshots: two daily, two weekly, one version checkpoint."""

import sqlite3
import hashlib
import os
from pathlib import Path

from .configuration import resolve

def limits():
    c=resolve().values['maintenance']
    return {k:c[k+'_backups'] for k in ('daily','weekly','special')}


def capture_profile(h):
    from .model import DEFAULT_DB
    source = h.path.parent / 'profile.json'
    if h.path.resolve() == DEFAULT_DB.resolve() and os.environ.get('OUTLOOK_AUTOMATION_PROFILE'):
        source = Path(os.environ['OUTLOOK_AUTOMATION_PROFILE'])
    if not source.is_file():
        return None
    raw = source.read_bytes()
    from .profile import load
    load(source)
    if source.read_bytes() != raw:
        raise ValueError('Profile changed during backup; retry')
    return raw


def embedded_profile(path):
    """Return the exact profile file from a backup, with integrity verification."""
    db = sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro&immutable=1', uri=True)
    try:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='backup_profile'").fetchone():
            return None
        row = db.execute('SELECT content,sha256 FROM backup_profile WHERE id=1').fetchone()
        if not row:
            return None
        raw = bytes(row[0])
        if hashlib.sha256(raw).hexdigest() != row[1]:
            raise ValueError('Backup profile checksum mismatch')
        return raw
    finally:
        db.close()


def include_profile(db, raw, at):
    if raw is None:
        return
    db.execute('CREATE TABLE IF NOT EXISTS backup_profile(id INTEGER PRIMARY KEY CHECK(id=1),content BLOB NOT NULL,sha256 TEXT NOT NULL,captured_at TEXT NOT NULL)')
    db.execute('INSERT OR REPLACE INTO backup_profile VALUES(1,?,?,?)',
               (raw,hashlib.sha256(raw).hexdigest(),at))
    db.commit()


def capture_configuration(h):
    root=h.path.parent.parent if h.path.parent.name=='monitor' else h.path.parent
    candidates=[root/'customisations.yaml',root/'.state/defaults.json']
    folder=root/'extensions'
    if folder.exists():candidates.extend(p for p in folder.rglob('*') if '__pycache__' not in p.parts and p.is_file())
    result={}
    for path in candidates:
        if not path.exists():continue
        if any(p.is_symlink() for p in (path,*path.parents) if p!=root):raise ValueError('Configuration backup does not follow links')
        if path.stat().st_size>2_000_000:raise ValueError('Configuration file exceeds backup limit')
        result[path.relative_to(root).as_posix()]=path.read_bytes()
    return result


def include_configuration(db,files):
    db.execute('CREATE TABLE IF NOT EXISTS backup_configuration(path TEXT PRIMARY KEY,content BLOB NOT NULL,sha256 TEXT NOT NULL)')
    db.execute('DELETE FROM backup_configuration')
    db.executemany('INSERT INTO backup_configuration VALUES(?,?,?)',[(p,raw,hashlib.sha256(raw).hexdigest()) for p,raw in files.items()])
    db.commit()


def embedded_configuration(path):
    """Extract only checksum-verified private files; callers choose the restore location."""
    db=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro&immutable=1',uri=True)
    try:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='backup_configuration'").fetchone():return {}
        result={}
        for name,raw,wanted in db.execute('SELECT path,content,sha256 FROM backup_configuration'):
            relative=Path(name)
            if relative.is_absolute() or '..' in relative.parts or hashlib.sha256(bytes(raw)).hexdigest()!=wanted:raise ValueError('Invalid backup configuration path/checksum')
            result[name]=bytes(raw)
        return result
    finally:db.close()


def plan(root):
    from .maintenance import safe_file, verified_backup, file_hash

    root = Path(root).resolve()
    folder = root / 'backups'
    groups = {k: [] for k in limits()}
    failures = []
    for raw in folder.glob('*.db'):
        try:
            path = safe_file(root, raw)
            group = 'daily' if path.name.startswith('daily-') else 'weekly' if path.name.startswith('weekly-') else 'special'
            groups[group].append(path)
        except (OSError, ValueError) as exc:
            failures.append({'path': str(raw), 'error': str(exc)})
    files = []
    for group, paths in groups.items():
        if len(paths) <= limits()[group]:
            continue
        paths.sort(key=lambda p: (p.name if group != 'special' else p.stat().st_mtime_ns, p.name), reverse=True)
        kept = []
        for path in paths:
            if path.suffix == '.db' and verified_backup(path):
                kept.append(path)
                if len(kept) == limits()[group]:
                    break
        if not kept:
            failures.append({'group': group, 'error': 'No verified replacement; excess backups retained until a usable snapshot exists'})
            continue
        for path in paths:
            if path not in kept:
                files.append(dict(path=str(path), bytes=path.stat().st_size, sha256=file_hash(path), role='backup'))
    return {'files': files, 'failures': failures}


def rotate(root):
    from .maintenance import safe_file, file_hash

    result = plan(root)
    result['deleted'] = []
    for item in result.pop('files'):
        try:
            path = safe_file(Path(root).resolve(), item['path'])
            if file_hash(path) != item['sha256']:
                raise ValueError('Backup changed after rotation planning; retry later')
            # Static backups must not depend on a WAL when their main file is removed.
            wal = Path(str(path) + '-wal')
            if wal.exists() and wal.stat().st_size:
                raise ValueError('Backup has an active WAL; retained for a later retry')
            for suffix in ('-wal', '-shm'):
                sidecar = Path(str(path) + suffix)
                if sidecar.exists():
                    safe_file(Path(root).resolve(), sidecar).unlink()
            path.unlink()
            result['deleted'].append(item)
        except FileNotFoundError:
            pass  # Another writer already finished this deletion.
        except (OSError, ValueError, sqlite3.Error) as exc:
            result['failures'].append({'path': item['path'], 'error': str(exc)})
    return result
