"""Bounded recovery snapshots: two daily, two weekly, one version checkpoint."""

import sqlite3
from pathlib import Path

LIMITS = {'daily': 2, 'weekly': 2, 'special': 1}


def plan(root):
    from .maintenance import safe_file, verified_backup, file_hash

    root = Path(root).resolve()
    folder = root / 'backups'
    groups = {k: [] for k in LIMITS}
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
        if len(paths) <= LIMITS[group]:
            continue
        paths.sort(key=lambda p: (p.name if group != 'special' else p.stat().st_mtime_ns, p.name), reverse=True)
        kept = []
        for path in paths:
            if path.suffix == '.db' and verified_backup(path):
                kept.append(path)
                if len(kept) == LIMITS[group]:
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
