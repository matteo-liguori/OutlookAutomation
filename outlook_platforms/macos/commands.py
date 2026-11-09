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
        return [mail_row(item, mailbox) for item in result['items']]
    if command == 'Get-OutlookMessage':
        if len(result['items']) != 1:
            return dict(status='blocked', code='exact-message-not-found-in-window')
        item = result['items'][0]
        return dict(EntryId=item['id'], StoreId='outlook-macos:' + mailbox.casefold(),
                    FolderPath=mailbox + '/Inbox', ReadPath='native-apple-events',
                    Subject=item['subject'], From=item.get('from', ''), SenderEmail=item.get('sender_email', ''),
                    To='; '.join(item.get('to', [])), Cc='; '.join(item.get('cc', [])),
                    Received=item['received_at'], Body=item.get('plain_text', ''),
                    Attachments=len(item.get('attachment_names', [])), AttachmentNames=item.get('attachment_names', []),
                    UnreadUnchanged=result.get('read_state_preserved') is True)
    if command == 'Read-OutlookMonitorCalendar':
        errors = ['Native recurrence expansion has not been verified.']
        if result['truncated']:
            errors.append('Calendar scan limit reached.')
        return dict(source='calendar:work', complete=False,
                    events=[dict(id=item['id'], title=item['subject'], start=item['start'], end=item['end'],
                                 location=item.get('location',''), all_day=item['all_day']) for item in result['items']],
                    start=result['window']['start'], end=result['window']['end_exclusive'],
                    scanned=result['matched_count'], errors=errors,
                    bounded_query_complete=not result['truncated'], filter=result['window'])
    raise ValueError('Unknown command')


def main(command, arguments):
    parser = argparse.ArgumentParser(prog='outlook.py ' + command)
    parser.add_argument('-Store', '--store', '--mailbox', dest='store', required=True)
    parser.add_argument('-Start', '--start')
    parser.add_argument('-End', '--end')
    parser.add_argument('-Limit', '--limit', type=int, default=25)
    parser.add_argument('-Action', '--action', default='Recent' if command == 'Read-OutlookMail' else 'Accounts')
    parser.add_argument('-Folder', '--folder', default='Inbox')
    parser.add_argument('-Output', '--output', choices=['Json'], default='Json')
    parser.add_argument('-EntryId', '--entry-id')
    parser.add_argument('-UnreadOnly', '--unread-only', action='store_true')
    parser.add_argument('-IncludePreview', '--include-preview', action='store_true')
    parser.add_argument('--subject')
    parser.add_argument('--selected-inbox-id')
    parser.add_argument('--calendar-id')
    parser.add_argument('--receipt', help='Optional native coverage receipt JSON path')
    args = parser.parse_args(arguments)
    if args.folder != 'Inbox':
        parser.error('This pilot currently implements the verified Inbox only')
    if command == 'Read-OutlookMail' and args.action != 'Recent':
        parser.error('This pilot currently implements Action Recent; other Windows actions remain unverified')
    if command == 'Inspect-Outlook' and args.action != 'Accounts':
        parser.error('This pilot implements account inspection without accessing profile file paths')
    if command == 'Get-OutlookMessage' and not args.entry_id:
        parser.error('Get-OutlookMessage requires -EntryId')
    operation = {'Inspect-Outlook':'check', 'Read-OutlookMail':'mail', 'Get-OutlookMessage':'mail',
                 'Read-OutlookMonitorCalendar':'calendar', 'List-OutlookCalendars':'calendars'}[command]
    try:
        request = request_for(operation, args.store, args.start, args.end, args.limit, args.subject,
                              command == 'Get-OutlookMessage' or args.include_preview,
                              args.selected_inbox_id, args.calendar_id, args.entry_id, args.unread_only)
    except ValueError as error:
        parser.error(str(error))
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
    print(json.dumps(output, indent=2))
    return 2 if isinstance(output, dict) and output.get('status') == 'blocked' else 0
