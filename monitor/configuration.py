"""Layered behaviour, round-trip YAML and non-destructive default reconciliation."""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from datetime import time
import hashlib
import io
from functools import lru_cache
import json
import os
import uuid
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ruamel.yaml import YAML
from ruamel.yaml.scalarstring import LiteralScalarString
from monitor.paths import ROOT, data_root

DEFAULT_FILE = ROOT / 'defaults/behaviour.yaml'
_current = ContextVar('behaviour', default=None)


def yaml():
    y = YAML(); y.preserve_quotes = True; y.width = 4096
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def read_yaml(path):
    try:
        value = yaml().load(Path(path).read_text(encoding='utf-8-sig'))
    except Exception as exc:
        raise ValueError(f'Invalid YAML in {path}: {exc}') from exc
    if not isinstance(value, dict):raise ValueError(f'{path} must contain a mapping')
    return value


def plain(value):
    if isinstance(value, dict):return {str(k):plain(v) for k,v in value.items()}
    if isinstance(value, list):return [plain(v) for v in value]
    return value


@lru_cache(maxsize=8)
def _defaults(path, modified):
    return plain(read_yaml(path))


def defaults(path=None):
    path=Path(path or DEFAULT_FILE)
    return deepcopy(_defaults(str(path),path.stat().st_mtime_ns))


def location(root=None):
    if root is not None:return Path(root)
    if os.environ.get('OUTLOOK_AUTOMATION_CUSTOMISATIONS'):
        return Path(os.environ['OUTLOOK_AUTOMATION_CUSTOMISATIONS']).resolve().parent
    # Explicit synthetic/alternate profiles do not inherit the workstation owner's settings.
    if os.environ.get('OUTLOOK_AUTOMATION_PROFILE'):return None
    return data_root()


def paths(root):
    return (Path(os.environ['OUTLOOK_AUTOMATION_CUSTOMISATIONS']) if os.environ.get('OUTLOOK_AUTOMATION_CUSTOMISATIONS') else root/'customisations.yaml', root/'.state/defaults.json')


def leaves(value, prefix=''):
    for key,v in value.items():
        path = prefix+'.'+key if prefix else key
        if isinstance(v, dict):yield from leaves(v, path)
        else:yield path,v


def assign(value, path, item):
    keys = path.split('.'); parent=value
    for key in keys[:-1]:parent=parent.setdefault(key,{})
    parent[keys[-1]] = LiteralScalarString(item) if isinstance(item,str) and '\n' in item else deepcopy(item)


def validate(value, base=None, partial=False):
    base=base or defaults(); known=dict(leaves(base))
    def keys(mapping,reference,prefix=''):
        for key,item in mapping.items():
            path=prefix+'.'+key if prefix else key
            if key not in reference:raise ValueError('Unknown customisation: '+path)
            if isinstance(item,dict):
                if not isinstance(reference[key],dict):raise ValueError('Wrong customisation type: '+path)
                keys(item,reference[key],path)
    keys(value,base)
    for key,v in leaves(value):
        if key not in known:raise ValueError('Unknown customisation: '+key)
        expected=known[key]
        if type(v) is not type(expected) and not (isinstance(v,str) and isinstance(expected,str)) and not (isinstance(v,list) and isinstance(expected,list)):
            raise ValueError('Wrong customisation type: '+key)
        if isinstance(v,str) and not v.strip():raise ValueError('Empty customisation: '+key)
        if isinstance(v,int) and not isinstance(v,bool) and v<0:raise ValueError('Negative customisation: '+key)
    if partial:return
    if value['schema_version']!=1:raise ValueError('Unsupported behaviour schema; run migration preview')
    import re
    if not 1<=value['updates']['interval_hours']<=168 or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*',value['updates']['remote']):raise ValueError('Invalid update interval or Git remote name')
    try:ZoneInfo(value['timezone'])
    except (ZoneInfoNotFoundError,ValueError):raise ValueError('Unknown timezone: '+value['timezone']) from None
    schedule=value['schedule']
    for v in [schedule['morning'],*schedule['continuations'],schedule['roundup_start'],schedule['roundup_end']]:
        if not isinstance(v,str) or len(v)!=5 or time.fromisoformat(v).tzinfo:raise ValueError('Schedule times need local HH:MM values')
    if len(schedule['continuations'])>8 or len(set(schedule['continuations']))!=len(schedule['continuations']):raise ValueError('Use up to eight distinct continuation times')
    if not 0<=schedule['roundup_weekday']<=6 or schedule['roundup_start']>=schedule['roundup_end']:raise ValueError('Invalid roundup slot')
    if not 0<=value['reviews']['hour']<=23 or not 0<=value['reviews']['upgrades_after_hour']<=23:raise ValueError('Invalid review hour')
    for k in ('card_days','followup_days','urgent_max_days','upgrades_days'):
        if not 1<=value['reviews'][k]<=90:raise ValueError('Review interval must be 1..90: '+k)
    if any(v<1 for v in value['maintenance'].values()):raise ValueError('Maintenance retention/counts must be positive')
    for k,v in value['permission_defaults'].items():
        expected=base['permission_defaults'][k]
        if isinstance(expected,str) and v not in (expected,'ask-every-time'):raise ValueError('Unsupported permission default: '+k)


class Configuration:
    def __init__(self, values, origins, root=None, reviews=None):
        self.values=values;self.origins=origins;self.root=root;self.reviews=reviews or []

    def get(self,key):return dict(leaves(self.values))[key]

    def instruction(self, topic):
        text=self.values['instructions'][topic]
        for old,new in [('Europe/London',self.values['timezone']),('14:00/19:00','/'.join(self.values['schedule']['continuations'])),('09:00',self.values['schedule']['morning'])]:
            text=text.replace(old,new)
        for key,value in leaves(self.values):
            if not key.startswith('instructions.') and not key.startswith('templates.'):
                text=text.replace('{{'+key+'}}',str(value))
        if self.values['timezone']!='Europe/London':text=text.replace('London',self.values['timezone'])
        return text

    def overlay(self, patch, origin):
        validate(patch,partial=True)
        values=deepcopy(self.values);origins=dict(self.origins)
        for key,v in leaves(patch):assign(values,key,v);origins[key]=origin
        validate(values)
        return Configuration(values,origins,self.root,self.reviews)


def resolve(root=None, profile=None, default_file=None, card=None):
    if root is None and profile is None and default_file is None and _current.get() is not None:
        current=_current.get()
        if card and card.get('customisations'):
            if set(card['customisations'])-{'instructions','reviews','templates','permission_defaults'}:raise ValueError('Invalid card customisation scope')
            return current.overlay(card['customisations'],'card:'+card['id'])
        return current
    base=defaults(default_file);values=deepcopy(base);origins={k:'default' for k,_ in leaves(base)}
    if profile is None:
        from .profile import PROFILE
        profile=PROFILE
    # Migration compatibility: read existing overrides without rewriting the profile.
    legacy={k:profile[k] for k in ('timezone','instructions','permission_defaults') if k in profile}
    for key,v in leaves(legacy):
        if key in origins:assign(values,key,v);origins[key]='legacy-profile'
    folder=location(root);reviews=[]
    if folder is not None:
        custom,state_path=paths(folder)
        if custom.exists():
            user=read_yaml(custom);state=json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {}
            old=dict(leaves(state.get('baseline',base)));new=dict(leaves(base))
            for key,v in leaves(user):
                if key not in new:
                    # A fuller/newer checkout may have prefilled this setting.
                    # Only a recorded, unedited default is safe to leave dormant.
                    if key in old and v==old[key]:continue
                    raise ValueError('Removed/unknown setting needs migration: '+key)
                if key in old and v==old[key]:continue
                assign(values,key,v);origins[key]='user'
            reviews=state.get('reviews',[])
    validate(values,base)
    result=Configuration(values,origins,folder,reviews)
    if card and card.get('customisations'):
        patch=card['customisations']
        if set(patch)-{'instructions','reviews','templates','permission_defaults'}:raise ValueError('Card customisations are limited to instructions, reviews, templates and permission defaults')
        result=result.overlay(patch,'card:'+card['id'])
    return result


@contextmanager
def snapshot(config=None):
    token=_current.set(config or resolve())
    try:yield _current.get()
    finally:_current.reset(token)


def zone():return ZoneInfo(resolve().values['timezone'])


def atomic(path, text):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        with temporary.open('x',encoding='utf-8',newline='\n') as f:f.write(text)
        os.replace(temporary,path)
    finally:temporary.unlink(missing_ok=True)


def dump(value):
    stream=io.StringIO();yaml().dump(value,stream);return stream.getvalue()


@contextmanager
def lock(root):
    folder=Path(root)/'.state';folder.mkdir(parents=True,exist_ok=True)
    path=folder/'configuration.lock'
    with path.open('a+b') as handle:
        if path.stat().st_size==0:handle.write(b' ');handle.flush()
        handle.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:raise ValueError('Configuration is being updated; retry after the current operation') from None
        try:yield
        finally:
            handle.seek(0)
            if os.name=='nt':msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
            else:fcntl.flock(handle,fcntl.LOCK_UN)


def sync(root=None, profile=None, default_file=None):
    folder=location(root)
    if folder is None:return []
    with lock(folder):
        base=defaults(default_file);custom,state_path=paths(folder)
        initial=custom.read_bytes() if custom.exists() else None
        state=json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {}
        user=read_yaml(custom) if custom.exists() else read_yaml(default_file or DEFAULT_FILE)
        old=dict(leaves(state.get('baseline',base)));new=dict(leaves(base));existing=dict(leaves(user));reviews=state.get('reviews',[])
        if not custom.exists() and profile:
            for section in ('timezone','instructions','permission_defaults'):
                if section in profile:
                    for key,v in leaves({section:profile[section]}):assign(user,key,v)
            existing=dict(leaves(user))
        for key,v in existing.items():
            if key not in new and key in old and v==old[key]:
                parent=user
                parts=key.split('.')
                for part in parts[:-1]:parent=parent[part]
                del parent[parts[-1]]
        for key,v in new.items():
            if key not in existing or existing[key]==old.get(key):assign(user,key,v)
            elif old.get(key)!=v and existing[key]!=v:
                ident=hashlib.sha256(json.dumps([key,v],sort_keys=True).encode()).hexdigest()[:16]
                if not any(r['id']==ident for r in reviews):reviews.append(dict(id=ident,key=key,override=plain(existing[key]),new_default=plain(v),notified=False))
        validate(plain(user),base)
        current=dict(leaves(user))
        reviews=[dict(r,override=plain(current[r['key']])) for r in reviews if r['key'] in current and current[r['key']]!=r['new_default']]
        text=dump(user)
        if (custom.read_bytes() if custom.exists() else None)!=initial:raise ValueError('Customisations changed during reconciliation; retry without overwriting the edit')
        if not custom.exists() or custom.read_text(encoding='utf-8')!=text:atomic(custom,text)
        fingerprint=hashlib.sha256(json.dumps([user['timezone'],plain(user['schedule'])],sort_keys=True).encode()).hexdigest()
        if state.get('schedule_fingerprint') and state['schedule_fingerprint']!=fingerprint:
            state['schedule_notice']=dict(timezone=user['timezone'],schedule=plain(user['schedule']),notified=False)
        atomic(state_path,json.dumps(dict(state,baseline=base,reviews=reviews,schedule_fingerprint=fingerprint),indent=2)+'\n')
        return reviews


def review(root=None, ident=None, decision=None):
    folder=location(root)
    if folder is None:return []
    custom,path=paths(folder)
    with lock(folder):
        state=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'reviews':[]}
        if ident:
            item=next((r for r in state['reviews'] if r['id']==ident),None)
            if not item or decision not in ('keep','default'):raise ValueError('Select a pending review and keep/default decision')
            if decision=='default':
                user=read_yaml(custom);assign(user,item['key'],item['new_default']);validate(plain(user));atomic(custom,dump(user))
            state['reviews'].remove(item);atomic(path,json.dumps(state,indent=2)+'\n')
        return state['reviews']


def notices(root=None):
    folder=location(root)
    if folder is None:return []
    _,path=paths(folder)
    if not path.exists():return []
    with lock(folder):
        state=json.loads(path.read_text(encoding='utf-8'));pending=[r for r in state.get('reviews',[]) if not r['notified']]
        for item in pending:item['notified']=True
        schedule=state.get('schedule_notice',{})
        changed=bool(schedule and not schedule['notified'])
        if changed:schedule['notified']=True
        if pending or changed:atomic(path,json.dumps(state,indent=2)+'\n')
        results=[dict(type='configuration_review',**{k:r[k] for k in ('id','key','override','new_default')},instruction='User override retained. Use config review to keep it or adopt the new default.') for r in pending]
        if changed:results.append(dict(type='schedule_reconciliation',timezone=schedule['timezone'],schedule=schedule['schedule'],instruction='Desired schedule changed. Reconcile the actual morning automation and same-day chat follow-ups through app tools, read back and verify; configuration alone did not update the app.'))
        return results
