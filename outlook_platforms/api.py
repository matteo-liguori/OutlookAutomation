"""Profile-selected Outlook interface; native access stays in platform adapters."""
import hashlib
import json
import platform
from datetime import datetime
from pathlib import Path
from monitor.profile import PROFILE, DEFAULT_PROVIDERS, platform_settings

OPERATIONS = ('status','probe','discover','accounts','stores','folders','recent','search',
              'read','collect','calendar','calendars','refresh','session','contacts','duplicates','send')
MAC_FIELDS = {
    'probe':['check_permission','app_path'], 'discover':['source'], 'accounts':['source'],
    'recent':['source','start','end','limit','subject','unread_only','include_preview'],
    'read':['source','start','end','limit','entry_id','subject','unread_only'],
    'calendar':['source','start','end','limit'], 'calendars':['source'],
}


def status(profile=PROFILE):
    config=platform_settings(profile)
    host={'Windows':'windows','Darwin':'macos'}.get(platform.system(),platform.system().lower())
    from .windows.commands import JSON_HELPERS
    fields = ({op:list(values) for op,values in MAC_FIELDS.items()}
              if config['os']=='macos' else {op:['source',*spec[1]] for op,spec in JSON_HELPERS.items()})
    if config['os']=='windows':fields['send'].append('approval_id')
    return dict(platform=config['os'],backend=config['outlook_backend'],host=host,
                host_matches=host==config['os'],
                operations=sorted(MAC_FIELDS if config['os']=='macos' else JSON_HELPERS),
                bindings=config.get('bindings',{}),
                payload_fields=fields,
                instruction='Use help outlook for payloads and capability limits. A listed operation does not grant permission.')


def check_send(h, payload, sender):
    from monitor.approvals import find
    if not h or not payload.get('approval_id'):
        raise ValueError('Record the exact approved message with approval put, then supply approval_id')
    kind,_,approval=find(h,payload['approval_id'])
    if kind!='one-off' or approval.get('status')!='approved' or approval.get('suspended') or not approval.get('message'):
        raise ValueError('Send needs an active exact-message approval; materialise any applicable standing scope without asking again')
    from monitor.model import now
    if approval.get('expires_at') and approval['expires_at']<=now():
        raise ValueError('Message approval has expired')
    expected=approval['message']
    actual=dict(sender=sender,to=payload.get('to',[]),cc=payload.get('cc',[]),bcc=payload.get('bcc',[]),
                subject=payload.get('subject',''),body=payload.get('body',''))
    for key,value in actual.items():
        if expected.get(key)!=value:
            raise ValueError('Approved message mismatch: '+key)
    for key in ('high_importance','reply_to_entry_id'):
        if payload.get(key) and expected.get(key)!=payload[key]:raise ValueError('Approved message mismatch: '+key)
    attachments=payload.get('attachments',[])
    wanted=expected.get('attachments',[])
    if len(attachments)!=len(wanted):raise ValueError('Approved attachment count mismatch')
    for path,item in zip(attachments,wanted):
        if not isinstance(item,dict) or 'sha256' not in item:
            raise ValueError('Record approved attachment paths and SHA256 hashes')
        if Path(path).resolve()!=Path(item['path']).resolve() or hashlib.sha256(Path(path).read_bytes()).hexdigest()!=item['sha256']:
            raise ValueError('Approved attachment changed')


def invoke(operation,payload=None,h=None,profile=PROFILE):
    if operation not in OPERATIONS:raise ValueError('Unknown Outlook operation')
    payload=dict(payload or {})
    for key in ('start','end'):
        if key in payload:
            try:point=datetime.fromisoformat(payload[key])
            except (ValueError,TypeError):raise ValueError(key+' needs an ISO timestamp with an offset') from None
            if point.utcoffset() is None:raise ValueError(key+' needs an explicit timezone offset')
    for key in ('limit','max_scan','page_size'):
        if key in payload and (type(payload[key]) is not int or not 1<=payload[key]<=5000):raise ValueError('Invalid '+key)
    for key in ('unread_only','include_preview','force_refresh','download_large_attachments','check_permission','high_importance','include_recipient_cache'):
        if key in payload and type(payload[key]) is not bool:raise ValueError(key+' must be a boolean')
    info=status(profile)
    if operation=='status':return info
    if not info['host_matches']:
        raise ValueError('Configured Outlook platform does not match this host; no other backend was tried')
    if operation not in info['operations']:
        return dict(status='unsupported',platform=info['platform'],operation=operation,
                    instruction='This adapter does not implement this operation yet; no fallback or access was attempted.')
    if set(payload)-set(info['payload_fields'][operation]):
        raise ValueError('Unsupported '+operation+' field: '+', '.join(sorted(set(payload)-set(info['payload_fields'][operation]))))
    config=platform_settings(profile)
    source=payload.pop('source',None)
    if operation in {'calendar','calendars'}:
        source=source or profile.get('roles',{}).get('work_calendar','calendar:work')
        providers={**DEFAULT_PROVIDERS,**profile.get('calendar_providers',{})}
        if source not in profile['calendars'] or providers.get(source)!='outlook':
            raise ValueError('Select an enabled Outlook calendar source')
        if info['platform']=='windows' and source!=profile.get('roles',{}).get('work_calendar','calendar:work'):
            raise ValueError('Windows calendar helper currently implements only the configured work calendar')
        binding=config.get('bindings',{}).get(source,{})
        mail_source=binding.get('mail_source',profile.get('roles',{}).get('work_mail','mail:work'))
    else:
        mail_source=source or profile.get('roles',{}).get('work_mail',next(iter(profile['mail'])))
        binding=config.get('bindings',{}).get(mail_source,{})
    if mail_source not in profile['mail']:raise ValueError('Select a configured mailbox source')
    store=profile['mail'][mail_source]
    # No arbitrary executable paths or account overrides arrive from request data.
    if operation=='send':check_send(h,payload,store)
    if info['platform']=='windows':
        from .windows.commands import execute_json
        result=execute_json(operation,payload,store,h)
    else:
        result=mac_invoke(operation,payload,store,binding,source)
    return dict(operation=operation,platform=info['platform'],backend=info['backend'],source=source or mail_source,**result)


def mac_invoke(operation,payload,store,binding,source):
    if operation=='probe':
        from mac_outlook import probe
        if set(payload)-{'check_permission','app_path'}:raise ValueError('Unsupported probe field')
        return dict(data=probe(**payload))
    from .macos.native import request_for,execute
    from .macos.commands import normalize
    allowed={'start','end','limit','subject','entry_id','unread_only','include_preview'}
    if set(payload)-allowed:raise ValueError('Unsupported native request field')
    names={'discover':'discover','accounts':'check','recent':'mail','read':'mail','calendar':'calendar','calendars':'calendars'}
    if operation in {'recent','read','accounts'} and not binding.get('selected_inbox_id'):
        raise ValueError('Configure the verified selected_inbox_id in platform.bindings for this mailbox')
    if operation in {'calendar','calendars'} and not binding.get('calendar_id'):
        raise ValueError('Configure the verified calendar_id in platform.bindings for this source')
    if operation=='read' and not payload.get('entry_id'):raise ValueError('read requires entry_id')
    request=request_for(names[operation],store,start=payload.get('start'),end=payload.get('end'),
                        limit=payload.get('limit',25),subject=payload.get('subject'),
                        include_body=operation=='read' or payload.get('include_preview',False),
                        selected_inbox_id=binding.get('selected_inbox_id') if operation in {'accounts','recent','read'} else None,
                        calendar_id=binding.get('calendar_id') if operation in {'calendar','calendars'} else None,
                        entry_id=payload.get('entry_id'),unread_only=payload.get('unread_only',False))
    raw=execute(request)
    if raw.get('status')!='ok':return dict(status=raw.get('status','blocked'),data=raw)
    commands={'accounts':'Inspect-Outlook','recent':'Read-OutlookMail','read':'Get-OutlookMessage',
              'calendar':'Read-OutlookMonitorCalendar','calendars':'List-OutlookCalendars'}
    data=normalize(commands[operation],raw,store) if operation in commands else raw
    if operation=='calendar':data['source']=source
    coverage={k:v for k,v in raw.items() if k not in {'items','calendars'}}
    return dict(data=data,coverage=coverage)
