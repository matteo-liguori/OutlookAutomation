"""Run bounded, read-only commands against Outlook's native Apple-event interface."""
import argparse
from datetime import datetime, timedelta
import json
from pathlib import Path
import platform
import re
import subprocess

SCRIPT = Path(__file__).with_name('native_outlook.js')


def request_for(command, mailbox, start=None, end=None, limit=25, subject=None, include_body=False, selected_inbox_id=None, calendar_id=None, entry_id=None, unread_only=False):
    if command not in {'check', 'mail', 'calendar', 'calendars', 'discover'}:
        raise ValueError('Supported commands: check, discover, mail, calendar, calendars')
    if not isinstance(mailbox, str) or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', mailbox):
        raise ValueError('Supply the exact confirmed mailbox email address')
    request = dict(command=command, mailbox=mailbox, limit=limit, include_body=include_body)
    if entry_id is not None:
        if command != 'mail' or not re.fullmatch(r'[0-9]+', str(entry_id)):
            raise ValueError('Use a numeric native message EntryId with mail')
        request['entry_id'] = str(entry_id)
    if unread_only:
        if command != 'mail':
            raise ValueError('UnreadOnly is available for mail')
        request['unread_only'] = True
    if selected_inbox_id is not None:
        if command == 'calendar' or not re.fullmatch(r'[0-9]+', str(selected_inbox_id)):
            raise ValueError('Selected inbox ID must be numeric and is available only for check/mail')
        request.update(binding='selected-inbox', folder_id=str(selected_inbox_id))
    if calendar_id is not None:
        if selected_inbox_id is not None or command == 'mail' or not re.fullmatch(r'[0-9]+', str(calendar_id)):
            raise ValueError('Use one numeric calendar ID with check/calendars/calendar')
        request.update(binding='named-calendar', calendar_id=str(calendar_id))
    if command in {'check', 'calendars', 'discover'}:
        return request
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise ValueError('Limit must be an integer from 1 to 100')
    try:
        first, last = (datetime.fromisoformat(value) for value in (start, end))
    except (TypeError, ValueError):
        raise ValueError('Supply ISO start/end timestamps with explicit timezone offsets') from None
    if first.utcoffset() is None or last.utcoffset() is None:
        raise ValueError('Start/end timestamps must include timezone offsets')
    if not timedelta(0) < last - first <= timedelta(days=7):
        raise ValueError('The end must follow the start by no more than seven days')
    request.update(start=first.isoformat(), end=last.isoformat())
    if subject is not None:
        if command != 'mail' or not isinstance(subject, str) or not subject.strip():
            raise ValueError('An exact non-empty subject is supported only for mail')
        request['subject'] = subject
    return request


def execute(request, runner=subprocess.run, system=None):
    if (system or platform.system()) != 'Darwin':
        return dict(status='blocked', code='requires-macos', writes_performed=False)
    try:
        process = runner(['/usr/bin/osascript', '-l', 'JavaScript', str(SCRIPT), json.dumps(request)],
                         capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return dict(status='blocked', code='native-command-timeout', writes_performed=False)
    if process.returncode:
        return dict(status='blocked', code='osascript-failed', exit_code=process.returncode, writes_performed=False)
    try:
        result = json.loads(process.stdout)
    except (ValueError, TypeError):
        return dict(status='blocked', code='invalid-native-response', writes_performed=False)
    if not isinstance(result, dict) or result.get('status') not in {'ok', 'blocked'}:
        return dict(status='blocked', code='invalid-native-response', writes_performed=False)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['check', 'discover', 'mail', 'calendar', 'calendars'])
    parser.add_argument('--mailbox', required=True)
    parser.add_argument('--start')
    parser.add_argument('--end')
    parser.add_argument('--limit', type=int, default=25)
    parser.add_argument('--subject', help='Exact subject within the bounded inbox window')
    parser.add_argument('--include-body', action='store_true')
    parser.add_argument('--selected-inbox-id', help='Native ID of the Gmail inbox first verified in the Outlook UI; binds to the matching mailbox window')
    parser.add_argument('--calendar-id', help='Native ID of a calendar whose name exactly matches the confirmed mailbox')
    parser.add_argument('--entry-id')
    parser.add_argument('--unread-only', action='store_true')
    args = parser.parse_args(argv)
    try:
        request = request_for(args.command, args.mailbox, args.start, args.end, args.limit, args.subject, args.include_body, args.selected_inbox_id, args.calendar_id, args.entry_id, args.unread_only)
    except ValueError as error:
        parser.error(str(error))
    result = execute(request)
    print(json.dumps(result, indent=2))
    return 0 if result['status'] == 'ok' else 2


if __name__ == '__main__':
    raise SystemExit(main())
