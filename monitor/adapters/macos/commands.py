from monitor.configuration import resolve
"""Mac equivalents of the Windows read helpers, with matching JSON field names."""
import argparse
import json
from pathlib import Path
import re

from .native import execute, request_for

JSON_COMMANDS={
    'accounts':('Inspect-Outlook',{}),
    'folders':('Read-OutlookMail',{'-Action':'Folders'}),
    'recent':('Read-OutlookMail',{'-Action':'Recent'}),
    'search':('Read-OutlookMail',{'-Action':'Search'}),
    'read':('Get-OutlookMessage',{}),
    'calendar':('Read-OutlookMonitorCalendar',{}),
    'calendars':('List-OutlookCalendars',{}),
    'collect':('Collect-OutlookMonitor',{}),
    'freshness':('Test-OutlookMailboxFreshness',{}),
    'draft-save':('Save-OutlookStoreDraft',{}),
    'draft-update':('Update-OutlookStoreDraft',{}),
    'send':('Send-OutlookStoreMail',{}),
    'move':('Move-OutlookMessage',{}),
}


def execute_json(operation,payload,store,h,source,runner=None,profile=None):
    """Use the tested native helper workflow without duplicating its journals/cache."""
    import os,sys,subprocess
    from monitor.profile import PROFILE,native_bindings
    from monitor.model import DEFAULT_DB
    runner=runner or subprocess.run
    command,fixed=JSON_COMMANDS[operation]
    arguments=['-Store',store]
    for key,value in fixed.items():arguments.extend([key,value])
    bindings=native_bindings(profile or PROFILE,store,source)
    if operation in {'calendar','calendars'}:
        if not bindings.get('calendar_id'):raise ValueError('Verified calendar_id required in platform.bindings')
        arguments.extend(['--calendar-id',str(bindings['calendar_id'])])
    else:
        if not bindings.get('inbox_id'):raise ValueError('Verified selected_inbox_id required in platform.bindings')
        arguments.extend(['--selected-inbox-id',str(bindings['inbox_id'])])
    writing=operation in {'send','draft-save','draft-update','move'}
    if writing:
        arguments.extend(['--approval-id',payload['approval_id']])
        if operation in {'send','draft-save'}:
            if not bindings.get('draft_folder_id'):raise ValueError('Verified draft_folder_id required in platform.bindings')
            arguments.extend(['--drafts-folder-id',str(bindings['draft_folder_id'])])
        # Message contents/attachments are read from the exact SQLite approval,
        # rather than exposed on the subprocess command line.
        values={k:v for k,v in payload.items() if k in {'entry_id','display_only'}}
    else:
        arguments.extend(['--source',source,'-Output','Json','--include-coverage'])
        values=payload
    flags={'start':'-Start','end':'-End','limit':'-Limit','subject':'--subject','entry_id':'-EntryId',
           'unread_only':'-UnreadOnly','include_preview':'-IncludePreview','folder':'-Folder','folder_id':'--folder-id',
           'query':'-Query','max_scan':'-MaxScan','page_size':'-PageSize','offset':'-Offset','snapshot_id':'-SnapshotId',
           'ingest_run':'--ingest-run','freshness':'--freshness-json','case_id':'-CaseId','action_ids':'--action-ids-json','force_refresh':'-ForceRefresh',
           'download_large_attachments':'-DownloadLargeAttachments','download_attachments':'--download-attachments',
           'freshness_minutes':'-FreshnessMinutes','interval_seconds':'-ProbeIntervalSeconds','max_wait_seconds':'-MaxWaitSeconds',
           'display_only':'-DisplayOnly'}
    if operation=='collect':flags.update(start='-Since',end='-Until')
    for key,value in values.items():
        if key not in flags:raise ValueError('Unsupported native helper field: '+key)
        if isinstance(value,bool):
            if value:arguments.append(flags[key])
        else:arguments.extend([flags[key],json.dumps(value) if key in {'freshness','action_ids'} else str(value)])
    env=dict(os.environ,OUTLOOK_AUTOMATION_DATABASE=str(h.path.resolve() if h else DEFAULT_DB))
    try:
        result=runner([sys.executable,'-m','monitor.adapters.legacy_cli','--platform','macos',command,*arguments],
                      capture_output=True,text=True,env=env,timeout=180)
    except subprocess.TimeoutExpired:
        raise ValueError('Outlook helper timed out; reconcile recorded state before retrying writes') from None
    try:data=json.loads(result.stdout)
    except (ValueError,TypeError):raise ValueError('Native helper failed; reconcile writes before retry: '+result.stderr.strip()[-1000:]) from None
    if operation=='calendar' and 'events' in data:data['source']=source
    if isinstance(data,dict) and set(data)=={'data','coverage'}:
        return dict(data,status='blocked' if result.returncode else 'ok')
    return dict(data=data,status='blocked' if result.returncode else data.get('status','ok') if isinstance(data,dict) else 'ok')


def mail_row(item, mailbox):
    return dict(Store=mailbox, Received=item['received_at'], Unread=not item['is_read'],
                From=item.get('from', ''), SenderEmail=item.get('sender_email', ''), Subject=item['subject'],
                Preview=re.sub(r'\s+', ' ', item.get('plain_text', '')).strip()[:240], EntryId=item['id'])


def normalize(command, result, mailbox):
    if result.get('status') != 'ok':
        return result
    if command == 'Inspect-Outlook':
        return [dict(DisplayName=mailbox, SmtpAddress=mailbox, AccountType=result['account_type'])]
    if command == 'List-OutlookCalendars':
        return result['calendars']
    if command == 'Read-OutlookMail':
        if 'folders' in result:
            return [dict(Store=mailbox,Folder=item['name'],FolderPath=item['path'],Items=item['items'],FolderId=item['id']) for item in result['folders']]
        if result.get('command')=='check':
            return [dict(DisplayName=mailbox,SmtpAddress=mailbox,AccountType=result['account_type'])]
        return [mail_row(item, mailbox) for item in result['items']]
    if command == 'Get-OutlookMessage':
        if len(result['items']) != 1:
            return dict(status='blocked', code='exact-message-not-found-in-window')
        item = result['items'][0]
        return dict(EntryId=item['id'], StoreId='outlook-macos:' + mailbox.casefold(),
                    FolderPath=item.get('folder_path',mailbox + '/Inbox'), ReadPath='native-apple-events',
                    MessageClass=item.get('message_class'),InternetMessageId=item.get('internet_message_id',''),
                    Subject=item['subject'], From=item.get('from', ''), SenderEmail=item.get('sender_email', ''),
                    To='; '.join(item.get('to', [])), Cc='; '.join(item.get('cc', [])),
                    Received=item['received_at'], Body=item.get('plain_text', ''),
                    Attachments=len(item.get('attachment_names', [])), AttachmentNames=item.get('attachment_names', []),AttachmentInfo=item.get('attachment_info',[]),
                    UnreadUnchanged=result.get('read_state_preserved') is True)
    if command == 'Read-OutlookMonitorCalendar':
        errors = list(result.get('errors', []))
        if result.get('recurrence_coverage') != 'expanded-native-icalendar':
            errors.append('Native recurrence expansion has not been verified.')
        if result['truncated']:
            errors.append('Calendar scan limit reached.')
        return dict(source='calendar:work', complete=bool(result.get('bounded_complete')) and not errors,
                    events=[dict(id=item['id'], title=item['subject'], start=item['start'], end=item['end'],
                                 location=item.get('location',''), all_day=item['all_day']) for item in result['items']],
                    start=result['window']['start'], end=result['window']['end_exclusive'],
                    scanned=result['matched_count'], errors=errors,
                    bounded_query_complete=bool(result.get('bounded_complete')) if result.get('recurrence_coverage')=='expanded-native-icalendar' else not result['truncated'], filter=result['window'])
    raise ValueError('Unknown command')


def main(command, arguments):
    if command in {'Save-OutlookStoreDraft','Update-OutlookStoreDraft','Send-OutlookStoreMail','Move-OutlookMessage'}:
        from .operations import cli
        return cli(command,arguments)
    parser = argparse.ArgumentParser(prog='outlook.py ' + command)
    parser.add_argument('-Store', '--store', '--mailbox', dest='store', required=True)
    parser.add_argument('-Start', '--start')
    parser.add_argument('-End', '--end')
    parser.add_argument('-Limit', '--limit', type=int, default=300 if command=='Read-OutlookMonitorCalendar' else 10)
    parser.add_argument('-Action', '--action', default='Recent' if command == 'Read-OutlookMail' else 'Accounts')
    parser.add_argument('-Folder', '--folder', default='Inbox')
    parser.add_argument('-Output', '--output', choices=['Json'], default='Json')
    parser.add_argument('-EntryId', '--entry-id')
    parser.add_argument('-UnreadOnly', '--unread-only', action='store_true')
    parser.add_argument('-IncludePreview', '--include-preview', action='store_true')
    parser.add_argument('--subject')
    parser.add_argument('--selected-inbox-id')
    parser.add_argument('--calendar-id')
    parser.add_argument('--folder-id')
    parser.add_argument('-Query', '--query')
    parser.add_argument('--receipt', help='Optional native coverage receipt JSON path')
    parser.add_argument('--include-coverage',action='store_true',help=argparse.SUPPRESS)
    parser.add_argument('-Since', '--since')
    parser.add_argument('-Until', '--until')
    parser.add_argument('-PageSize', '--page-size', type=int, default=150)
    parser.add_argument('-MaxScan', '--max-scan', type=int, default=750)
    parser.add_argument('-Offset', '--offset', type=int, default=0)
    parser.add_argument('-SnapshotId', '--snapshot-id', default='')
    parser.add_argument('--ingest-run')
    parser.add_argument('--source', default='mail:work')
    parser.add_argument('--freshness', help='Existing source freshness proof JSON, if available')
    parser.add_argument('--freshness-json', help='Inline freshness proof from the shared harness')
    parser.add_argument('-FreshnessMinutes',type=int,default=1440)
    parser.add_argument('-ProbeIntervalSeconds',type=int,default=15)
    parser.add_argument('-MaxWaitSeconds',type=int,default=30)
    parser.add_argument('-CaseId','--case-id')
    parser.add_argument('--action-ids-json')
    parser.add_argument('-ForceRefresh','--force-refresh',action='store_true')
    parser.add_argument('-DownloadLargeAttachments','--download-large-attachments',action='store_true')
    parser.add_argument('--download-attachments',action='store_true')
    args = parser.parse_args(arguments)
    args.action_ids = json.loads(args.action_ids_json) if args.action_ids_json else None
    if args.case_id:
        from monitor.core import Harness
        from monitor.threadcache import retained_owners
        h=Harness()
        try:retained_owners(h,args.case_id,args.action_ids)
        finally:h.close()
    elif args.download_attachments or args.download_large_attachments:
        parser.error('Attachment saves require CaseId and explicit action IDs')
    try:
        from monitor.profile import PROFILE,native_bindings
        bindings=native_bindings(PROFILE,args.store,args.source)
    except ValueError:
        bindings={}
    if command in {'Read-OutlookMonitorCalendar','List-OutlookCalendars'}:
        if not args.selected_inbox_id:args.calendar_id=args.calendar_id or bindings.get('calendar_id')
    else:
        args.selected_inbox_id=args.selected_inbox_id or bindings.get('inbox_id')
    if command == 'Test-OutlookMailboxFreshness':
        from .freshness import probe
        if args.folder!='Inbox':parser.error('Native freshness currently probes the verified Inbox')
        try:result=probe(args.store,args.selected_inbox_id,freshness_minutes=args.FreshnessMinutes,interval=args.ProbeIntervalSeconds,max_wait=args.MaxWaitSeconds)
        except ValueError as error:parser.error(str(error))
        print(json.dumps(result,indent=2));return 0 if result['Current'] else 2
    if command == 'Collect-OutlookMonitor':
        from monitor.core import Harness
        from monitor.sources import MAIL_STORES
        from .collector import collect_page, ingest_snapshot
        if MAIL_STORES.get(args.source) != args.store:
            parser.error('Store does not match the configured harness source')
        h=Harness()
        try:
            result=collect_page(h,args.store,args.since,args.until,args.selected_inbox_id,
                                page_size=args.page_size,max_scan=args.max_scan,offset=args.offset,snapshot_id=args.snapshot_id)
            if args.ingest_run:
                freshness=json.loads(args.freshness_json) if args.freshness_json else json.loads(Path(args.freshness).read_text()) if args.freshness else None
                result=ingest_snapshot(h,args.ingest_run,args.source,result,freshness=freshness,page_size=args.page_size)
        except ValueError as error:
            parser.error(str(error))
        finally:
            h.close()
        print(json.dumps(result,indent=2))
        return 0
    if command == 'Read-OutlookMail' and args.action not in {'Recent','Search','Accounts','Folders'}:
        parser.error('Unknown mail action')
    if command == 'Read-OutlookMail' and args.action=='Search' and not args.query:
        parser.error('Search requires -Query')
    if command == 'Inspect-Outlook' and args.action != 'Accounts':
        parser.error('This pilot implements account inspection without accessing profile file paths')
    if command == 'Get-OutlookMessage' and not args.entry_id:
        parser.error('Get-OutlookMessage requires -EntryId')
    if command=='Get-OutlookMessage' and args.case_id:
        if args.store.casefold() not in {value.casefold() for value in PROFILE['mail'].values()}:parser.error('Store differs from the configured profile')
    if command=='Get-OutlookMessage' and args.case_id and not args.force_refresh and not args.download_attachments and not args.download_large_attachments:
        from monitor.core import Harness
        from monitor.threadcache import find_message
        if args.store.casefold() not in {value.casefold() for value in PROFILE['mail'].values()}:parser.error('Store differs from the configured profile')
        h=Harness()
        try:cached=find_message(h,dict(case_id=args.case_id,entry_id=args.entry_id,store_id='outlook-macos:'+args.store.casefold()))
        finally:h.close()
        if cached.get('found'):
            from monitor.threadcache import put_message
            h=Harness()
            try:put_message(h,dict(case_id=args.case_id,action_ids=args.action_ids,message=cached['message']))
            finally:h.close()
            output=dict(data=cached['message'],coverage=dict(cache_hit=True,fresh_native_read=False)) if args.include_coverage else cached['message']
            print(json.dumps(output,indent=2));return 0
    operation = {'Inspect-Outlook':'check', 'Read-OutlookMail':'mail', 'Get-OutlookMessage':'mail',
                 'Read-OutlookMonitorCalendar':'calendar', 'List-OutlookCalendars':'calendars'}[command]
    if command=='Read-OutlookMail' and args.action in {'Accounts','Folders'}:
        operation={'Accounts':'check','Folders':'folders'}[args.action]
    if command=='Get-OutlookMessage' and not args.start and not args.end:operation='message'
    try:
        request = request_for(operation, args.store, args.start, args.end, args.limit, args.subject,
                              command == 'Get-OutlookMessage' or args.include_preview,
                              args.selected_inbox_id, args.calendar_id, args.entry_id, args.unread_only)
    except ValueError as error:
        parser.error(str(error))
    if operation in {'mail','message'}:
        if args.folder_id:
            if not args.folder_id.isdigit():parser.error('Folder ID must be numeric')
            request['target_folder_id']=args.folder_id
        elif args.folder!='Inbox':
            inventory=execute(request_for('folders',args.store,selected_inbox_id=args.selected_inbox_id))
            if inventory.get('status')!='ok':print(json.dumps(inventory));return 2
            name=args.folder.replace('\\','/').casefold()
            aliases={'inbox':{'inbox'},'sent':{'sent','sent mail','sent items'},'drafts':{'drafts'},'trash':{'trash','bin','deleted items'}}
            names=aliases.get(name,{name})
            matching=[item for item in inventory['folders'] if item['name'].casefold() in names or item['path'].casefold()==name or item['path'].split('/',1)[-1].casefold()==name]
            if name=='drafts' and bindings.get('draft_folder_id'):
                matching=[item for item in matching if item['id']==str(bindings['draft_folder_id'])]
            if len(matching)!=1:parser.error('Folder is missing or ambiguous; use --folder-id from Folders')
            request['target_folder_id']=matching[0]['id']
        if args.action=='Search':
            if not 1<=args.max_scan<=5000:parser.error('MaxScan must be 1..5000')
            request.update(search_query=args.query,max_scan=args.max_scan)
    result = execute(request)
    if args.receipt:
        target = Path(args.receipt).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        # Bodies remain in the command result; receipts carry bounds, IDs and state.
        receipt = {key:value for key,value in result.items() if key != 'items'}
        if 'items' in result:
            receipt['items'] = [{key:value for key,value in item.items() if key not in {'plain_text','to','cc'}} for item in result['items']]
        target.write_text(json.dumps(receipt, indent=2) + '\n')
    output = normalize(command, result, args.store)
    if command=='Get-OutlookMessage' and result.get('status')=='ok' and len(result.get('items',[]))==1:
        from monitor.paths import data_root
        from .attachments import download_attachment
        assets=[]
        if args.download_attachments or args.download_large_attachments or args.case_id:
            for attachment in result['items'][0].get('attachment_info',[]):
                if attachment.get('size') and attachment['size']>resolve().get('maintenance.small_attachment_bytes') and not args.download_large_attachments and args.case_id:
                    continue
                from monitor.core import Harness
                from monitor.threadcache import attachment_path
                h=Harness()
                try:target=attachment_path(h,args.case_id,attachment['name'],args.entry_id+':'+str(attachment['index']),args.action_ids)
                finally:h.close()
                asset=download_attachment(args.store,args.selected_inbox_id,result['items'][0]['folder_id'],args.entry_id,attachment,private_root=data_root(),target=target,allow_large=args.download_large_attachments)
                if asset.get('status')!='ok':print(json.dumps(asset));return 2
                assets.append(asset)
            output['DownloadedAttachments']=assets
        if args.case_id:
            from monitor.core import Harness
            from monitor.threadcache import put_message,attachment_path,register_attachment
            h=Harness()
            try:
                cached_assets=[]
                for asset in assets:
                    target=Path(asset['path'])
                    mail_source=next(key for key,address in PROFILE['mail'].items() if address.casefold()==args.store.casefold())
                    registered=register_attachment(h,dict(case_id=args.case_id,action_ids=args.action_ids,path=str(target),name=asset['name'],source=mail_source))
                    cached_assets.append(dict(asset,path=registered['path'],downloaded=True))
                output['CachedAttachments']=cached_assets
                cache=put_message(h,dict(case_id=args.case_id,action_ids=args.action_ids,message=output));output['CacheEvidenceId']=cache['body_evidence_id']
            finally:h.close()
    rendered=dict(data=output,coverage={k:v for k,v in result.items() if k not in {'items','folders','calendars'}}) if args.include_coverage else output
    print(json.dumps(rendered, indent=2))
    return 2 if isinstance(output, dict) and output.get('status') == 'blocked' else 0
