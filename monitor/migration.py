"""Previewable, idempotent migration of private behavioural configuration."""
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import zipfile

from automation_paths import data_root
from .configuration import defaults,read_yaml,dump,assign,leaves,validate,plain,atomic,lock,paths


def run(root=None,apply=False):
    root=Path(root or data_root());profile_path=root/'monitor/profile.json'
    profile=json.loads(profile_path.read_text(encoding='utf-8-sig'));custom,state_path=paths(root)
    marker=root/'.state/four-layer-migration.json'
    if marker.exists():return dict(status='already-migrated',**json.loads(marker.read_text(encoding='utf-8')))
    legacy={k:profile[k] for k in ('timezone','instructions','permission_defaults') if k in profile}
    plan=dict(status='preview',changes=list(legacy),customisations=str(custom),database=str(root/'monitor/monitor.db'),backup=str(root/'.state/migrations/four-layer.zip'))
    if not apply:return plan
    with lock(root):
        before={p:p.read_bytes() if p.exists() else None for p in (profile_path,custom,state_path,marker)}
        base=defaults();settings=read_yaml(custom) if custom.exists() else read_yaml(__import__('monitor.configuration',fromlist=['DEFAULT_FILE']).DEFAULT_FILE)
        for key,value in leaves(legacy):
            current=dict(leaves(settings)).get(key);default=dict(leaves(base)).get(key)
            if not custom.exists() or current==default:assign(settings,key,value)
        validate(plain(settings));new_profile={k:v for k,v in profile.items() if k not in legacy}
        dbpath=root/'monitor/monitor.db';db=sqlite3.connect(dbpath,timeout=10)
        db.execute('BEGIN IMMEDIATE')
        at=datetime.now(timezone.utc).isoformat(timespec='seconds')
        try:
            # A writer reservation freezes the committed snapshot; backup uses a separate reader.
            source=sqlite3.connect(dbpath.resolve().as_uri()+'?mode=ro',uri=True);memory=sqlite3.connect(':memory:')
            try:source.backup(memory);database_bytes=memory.serialize()
            finally:source.close();memory.close()
            old_state=json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {}
            state=dict(baseline=base,reviews=old_state.get('reviews',[]))
            produced={profile_path:json.dumps(new_profile,ensure_ascii=False,indent=2)+'\n',custom:dump(settings),state_path:json.dumps(state,indent=2)+'\n'}
            backup=Path(plan['backup']);backup.parent.mkdir(parents=True,exist_ok=True)
            contents={'before/monitor.db':database_bytes}
            for path,raw in before.items():
                if raw is not None:contents['before/'+path.relative_to(root).as_posix()]=raw
            for path,text in produced.items():contents['after/'+path.relative_to(root).as_posix()]=text.encode()
            for path in (root/'extensions').rglob('*') if (root/'extensions').exists() else []:
                if path.is_symlink():raise ValueError('Migration does not follow extension links')
                if path.is_file() and '__pycache__' not in path.parts:contents['private/'+path.relative_to(root).as_posix()]=path.read_bytes()
            if not backup.exists():
                with zipfile.ZipFile(backup,'x',zipfile.ZIP_DEFLATED) as archive:
                    for name,raw in contents.items():archive.writestr(name,raw)
                    archive.writestr('SHA256.json',json.dumps({n:hashlib.sha256(b).hexdigest() for n,b in contents.items()},indent=2))
            with zipfile.ZipFile(backup) as archive:
                hashes=json.loads(archive.read('SHA256.json'))
                if any(hashlib.sha256(archive.read(n)).hexdigest()!=v for n,v in hashes.items()):raise ValueError('Migration backup integrity check failed')
            from .extensions import metadata,bundle
            original=metadata(db,'startup-observation')
            folder=root/'extensions/startup-observation'
            if original.get('entries') is not None:
                if not (folder/'manifest.yaml').exists():raise ValueError('Prepare the private diagnostic bundle before migrating its observations')
                item=bundle(root,'startup-observation')
                if item['manifest'].get('expires_at')!=original['expires_at']:raise ValueError('Diagnostic expiry changed during migration')
                eid='migration-'+hashlib.sha256(b'User requested preservation of existing private diagnostic').hexdigest()
                db.execute('INSERT OR IGNORE INTO evidence VALUES(?,?,?)',(eid,json.dumps(dict(source='user-instruction',kind='migration',captured_at=at)),'Preserve the existing authorised private startup diagnostic during four-layer migration.'))
                metadata(db,'extension-observations:startup-observation',original)
                metadata(db,'extension-approval:startup-observation',dict(hash=item['hash'],approved_at=at,evidence_ids=[eid],disabled=False))
                metadata(db,'startup-observation',dict(moved_to='extension-observations:startup-observation'))
            for path,text in produced.items():atomic(path,text)
            receipt=dict(at=at,backup=str(backup),migrated_sections=list(legacy))
            atomic(marker,json.dumps(receipt,indent=2)+'\n');db.commit()
            return dict(status='migrated',**receipt)
        except BaseException:
            db.rollback()
            for path,raw in before.items():
                if raw is None:path.unlink(missing_ok=True)
                else:path.write_bytes(raw)
            raise
        finally:db.close()
