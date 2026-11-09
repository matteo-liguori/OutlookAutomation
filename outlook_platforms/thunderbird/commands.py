"""Thunderbird implementation of the shared harness command contract."""
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
import base64
import json
import re
import uuid

from .native_host import request as transport

FIELDS = {
    'discover':['source'], 'accounts':['source'], 'folders':['source'],
    'recent':['source','start','end','limit','subject','unread_only','folder_id'],
    'search':['source','start','end','limit','query','subject','unread_only','folder_id'],
    'read':['source','entry_id','folder_id','case_id'],
    'collect':['source','start','end','page_size','max_scan','offset','snapshot_id','run_id'],
    'freshness':['source','freshness_minutes','interval_seconds','max_wait_seconds'],
    'send':['source','approval_id','to','cc','bcc','subject','body','attachments'],
    'move':['source','approval_id','entry_id'],
    'move-reconcile':['source','approval_id'],
    'trash':['source','approval_id','entry_id'], 'trash-reconcile':['source','approval_id'],
    'calendars':['source'], 'calendar':['source','start','end','limit'],
    'calendar-read':['source','event_id'],
    'calendar-create':['source','approval_id'], 'calendar-update':['source','approval_id'],
    'calendar-delete':['source','approval_id'],
}
WRITES={'send','move','trash','calendar-create','calendar-update','calendar-delete'}


def call(request, runner=transport):
    envelope=runner(request)
    return envelope.get('result',envelope)


def window(payload, *, days=7):
    end=payload.get('end') or datetime.now(timezone.utc).isoformat()
    start=payload.get('start') or (datetime.fromisoformat(end)-timedelta(days=days)).isoformat()
    first,last=(datetime.fromisoformat(v) for v in (start,end))
    if first.utcoffset() is None or last.utcoffset() is None or not timedelta(0)<last-first<=timedelta(days=days):
        raise ValueError('Use an ordered timezone-aware window of at most '+str(days)+' days')
    return dict(start=first.isoformat(),end=last.isoformat())


def row(message,mailbox):
    locator=[message['folder']['accountId'],message['folder']['id'],message.get('internet_message_id','')]
    entry_id=('tb:'+base64.urlsafe_b64encode(json.dumps(locator,separators=(',',':')).encode()).decode().rstrip('=')) if locator[2] else str(message['id'])
    return dict(Store=mailbox,StoreId='thunderbird:'+mailbox.casefold(),EntryId=entry_id,TransientId=message['id'],
                InternetMessageId=message.get('internet_message_id',''),Received=message['date'],
                Unread=not message['read'],From=message['author'],SenderEmail=parseaddr(message['author'])[1],
                Subject=message['subject'],FolderId=message['folder']['id'])


def resolve_entry(entry_id,base,runner,folder_id=None):
    """Thunderbird integer IDs are session-local; persisted locators use RFC IDs."""
    if str(entry_id).isdigit():return dict(message_id=int(entry_id),**({'source_folder_id':folder_id} if folder_id else {}))
    try:
        if not isinstance(entry_id,str) or not entry_id.startswith('tb:') or len(entry_id)>12000:raise ValueError()
        token=entry_id[3:];account_id,parent,internet_id=json.loads(base64.urlsafe_b64decode(token+'='*(-len(token)%4)))
        if account_id!=base['account_id'] or not isinstance(parent,str) or not isinstance(internet_id,str) or not internet_id or (folder_id and folder_id!=parent):raise ValueError()
    except (ValueError,TypeError,UnicodeError):raise ValueError('Invalid or mismatched Thunderbird message locator') from None
    result=call(dict(base,action='query',folder_id=parent,internet_message_id=internet_id),runner)
    matches=result.get('messages',[])
    if result.get('status')!='ok' or not result.get('complete') or len(matches)!=1:raise ValueError('Exact message locator is missing or ambiguous; rediscover after filing')
    return dict(message_id=matches[0]['id'],source_folder_id=parent,internet_message_id=internet_id)


def mime(full):
    plain=[];html=[];attachments=[]
    def walk(part):
        if part.get('name'):
            attachments.append(dict(name=part['name'],part_name=part.get('partName'),size=part.get('size')))
            return
        body=part.get('body')
        if body and part.get('contentType')=='text/plain':plain.append(body)
        elif body and part.get('contentType')=='text/html':html.append(body)
        for child in part.get('parts',[]):walk(child)
    walk(full)
    if not plain and html:
        from html.parser import HTMLParser
        class Text(HTMLParser):
            def __init__(self):super().__init__();self.parts=[];self.hidden=0
            def handle_starttag(self,tag,attrs):
                if tag in {'script','style'}:self.hidden+=1
                if tag in {'p','br','div','tr'}:self.parts.append('\n')
            def handle_endtag(self,tag):
                if tag in {'script','style'}:self.hidden=max(0,self.hidden-1)
            def handle_data(self,data):
                if not self.hidden:self.parts.append(data)
        parser=Text();parser.feed('\n'.join(html));plain=[''.join(parser.parts)]
    return '\n'.join(plain),attachments


def write(h,operation,payload,base,binding,runner):
    from outlook_platforms.macos.operations import require_approval,allowed,journal
    from monitor.model import digest
    from outlook_platforms.api import check_send
    if not h:raise ValueError('Writes require the open shared harness database')
    approval_id=payload.get('approval_id');grant=require_approval(h,approval_id)
    scope=grant.get('scope',{})
    request=dict(base,approval_id=approval_id)
    if operation=='send':
        allowed(grant,'send')
        check_send(h,payload,base['mailbox'])
        message=grant.get('message',{})
        if grant.get('message_hash')!=digest(message):raise ValueError('Approval message integrity mismatch')
        if message.get('attachments'):raise ValueError('Thunderbird outgoing attachments are not implemented yet')
        for key in ('to','cc','bcc'):
            if not isinstance(message.get(key),list) or any(not isinstance(v,str) or not re.fullmatch(r'[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+',v) for v in message[key]):
                raise ValueError('Use explicit SMTP recipient addresses')
        if not message.get('to') or not isinstance(message.get('body'),str) or not message.get('subject') or any(c in message['subject'] for c in '\r\n'):
            raise ValueError('Invalid approved message')
        request.update(action='send-message',
                       **{key:message[key] for key in ('to','cc','bcc','subject','body')})
    elif operation in {'move','trash'}:
        needed={'mailbox','source_folder_id','entry_id','internet_message_id'}
        if operation=='move':needed.add('destination_folder_id')
        if scope.get('operation')!=('trash-message' if operation=='trash' else 'move-message') or not needed<=scope.keys():raise ValueError('Exact '+operation+' scope required')
        if scope['mailbox'].casefold()!=base['mailbox'].casefold():raise ValueError('Move mailbox mismatch')
        if payload.get('entry_id') is not None and str(payload['entry_id'])!=str(scope['entry_id']):raise ValueError('Move entry ID differs from approval')
        if not scope['internet_message_id']:raise ValueError('Exact message ID and RFC Message-ID required')
        destination=binding['trash_folder_id'] if operation=='trash' else scope['destination_folder_id']
        if operation=='trash' and scope.get('destination_folder_id')!=destination:raise ValueError('Trash approval must name the configured recoverable destination')
        request.update(action='trash-message' if operation=='trash' else 'move-message',entry_id=str(scope['entry_id']),
                       destination_folder_id=destination,**{k:scope[k] for k in ('source_folder_id','internet_message_id')})
    else:
        if scope.get('operation')!=operation or scope.get('calendar_id')!=binding['calendar_id']:
            raise ValueError('Exact calendar operation and calendar ID approval required')
        needed=['event_id']
        if operation!='calendar-delete':needed.append('icalendar')
        if operation!='calendar-create':needed.append('expected_icalendar')
        if any(not isinstance(scope.get(key),str) or not scope[key] for key in needed):raise ValueError('Exact event content/version required')
        request.update(action=operation,**{key:scope[key] for key in needed})
        if operation!='calendar-delete':validate_event(scope['icalendar'],scope['event_id'])
    identity=digest(request);key='thunderbird:'+operation+':'+approval_id
    with h.write():
        state=journal(h,key)
        if state:
            if state.get('fingerprint')!=identity:raise ValueError('Approval request changed')
            if state['stage']=='complete':return dict(state['result'],duplicate_prevented=True)
            if state['stage']!='not-attempted':raise ValueError('Prior Thunderbird write needs reconciliation; no command was reissued')
        journal(h,key,dict(stage='attempting',fingerprint=identity))
    # A transport exception deliberately leaves the durable attempting state.
    if operation in {'move','trash'}:
        locator=resolve_entry(request.pop('entry_id'),base,runner,request['source_folder_id'])
        if locator.get('internet_message_id',request['internet_message_id'])!=request['internet_message_id']:raise ValueError('RFC Message-ID differs from approval')
        request.update(locator)
    result=call(request,runner)
    not_attempted=result.get('code')=='client-write-in-progress' and result.get('mutation_may_have_occurred') is False
    with h.write():journal(h,key,dict(stage='complete' if result.get('status')=='ok' else 'not-attempted' if not_attempted else 'uncertain',fingerprint=identity,result=result))
    return result


def validate_event(text,event_id):
    from icalendar import Calendar
    parsed=Calendar.from_ical(text)
    events=parsed.walk('VEVENT')
    if len(events)!=1 or str(events[0].get('UID',''))!=event_id or events[0].get('RECURRENCE-ID'):
        raise ValueError('One exact master VEVENT is required')
    e=events[0]
    if e.get('ATTENDEE') or e.get('ORGANIZER'):raise ValueError('Calendar invitations are not implemented')
    start=e.decoded('DTSTART');end=e.decoded('DTEND')
    if type(start)!=type(end) or end<=start:raise ValueError('Invalid calendar event range')
    if isinstance(start,datetime) and (start.utcoffset() is None or end.utcoffset() is None):
        raise ValueError('Timed events require explicit timezones')


def calendar_event(event):
    """Preserve authoritative cancellation status for shared run ingestion."""
    from icalendar import Calendar
    value=dict(event)
    if event.get('icalendar'):
        items=Calendar.from_ical(event['icalendar']).walk('VEVENT')
        if len(items)!=1:raise ValueError('Expected one exported calendar event')
        value['status']=str(items[0].get('STATUS','')).lower()
    return value


def collect(h,base,payload,binding,runner):
    from monitor.threadcache import snapshot
    if not h:raise ValueError('Collection requires the shared harness database')
    bounds=window(payload);offset=payload.get('offset',0);size=payload.get('page_size',150);cap=payload.get('max_scan',750)
    if type(offset) is not int or not 0<=offset<=5000 or not 1<=size<=500 or not 1<=cap<=5000:raise ValueError('Invalid collection page/scan limit')
    sid=payload.get('snapshot_id')
    if sid:
        if not re.fullmatch('[a-f0-9]{32}',sid):raise ValueError('Invalid snapshot ID')
        saved=snapshot(h,'get',sid=sid)
        if (saved['Store'],saved.get('InboxId'),saved['Since'],saved['Until'])!=(base['mailbox'],binding['selected_inbox_id'],bounds['start'],bounds['end']):
            raise ValueError('Snapshot source/window mismatch')
    else:
        if offset:raise ValueError('Continuation requires snapshot_id')
        started=datetime.now(timezone.utc).isoformat()
        query_bounds=dict(bounds,end=(datetime.fromisoformat(bounds['end'])+timedelta(milliseconds=1)).isoformat())
        result=call(dict(base,action='query',folder_id=binding['selected_inbox_id'],**query_bounds),runner)
        if result.get('status')!='ok':return result
        messages=sorted([m for m in result['messages'] if datetime.fromisoformat(bounds['start'])<=datetime.fromisoformat(m['date'])<=datetime.fromisoformat(bounds['end'])],key=lambda m:(m['date'],m['id']),reverse=True)
        saved=dict(Store=base['mailbox'],StoreId='thunderbird:'+base['mailbox'].casefold(),
                   Since=bounds['start'],Until=bounds['end'],InboxId=binding['selected_inbox_id'],
                   SnapshotId=uuid.uuid4().hex,StartedAt=started,Scanned=result['scanned'],
                   Complete=result['complete'] and len(messages)<=cap,Errors=[] if result['complete'] and len(messages)<=cap else ['Scan limit reached'],
                   Items=[dict(row(m,base['mailbox']),ConversationId='',MessageClass='thunderbird:message') for m in messages[:cap]])
        snapshot(h,'put',saved)
    if offset>len(saved['Items']):raise ValueError('Offset exceeds snapshot')
    end=min(offset+size,len(saved['Items']));more=end<len(saved['Items'])
    return dict(**{k:saved[k] for k in ('Store','StoreId','Since','Until','SnapshotId','StartedAt','Scanned','Errors')},
                Offset=offset,NextOffset=end if more else None,Count=end-offset,Complete=not more and saved['Complete'],
                FirstId=saved['Items'][0]['EntryId'] if saved['Items'] else '',
                Newest=saved['Items'][0]['Received'] if saved['Items'] else None,Items=saved['Items'][offset:end])


def execute_json(operation,payload,store,h,source,*,profile,runner=transport):
    binding=profile['platform'].get('bindings',{}).get(source,{})
    if operation not in FIELDS:raise ValueError('Unsupported Thunderbird operation')
    if operation=='discover':
        result=call(dict(action='accounts'),runner)
        if result.get('status')=='ok':
            calendar=call(dict(action='calendar-list'),runner)
            result['calendars']=calendar.get('calendars',[])
            result['calendar_available']=calendar.get('status')=='ok'
        return dict(data=result,status=result.get('status','blocked'))
    if operation.startswith('calendar'):
        base=dict(calendar_id=binding['calendar_id'])
    else:base=dict(mailbox=store,account_id=binding['account_id'])
    if operation in WRITES:
        result=write(h,operation,payload,base,binding,runner)
        return dict(data=result,status=result.get('status','blocked'))
    if operation in {'move-reconcile','trash-reconcile'}:
        from outlook_platforms.macos.operations import require_approval,journal
        from monitor.model import digest
        grant=require_approval(h,payload.get('approval_id'));scope=grant.get('scope',{})
        action='trash-message' if operation=='trash-reconcile' else 'move-message'
        if scope.get('operation')!=action or scope.get('mailbox','').casefold()!=store.casefold():raise ValueError('Exact original move/trash approval required')
        if action=='trash-message' and scope.get('destination_folder_id')!=binding['trash_folder_id']:raise ValueError('Trash binding changed')
        expected=dict(base,action=action,approval_id=payload['approval_id'],entry_id=str(scope['entry_id']),
                      **{k:scope[k] for k in ('source_folder_id','destination_folder_id','internet_message_id')})
        key='thunderbird:'+('trash' if action=='trash-message' else 'move')+':'+payload['approval_id'];state=journal(h,key)
        if not state or state.get('fingerprint')!=digest(expected):raise ValueError('Original move journal/binding mismatch')
        if state['stage']=='complete':return dict(data=dict(state['result'],duplicate_prevented=True),status='ok')
        request=dict(expected,action='reconcile-trash' if action=='trash-message' else 'reconcile-move');request.pop('entry_id')
        result=call(request,runner)
        if result.get('status')=='ok':
            with h.write():journal(h,key,dict(state,stage='complete',result=result))
        return dict(data=result,status=result.get('status','blocked'))
    if operation=='collect':return dict(data=collect(h,base,payload,binding,runner))
    if operation=='freshness':
        return dict(data=dict(Store=store,Current=False,Status='unknown',
            LatestStoreId='thunderbird:'+store.casefold(),CheckedAt=datetime.now(timezone.utc).isoformat(),
            Reason='The standard Thunderbird API exposes local mail, not provider sync completion.'),status='partial')
    if operation in {'accounts','folders'}:
        # folders and read are checked inside the client before any mail content is read.
        result=call(dict(base,action='folders'),runner)
        data=([dict(DisplayName=store,SmtpAddress=store,AccountId=binding['account_id'])] if operation=='accounts'
              else [dict(Store=store,Folder=f['name'],FolderPath=f['path'],FolderId=f['id'],SpecialUse=f.get('specialUse',[])) for f in result.get('folders',[])])
    elif operation in {'recent','search'}:
        request=dict(base,action='query',**window(payload),folder_id=payload.get('folder_id',binding['selected_inbox_id']))
        request.update({key:payload[key] for key in ('subject','query','unread_only') if key in payload})
        result=call(request,runner)
        data=[row(m,store) for m in sorted(result.get('messages',[]),key=lambda m:(m['date'],m['id']),reverse=True)[:payload.get('limit',10)]]
    elif operation=='read':
        request=dict(base,action='read',**resolve_entry(payload.get('entry_id'),base,runner,payload.get('folder_id')))
        result=call(request,runner)
        if result.get('status')=='ok':
            message=result['message'];body,attachments=mime(result['full'])
            data=dict(row(message,store),FolderPath=message['folder']['path'],ReadPath='thunderbird-native-messaging',
                      MessageClass='thunderbird:message',To='; '.join(message.get('recipients',[])),Cc='; '.join(message.get('cc',[])),Body=body,
                      Attachments=len(attachments),AttachmentNames=[a['name'] for a in attachments],AttachmentInfo=attachments,
                      UnreadUnchanged=result['read_state_preserved'])
            if payload.get('case_id'):
                from monitor.threadcache import put_message
                data['CacheEvidenceId']=put_message(h,dict(case_id=payload['case_id'],message=data))['body_evidence_id']
        else:data=result
    elif operation=='calendars':
        result=call(dict(action='calendar-list'),runner)
        data=[c for c in result.get('calendars',[]) if c['id']==binding['calendar_id']]
        if result.get('status')=='ok' and len(data)!=1:raise ValueError('Configured calendar not available')
    elif operation=='calendar-read':
        result=call(dict(base,action='calendar-read',event_id=payload['event_id']),runner);data=dict(result)
        if result.get('event'):data['event']=calendar_event(result['event'])
    elif operation=='calendar':
        result=call(dict(base,action='calendar-query',**window(payload,days=366),limit=payload.get('limit',300)),runner)
        data=dict(source=source,events=[calendar_event(e) for e in result.get('events',[])],complete=result.get('complete',False),
                  **window(payload,days=366),server_verified=False,errors=[] if result.get('complete') else [result.get('code','Partial client query')])
    else:raise ValueError('Unsupported Thunderbird command')
    if result.get('status')!='ok':return dict(data=result,status=result.get('status','blocked'))
    coverage={k:v for k,v in result.items() if k in {'complete','scanned','read_state_preserved','server_verified','coverage'}}
    return dict(data=data,coverage=coverage,status='ok' if result.get('complete',True) else 'partial')
