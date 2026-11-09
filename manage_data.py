"""Relocate an installation's private data without changing reusable source."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
from contextlib import contextmanager
from automation_paths import ROOT, data_root


@contextmanager
def database(path):
    db=sqlite3.connect(path,timeout=2)
    try:
        with db:
            yield db
    finally:
        db.close()


def replace_paths(value, old, new):
    if isinstance(value, dict):
        return {k:replace_paths(v, old, new) for k,v in value.items()}
    if isinstance(value, list):
        return [replace_paths(v, old, new) for v in value]
    if isinstance(value, str) and '\n' not in value:
        for prefix, target in [(str(old),str(new)), (old.as_posix(),new.as_posix())]:
            if value.casefold() == prefix.casefold() or value.casefold().startswith(prefix.casefold()+'/') or value.casefold().startswith(prefix.casefold()+'\\'):
                return target+value[len(prefix):]
        # macOS /var may alias /private/var. Rebase owned absolute paths by
        # identity as well as spelling, without modifying message body text.
        try:
            path=Path(value)
            if path.is_absolute() and path.resolve().is_relative_to(old):
                return str(new/path.resolve().relative_to(old))
        except (OSError,ValueError):
            pass
    return value


def restore_profile(backup, destination):
    from monitor.backups import embedded_profile
    raw=embedded_profile(backup)
    if raw is None:
        raise ValueError('This backup has no profile snapshot')
    destination=Path(destination)
    destination.parent.mkdir(parents=True,exist_ok=True)
    with destination.open('xb') as output:
        output.write(raw)
    return dict(profile=str(destination),restored=True)


def relocate(destination, root=ROOT):
    root = Path(root).resolve()
    old = data_root(root)
    new = Path(destination).expanduser().resolve()
    if not old.is_dir() or new.exists() or new == root or new.is_relative_to(root) or root.is_relative_to(new):
        raise ValueError('Use a new dedicated data directory outside the source checkout')
    if old.is_symlink() or any(p.is_symlink() or (hasattr(p,'is_junction') and p.is_junction()) for p in old.rglob('*')):
        raise ValueError('Data relocation refuses links/junctions')
    # Checkpoint before moving; callers must stop other harness processes first.
    databases = list(old.rglob('*.db'))
    for path in databases:
        with database(path) as db:
            if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('Database failed validation: '+path.name)
            if db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0]:
                raise ValueError('Database is busy; stop other harness processes')
            db.execute('BEGIN EXCLUSIVE');db.rollback()
    new.parent.mkdir(parents=True, exist_ok=True)
    # Same-volume rename keeps file contents/ACLs and avoids two writable copies.
    old.rename(new)
    changed = 0
    try:
        for relative in [p.relative_to(old) for p in databases]:
            with database(new / relative) as db:
                for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
                    if not table.replace('_','').isalnum():
                        raise ValueError('Unexpected database table name')
                    fields = [r[1] for r in db.execute(f'PRAGMA table_info("{table}")')]
                    # Preserve full message text and mutation fingerprints exactly.
                    if table == 'mutations':continue
                    for row in db.execute(f'SELECT rowid,* FROM "{table}"').fetchall():
                        for col,value in zip(fields,row[1:]):
                            if not isinstance(value,str) or (table=='thread_messages' and col=='body'):continue
                            try:
                                parsed=json.loads(value)
                                rewritten=replace_paths(parsed,old,new)
                                updated=json.dumps(rewritten,ensure_ascii=False,sort_keys=True) if rewritten!=parsed else value
                            except (ValueError,TypeError):
                                updated=replace_paths(value,old,new)
                            if updated!=value:
                                db.execute(f'UPDATE "{table}" SET "{col}"=? WHERE rowid=?',(updated,row[0]));changed+=1
                if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or db.execute('PRAGMA foreign_key_check').fetchall():
                    raise ValueError('Relocated database failed validation')
        # An existing pointer belongs to this same installation and is replaced here.
        pointer=root/'.local/data-root.txt'
        pointer.parent.mkdir(parents=True,exist_ok=True)
        temporary=pointer.with_suffix('.tmp')
        temporary.write_text(str(new)+'\n',encoding='utf-8')
        os.replace(temporary,pointer)
    except BaseException:
        # Keep the data in its one new location, accessible via the printed path.
        # No source or backup is deleted on failure.
        raise RuntimeError('Relocation needs attention; private data is at '+str(new))
    return dict(data_root=str(new),databases=len(databases),path_fields_updated=changed)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    operation=parser.add_mutually_exclusive_group(required=True)
    operation.add_argument('--to',help='New data folder; stop other harness processes before running')
    operation.add_argument('--restore-profile-from',help='Recover embedded profile JSON from a backup')
    parser.add_argument('--profile-output',help='Recovery destination; refuses to overwrite an existing file')
    args=parser.parse_args()
    result=relocate(args.to) if args.to else restore_profile(args.restore_profile_from,args.profile_output or data_root()/'monitor/profile.json')
    print(json.dumps(result,indent=2))
