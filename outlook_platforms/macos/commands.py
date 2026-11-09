"""Mac equivalents of the Windows read helpers, with matching JSON field names."""
import argparse
import json
from pathlib import Path
import re

from .native import execute, request_for


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
    parser.add_argument('-Since', '--since')
    parser.add_argument('-Until', '--until')
    parser.add_argument('-PageSize', '--page-size', type=int, default=150)
    parser.add_argument('-MaxScan', '--max-scan', type=int, default=750)
    parser.add_argument('-Offset', '--offset', type=int, default=0)
    parser.add_argument('-SnapshotId', '--snapshot-id', default='')
    parser.add_argument('--ingest-run')
    parser.add_argument('--source', default='mail:work')
    parser.add_argument('--freshness', help='Existing source freshness proof JSON, if available')
    parser.add_argument('-FreshnessMinutes',type=int,default=1440)
    parser.add_argument('-ProbeIntervalSeconds',type=int,default=15)
    parser.add_argument('-MaxWaitSeconds',type=int,default=30)
    parser.add_argument('-CaseId','--case-id')
    parser.add_argument('-ForceRefresh','--force-refresh',action='store_true')
    parser.add_argument('-DownloadLargeAttachments','--download-large-attachments',action='store_true')
    parser.add_argument('--download-attachments',action='store_true')
    args = parser.parse_args(arguments)
    try:
        from monitor.profile import PROFILE
        bindings=PROFILE.get('outlook_native',{})
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
                freshness=json.loads(Path(args.freshness).read_text()) if args.freshness else None
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
        if cached.get('found'):print(json.dumps(cached['message'],indent=2));return 0
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
            matching=[item for item in inventory['folders'] if item['name'].casefold()==name or item['path'].casefold()==name or item['path'].split('/',1)[-1].casefold()==name]
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
        from automation_paths import data_root
        from .attachments import download_attachment
        assets=[]
        if args.download_attachments or args.download_large_attachments or args.case_id:
            for attachment in result['items'][0].get('attachment_info',[]):
                if attachment.get('size') and attachment['size']>10*1024*1024 and not args.download_large_attachments and args.case_id:
                    continue
                asset=download_attachment(args.store,args.selected_inbox_id,result['items'][0]['folder_id'],args.entry_id,attachment,private_root=data_root(),allow_large=args.download_large_attachments)
                if asset.get('status')!='ok':print(json.dumps(asset));return 2
                assets.append(asset)
            output['DownloadedAttachments']=assets
        if args.case_id:
            from monitor.core import Harness
            from monitor.threadcache import put_message,attachment_path,register_attachment
            import shutil
            h=Harness()
            try:
                cached_assets=[]
                for asset in assets:
                    target=attachment_path(h,args.case_id,asset['name'],args.entry_id+':'+str(asset['index']));target.parent.mkdir(parents=True,exist_ok=True)
                    shutil.copyfile(asset['path'],target)
                    registered=register_attachment(h,dict(case_id=args.case_id,path=str(target),name=asset['name'],source='mail:work'))
                    cached_assets.append(dict(asset,path=registered['path'],downloaded=True))
                output['CachedAttachments']=cached_assets
                cache=put_message(h,dict(case_id=args.case_id,message=output));output['CacheEvidenceId']=cache['body_evidence_id']
            finally:h.close()
    print(json.dumps(output, indent=2))
    return 2 if isinstance(output, dict) and output.get('status') == 'blocked' else 0
