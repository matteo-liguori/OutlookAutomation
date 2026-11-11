"""Profile-selected Outlook interface; native access stays in platform adapters."""
import hashlib
import json
import platform
from datetime import datetime
from pathlib import Path
from monitor.profile import PROFILE, DEFAULT_PROVIDERS, platform_settings, source_backend

OPERATIONS = ('status','probe','discover','accounts','stores','folders','recent','search',
              'read','collect','calendar','calendars','calendar-read','calendar-create','calendar-update','calendar-delete','refresh','freshness','session','contacts','duplicates','draft-save','draft-update','draft-reconcile','draft-inspect','draft-discard','send','send-reconcile','move','move-reconcile','trash','trash-reconcile')
MAC_FIELDS = {
    'probe':['check_permission','app_path'], 'discover':['source'], 'accounts':['source'],
    'folders':['source'],
    'recent':['source','start','end','limit','subject','unread_only','include_preview','folder','folder_id'],
    'search':['source','start','end','limit','query','unread_only','include_preview','folder','folder_id','max_scan'],
    'read':['source','start','end','limit','entry_id','subject','unread_only','folder','folder_id','case_id','force_refresh','download_large_attachments','download_attachments'],
    'collect':['source','start','end','page_size','max_scan','offset','snapshot_id','ingest_run','freshness'],
    'freshness':['source','freshness_minutes','interval_seconds','max_wait_seconds'],
    'draft-save':['source','approval_id','to','cc','bcc','subject','body','attachments','display_only'],
    'draft-update':['source','approval_id','to','cc','bcc','subject','body','attachments','entry_id'],
    'send':['source','approval_id','to','cc','bcc','subject','body','attachments'],
    'move':['source','approval_id','entry_id'],
    'calendar':['source','start','end','limit'], 'calendars':['source'],
}


def _outlook_status(profile=PROFILE,details=False):
    config=platform_settings(profile)
    host={'Windows':'windows','Darwin':'macos'}.get(platform.system(),platform.system().lower())
    from .windows.commands import JSON_HELPERS
    fields = ({op:list(values) for op,values in MAC_FIELDS.items()}
              if config['os']=='macos' else {op:['source',*spec[1]] for op,spec in JSON_HELPERS.items()})
    if config['os']=='windows':fields['send'].append('approval_id')
    # The public collection contract is shared; preparation/ingestion is internal.
    fields['collect']=['source','start','end','page_size','max_scan','offset','snapshot_id','run_id']
    hidden={'probe','session','refresh','move'}
    operations=sorted(set(fields)-hidden) if host==config['os'] else []
    sources={op:list(profile['mail']) for op in operations}
    for op in ('calendar','calendars'):
        if op in sources:sources[op]=[s for s in profile['calendars'] if {**DEFAULT_PROVIDERS,**profile.get('calendar_providers',{})}.get(s)=='outlook']
    if config['os']=='windows' and 'calendar' in sources:
        sources['calendar']=[s for s in sources['calendar'] if s==profile.get('roles',{}).get('work_calendar','calendar:work')]
    if config['os']=='macos':
        from monitor.profile import native_bindings
        for op in operations:
            if op=='discover':continue
            usable=[]
            for source in sources[op]:
                mail_source=(config.get('bindings',{}).get(source,{}).get('mail_source',profile.get('roles',{}).get('work_mail','mail:work')) if source in profile['calendars'] else source)
                try:bound=native_bindings(profile,profile['mail'][mail_source],source)
                except (ValueError,KeyError):continue
                required=['calendar_id'] if op in {'calendar','calendars'} else ['inbox_id']
                if op in {'send','draft-save'}:required.append('draft_folder_id')
                if all(bound.get(key) for key in required):usable.append(source)
            sources[op]=usable
    operations=[op for op in operations if sources[op]]
    public=dict(operations=operations,payload_fields={op:fields[op] for op in operations},sources={op:sources[op] for op in operations},ready=host==config['os'])
    if not details:return public
    return dict(public,platform=config['os'],backend=config['outlook_backend'],host=host,
                host_matches=host==config['os'],
                bindings=config.get('bindings',{}),
                backend_operations=sorted(fields),
                known_limits=({'move':'Native Gmail move failed its live verification; do not treat filing as working.',
                               'send':'Submission is not delivery; native automatic flushing remains unverified.'} if config['os']=='macos' else {}),
                instruction='Use help outlook for payloads and capability limits. A listed operation does not grant permission.')


def status(profile=PROFILE,details=False):
    info=_outlook_status(profile,details=True)
    config=platform_settings(profile)
    if config['os']!='macos':return info if details else {k:info[k] for k in ('operations','payload_fields','sources','ready')}
    from .thunderbird.commands import FIELDS
    sources={op:[s for s in values if source_backend(profile,s)!='thunderbird'] for op,values in info['sources'].items()}
    fields={op:keys for op,keys in info['payload_fields'].items() if sources.get(op)}
    for op,keys in FIELDS.items():
        candidates=profile['calendars'] if op.startswith('calendar') else profile['mail']
        for source in candidates:
            if not info['host_matches'] or source_backend(profile,source)!='thunderbird':continue
            binding=config.get('bindings',{}).get(source,{})
            required=['calendar_id'] if op.startswith('calendar') else [] if op=='discover' else ['account_id','selected_inbox_id']
            if op in {'send','draft-save','draft-update'}:required.extend(['sent_folder_id','draft_folder_id'])
            if op=='contacts':required.append('address_book_ids')
            if op in {'trash','trash-reconcile'}:required.append('trash_folder_id')
            if all(binding.get(key) for key in required):
                sources.setdefault(op,[]).append(source)
                fields[op]=sorted(set(fields.get(op,[]))|set(keys))
    operations=sorted(op for op,values in sources.items() if values)
    info.update(operations=operations,sources={op:sources[op] for op in operations},payload_fields={op:fields[op] for op in operations},
                backend_operations=sorted(set(info['backend_operations'])|set(FIELDS)),
                source_backends={source:source_backend(profile,source) for source in [*profile['mail'],*profile['calendars']]})
    if 'thunderbird' in info['source_backends'].values():
        for key in ('move','send'):
            if key in info['known_limits']:info['known_limits']['mac_outlook_'+key]=info['known_limits'].pop(key)
        info['known_limits']['thunderbird']='Thunderbird must be running. Draft reopening and sync need the privileged client module. Address books must be explicitly bound. Calendar invitation responses remain unsupported. Freshness requires a successful provider callback and recent stable Inbox samples.'
    return info if details else {k:info[k] for k in ('operations','payload_fields','sources','ready')}


def available_operations(profile=PROFILE):
    return ['status',*status(profile)['operations']]


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
        if key in payload and payload[key]!=expected.get(key,False if key=='high_importance' else ''):raise ValueError('Approved message mismatch: '+key)
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
    for key in ('unread_only','include_preview','force_refresh','download_large_attachments','download_attachments','display_only','check_permission','high_importance','include_recipient_cache','content_hash'):
        if key in payload and type(payload[key]) is not bool:raise ValueError(key+' must be a boolean')
    info=status(profile,details=True)
    if operation=='status':return status(profile)
    if not info['host_matches']:
        raise ValueError('Configured Outlook platform does not match this host; no other backend was tried')
    if operation not in info['operations']:
        return dict(status='unsupported',operation=operation,
                    instruction='This adapter does not implement this operation yet; no fallback or access was attempted.')
    if set(payload)-set(info['payload_fields'][operation]):
        raise ValueError('Unsupported '+operation+' field: '+', '.join(sorted(set(payload)-set(info['payload_fields'][operation]))))
    config=platform_settings(profile)
    source=payload.pop('source',None)
    if operation.startswith('calendar'):
        source=source or profile.get('roles',{}).get('work_calendar','calendar:work')
        providers={**DEFAULT_PROVIDERS,**profile.get('calendar_providers',{})}
        if source not in profile['calendars'] or (providers.get(source)!='outlook' and source_backend(profile,source)!='thunderbird'):
            raise ValueError('Select an enabled Outlook calendar source')
        if info['platform']=='windows' and source!=profile.get('roles',{}).get('work_calendar','calendar:work'):
            raise ValueError('Windows calendar helper currently implements only the configured work calendar')
        binding=config.get('bindings',{}).get(source,{})
        mail_source=binding.get('mail_source',profile.get('roles',{}).get('work_mail','mail:work'))
    else:
        mail_source=source or profile.get('roles',{}).get('work_mail',next(iter(profile['mail'])))
        binding=config.get('bindings',{}).get(mail_source,{})
    if mail_source not in profile['mail']:raise ValueError('Select a configured mailbox source')
    if (source or mail_source) not in info['sources'][operation]:raise ValueError('This source needs setup for the requested operation')
    store=profile['mail'][mail_source]
    selected_backend=source_backend(profile,source or mail_source)
    if selected_backend=='thunderbird':
        from .thunderbird.commands import FIELDS
        if set(payload)-set(FIELDS[operation]):raise ValueError('Unsupported Thunderbird field')
    # No arbitrary executable paths or account overrides arrive from request data.
    if operation in {'send','draft-save','draft-update'}:
        if h and payload.get('approval_id'):
            from monitor.approvals import find
            approved=find(h,payload['approval_id'])[2].get('message',{})
            for key in ('to','cc','bcc','subject','body','high_importance','reply_to_entry_id'):
                if key not in payload and key in approved:payload[key]=approved[key]
            if 'attachments' not in payload:
                if any(not isinstance(item,dict) or 'path' not in item for item in approved.get('attachments',[])):
                    raise ValueError('Record approved attachment paths and hashes before sending')
                payload['attachments']=[item['path'] for item in approved.get('attachments',[])]
        check_send(h,payload,store)
    def backend(op,values):
        if info['platform']=='windows':
            from .windows.commands import execute_json
            return execute_json(op,values,store,h)
        if selected_backend=='thunderbird':
            from .thunderbird.commands import execute_json
            return execute_json(op,values,store,h,source or mail_source,profile=profile)
        if op in {'probe','discover'}:return mac_invoke(op,values,store)
        from .macos.commands import execute_json
        return execute_json(op,values,store,h,source or mail_source,profile=profile)
    if operation=='collect':result=collect_workflow(h,payload,backend,info['platform'],source or mail_source)
    elif operation=='send' and info['platform']=='windows':
        if not prepare_transport(h,backend):return dict(operation=operation,source=source or mail_source,status='blocked',data={'Status':'not-submitted','Verified':False})
        result=backend(operation,payload)
        # Complete the helper's send-session contract internally, not via an
        # OS-specific instruction for the agent.
        prepare_transport(h,backend,force=True)
    else:result=backend(operation,payload)
    if operation=='freshness' and isinstance(result['data'],list):
        if len(result['data'])!=1:raise ValueError('Expected one source freshness result')
        result['data']=result['data'][0]
    if operation=='send' and isinstance(result['data'],dict):
        data=result['data']
        data.pop('NextStep',None)
        data.setdefault('Verified',data.get('delivery_verified') is True)
        data.setdefault('NextStep','Record the verified self-delivery outcome.' if data.get('delivery_verified') else 'Sent copy verified; recipient delivery is not confirmed.' if data.get('sent_verified') else 'Verify the unique delivered/Sent message before recording completion.')
    result.setdefault('status','ok')
    return dict(operation=operation,source=source or mail_source,**result)


def prepare_transport(h,backend,force=False):
    from monitor.model import now
    row=h.db.execute("SELECT value FROM meta WHERE key='outlook:last-refresh'").fetchone() if h else None
    receipt=json.loads(row[0]) if row else {}
    at=datetime.fromisoformat(now())
    recent=receipt.get('success') is True and receipt.get('completed_at') and 0<=(at-datetime.fromisoformat(receipt['completed_at'])).total_seconds()<=900
    return True if recent and not force else backend('refresh',{})['data'].get('success') is True


def collect_workflow(h,payload,backend,selected,source):
    values=dict(payload);run_id=values.pop('run_id',None)
    first=not values.get('offset',0)
    if run_id and (not h or not first):raise ValueError('Run ingestion requires page zero and an open harness')
    if run_id:h.run(run_id)
    freshness=None
    if first:
        if selected=='windows':
            if not prepare_transport(h,backend):return dict(status='blocked',data=dict(Complete=False,Errors=['Source preparation did not complete.']))
        freshness=backend('freshness',dict(interval_seconds=15,max_wait_seconds=30))['data']
        if isinstance(freshness,list):
            if len(freshness)!=1:raise ValueError('Expected one source freshness result')
            freshness=freshness[0]
    page=backend('collect',values)['data']
    result=dict(data=page,freshness=freshness,status='ok' if page.get('Complete') and freshness and freshness.get('Current') else 'partial')
    if run_id:
        from monitor.collect import import_pages
        pages=[page];seen=set()
        while pages[-1].get('NextOffset') is not None:
            offset=pages[-1]['NextOffset']
            if offset in seen:raise ValueError('Repeated collection offset')
            seen.add(offset)
            pages.append(backend('collect',dict(values,offset=offset,snapshot_id=page['SnapshotId']))['data'])
        result['ingestion']=import_pages(h,run_id,source,pages,page['StartedAt'],freshness)
        result['data']={k:v for k,v in page.items() if k!='Items'}
        result['data'].update(Count=sum(p['Count'] for p in pages),NextOffset=None,
                              Complete=pages[-1]['Complete'],run_id=run_id)
        result['status']='ok' if pages[-1]['Complete'] and freshness and freshness.get('Current') else 'partial'
    return result


def mac_invoke(operation,payload,store):
    if operation=='probe':
        from mac_outlook import probe
        if set(payload)-{'check_permission','app_path'}:raise ValueError('Unsupported probe field')
        return dict(data=probe(**payload))
    from .macos.native import request_for,execute
    if operation!='discover' or payload:raise ValueError('Unsupported discovery field')
    return dict(data=execute(request_for('discover',store)))
