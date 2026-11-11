"""Version-approved private extensions. No executable code lives in card records."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys

from .configuration import read_yaml, plain, validate, resolve, snapshot


def clock():return datetime.now(timezone.utc).isoformat(timespec='seconds')


def metadata(db, key, value=None):
    if value is None:
        row=db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
        return json.loads(row[0]) if row else {}
    db.execute('INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,json.dumps(value)))


def bundle(root, ident):
    if not isinstance(ident,str) or not re.fullmatch('[a-z0-9][a-z0-9-]{0,79}',ident):raise ValueError('Invalid extension ID')
    parent=Path(root)/'extensions';folder=parent/ident
    if parent.is_symlink() or folder.is_symlink() or not folder.resolve().is_relative_to(parent.resolve()):raise ValueError('Linked extension directories are not supported')
    manifest=plain(read_yaml(folder/'manifest.yaml'))
    allowed={'id','kind','enabled','expires_at','settings','card_overlays','actions','hooks','commands','timeout_seconds'}
    if set(manifest)-allowed or manifest.get('id')!=ident or manifest.get('kind') not in ('action','experiment'):raise ValueError('Invalid extension manifest')
    if type(manifest.get('enabled',False)) is not bool:raise ValueError('Extension enabled must be boolean')
    if manifest.get('expires_at'):
        when=datetime.fromisoformat(manifest['expires_at'].replace('Z','+00:00'))
        if when.utcoffset() is None:raise ValueError('Extension expiry needs an offset')
    timeout=manifest.get('timeout_seconds',30)
    if type(timeout) is not int or not 1<=timeout<=300:raise ValueError('Extension timeout must be 1..300 seconds')
    hashed=[]
    for path in sorted(folder.rglob('*')):
        if '__pycache__' in path.parts or '.state' in path.parts:continue
        if path.is_symlink() or getattr(path,'is_junction',lambda:False)():raise ValueError('Extension cannot contain links')
        if not path.is_file() or path.name=='manifest.yaml':continue
        if path.stat().st_size>2_000_000 or len(hashed)>=200:raise ValueError('Extension exceeds bundle size limit')
        hashed.append((path.relative_to(folder).as_posix(),hashlib.sha256(path.read_bytes()).hexdigest()))
    version=dict(manifest);version.pop('enabled',None)
    fingerprint=hashlib.sha256(json.dumps([version,hashed],sort_keys=True).encode()).hexdigest()
    return dict(id=ident,folder=folder,manifest=manifest,hash=fingerprint)


def available(config=None,h=None):
    config=config or resolve();root=config.root
    if root is None or not (root/'extensions').exists():return []
    owned=None;db=h.db if h else None
    path=root/'monitor/monitor.db'
    if db is None and path.exists():
        owned=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True);db=owned
    result=[]
    try:
        for folder in sorted((root/'extensions').iterdir()):
            if not folder.is_dir() or not (folder/'manifest.yaml').exists():continue
            item=bundle(root,folder.name);m=item['manifest']
            grant=metadata(db,'extension-approval:'+item['id']) if db is not None else {}
            expired=bool(m.get('expires_at') and datetime.fromisoformat(m['expires_at'].replace('Z','+00:00'))<=datetime.now(timezone.utc))
            item['active']=bool(m.get('enabled') and not expired and grant.get('hash')==item['hash'] and not grant.get('disabled'))
            item['reason']='expired' if expired else 'disabled' if not m.get('enabled') or grant.get('disabled') else 'code-version-needs-approval' if grant.get('hash')!=item['hash'] else 'active'
            result.append(item)
    finally:
        if owned:owned.close()
    return result


def effective(config,h=None,card=None):
    for item in available(config,h):
        if not item['active']:continue
        m=item['manifest']
        config=config.overlay(m.get('settings',{}),'extension:'+item['id'])
        patch=m.get('card_overlays',{}).get(card['id'],{}) if card else {}
        if patch:config=config.overlay(patch,'extension:'+item['id']+':'+card['id'])
    return config


def approve(h,payload,config=None):
    config=config or resolve();item=bundle(config.root,payload['id'])
    if payload.get('hash')!=item['hash']:raise ValueError('Extension changed; inspect its current hash before approval')
    if not payload.get('evidence_ids'):raise ValueError('Code approval requires user authority evidence')
    h.evidence_exists(payload['evidence_ids'])
    with h.write():metadata(h.db,'extension-approval:'+item['id'],dict(hash=item['hash'],approved_at=clock(),evidence_ids=payload['evidence_ids'],disabled=False))
    return dict(id=item['id'],approved_hash=item['hash'])


def disable(h,ident):
    with h.write():
        grant=metadata(h.db,'extension-approval:'+ident);grant['disabled']=True;metadata(h.db,'extension-approval:'+ident,grant)
    return dict(id=ident,disabled=True)


def invoke(item, entrypoint, context):
    # Recheck immediately before executing, including deletion/change since resolution.
    current=bundle(item['folder'].parent.parent,item['id'])
    if current['hash']!=item['hash'] or not current['manifest'].get('enabled'):raise ValueError('Extension changed or disabled before execution')
    if not re.fullmatch(r'[A-Za-z0-9_./-]+\.py:[A-Za-z_][A-Za-z0-9_]*',entrypoint):raise ValueError('Use relative module.py:function entrypoints')
    path=item['folder']/entrypoint.split(':')[0]
    if not path.resolve().is_relative_to(item['folder'].resolve()) or path.is_symlink():raise ValueError('Extension entrypoint escaped its bundle')
    try:
        result=subprocess.run([sys.executable,'-m','monitor.extension_worker'],input=json.dumps(dict(folder=str(item['folder']),entrypoint=entrypoint,context=context)),
                              text=True,encoding='utf-8',capture_output=True,timeout=item['manifest'].get('timeout_seconds',30))
    except subprocess.TimeoutExpired:raise ValueError('Extension timed out; reconcile any possible external effects before retry') from None
    if result.returncode or len(result.stdout)>1_000_000:raise ValueError('Extension failed; reconcile before retry: '+result.stderr[-500:])
    try:value=json.loads(result.stdout)
    except ValueError:raise ValueError('Extension must return one JSON object') from None
    if not isinstance(value,dict):raise ValueError('Extension result must be an object')
    return value


def event(h,name,context):
    for item in available(h=h):
        entry=item['manifest'].get('hooks',{}).get(name)
        if not item['active'] or not entry:continue
        key='extension-observations:'+item['id']
        value=invoke(item,entry,dict(context,observations=metadata(h.db,key),now=clock()))
        if 'observations' in value:
            with h.write():metadata(h.db,key,value['observations'])


def override(h,command,context):
    for item in available(h=h):
        entry=item['manifest'].get('commands',{}).get(command)
        if item['active'] and entry:return invoke(item,entry,context)
    return None


def project(h,card):
    for item in available(h=h):
        entry=item['manifest'].get('hooks',{}).get('card_context')
        if item['active'] and entry:
            result=invoke(item,entry,dict(card=card))
            projected=result.get('card',card)
            if not isinstance(projected,dict) or projected.get('id')!=card['id']:raise ValueError('Card projection must preserve its identity')
            card=projected
    return card


def run_action(h,ident,payload):
    from .approvals import find
    from .model import digest,now
    row=h.db.execute('SELECT case_id,details FROM actions WHERE id=?',(ident,)).fetchone()
    if not row:raise ValueError('Unknown action')
    card=h.case(row['case_id']);action=json.loads(row['details']);reference=action.get('custom_action')
    if not reference:raise ValueError('Action has no custom implementation')
    config=resolve(card=card);items=available(config,h)
    item=next((i for i in items if i['id']==reference['bundle'] and i['active']),None)
    if not item:raise ValueError('Custom action blocked: bundle missing, expired, disabled or unapproved')
    definition=item['manifest'].get('actions',{}).get(reference['action'])
    if not definition:raise ValueError('Custom action is not declared by its bundle')
    args=reference.get('parameters',{});schema=definition.get('parameters',{})
    if set(args)-set(schema):raise ValueError('Unknown custom action parameter')
    types={'string':str,'integer':int,'boolean':bool,'object':dict,'array':list}
    for key,rule in schema.items():
        if rule.get('required') and key not in args:raise ValueError('Missing custom action parameter: '+key)
        if key in args and type(args[key]) is not types.get(rule['type']):raise ValueError('Invalid custom action parameter: '+key)
    grant=find(h,payload.get('approval_id') or reference.get('approval_id'))[2];scope=grant.get('scope',{})
    if grant.get('status','approved')!='approved' or grant.get('suspended') or grant.get('expires_at','9999')<=now():raise ValueError('Custom action execution permission is inactive')
    if grant.get('case_id')!=card['id'] or scope.get('operation')!='custom-action' or scope.get('bundle')!=item['id'] or scope.get('action')!=reference['action']:raise ValueError('Custom action needs exact card-bound execution permission')
    key=payload.get('idempotency_key')
    if not isinstance(key,str) or not key:raise ValueError('Custom actions require an idempotency_key')
    key='custom-action:'+key;fingerprint=digest([ident,reference,item['hash'],grant['id']])
    with h.write():
        previous=metadata(h.db,key)
        if previous:
            if previous['fingerprint']!=fingerprint:raise ValueError('Idempotency key scope changed')
            if previous['stage']=='complete':return dict(previous['result'],duplicate_prevented=True)
            raise ValueError('Prior execution is uncertain; reconcile before running again')
        metadata(h.db,key,dict(stage='attempting',fingerprint=fingerprint))
    with snapshot(effective(config,h,card)):
        result=invoke(item,definition['entrypoint'],dict(parameters=args,card=card,action=action,settings=config.values))
        receipts=[]
        from outlook_platforms.api import invoke as outlook
        for request in result.pop('requests',[]):receipts.append(outlook(request['operation'],request.get('payload',{}),h))
        result['receipts']=receipts
    with h.write():metadata(h.db,key,dict(stage='complete',fingerprint=fingerprint,result=result))
    return result
