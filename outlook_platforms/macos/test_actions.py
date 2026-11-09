"""Execute one ledger-backed, explicitly authorized native self-test action."""
import json
import hashlib
from pathlib import Path
import re
import subprocess
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .native import SCRIPT

ACTIONS = SCRIPT.with_name('test_actions.js')


def audit_result(result):
    """Keep collected mail bodies out of generic harness evidence and ledgers."""
    result = json.loads(json.dumps(result))
    for item in result.get('items', []):
        if 'body' in item:
            body = item.pop('body')
            item.update(body_length=len(body), body_sha256=hashlib.sha256(body.encode()).hexdigest())
    return result


def execute_action(request, runner=subprocess.run):
    if not re.fullmatch('[a-f0-9]{12}', request.get('token', '')):
        raise ValueError('A unique test ledger token is required')
    if request.get('authorized') is not True:
        raise ValueError('Record explicit test authorization before running mutations')
    if request.get('action')=='calendar-create' and request.get('daily_count'):
        from icalendar import Calendar, Event, Timezone
        count=request['daily_count']
        if type(count) is not int or not 2<=count<=5:raise ValueError('Finite test recurrence count must be 2..5')
        zone=ZoneInfo(request.get('timezone_name','Europe/London'))
        first,last=(datetime.fromisoformat(request[field]).astimezone(zone) for field in ('start','end'))
        cal=Calendar();cal.add('VERSION','2.0');cal.add('PRODID','-//OutlookAutomation synthetic tests//EN')
        cal.add_component(Timezone.from_tzinfo(zone,first_date=first.date()-timedelta(days=366),last_date=last.date()+timedelta(days=366)))
        event=Event();event.add('UID','outlook-test-'+request['token']+'@example.invalid');event.add('SUMMARY','[OutlookAutomation test '+request['token']+'] calendar')
        event.add('DTSTART',first);event.add('DTEND',last);event.add('CLASS','PRIVATE');event.add('RRULE',{'FREQ':'DAILY','COUNT':count})
        cal.add_component(event)
        path=SCRIPT.parents[2]/'.local/pilot-evidence'/('calendar-fixture-'+request['token']+'.ics')
        path.parent.mkdir(parents=True,exist_ok=True)
        if path.exists():raise ValueError('Test ICS already exists; inspect the prior attempt before retrying')
        path.write_bytes(cal.to_ical())
        request=dict(request,action='calendar-import',ics_path=str(path))
    if request.get('action')=='calendar-import':
        from icalendar import Calendar
        path=Path(request['ics_path']).resolve();allowed=(SCRIPT.parents[2]/'.local').resolve()
        if not path.is_relative_to(allowed) or Path(request['ics_path']).is_symlink():raise ValueError('Test import must stay in the private test folder')
        cal=Calendar.from_ical(path.read_bytes());events=cal.walk('VEVENT')
        if not events or len(events)>10 or cal.walk('VALARM'):raise ValueError('Invalid synthetic calendar import')
        for event in events:
            if str(event.get('SUMMARY'))!='[OutlookAutomation test '+request['token']+'] calendar' or request['token'] not in str(event.get('UID')) or event.get('ATTENDEE') or event.get('ORGANIZER'):
                raise ValueError('Calendar import must contain only the marked private test fixture')
            if event.get('RRULE') and not 1<=int(event['RRULE'].get('COUNT',[0])[0])<=5:raise ValueError('Test recurrence must be finite')
    source = SCRIPT.read_text() + '\n' + ACTIONS.read_text()
    try:
        p = runner(['/usr/bin/osascript', '-l', 'JavaScript', '-e', source, json.dumps(request)],
                   capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return dict(status='blocked', code='test-action-timeout', mutation_may_have_occurred=True)
    if p.returncode:
        return dict(status='blocked', code='test-action-transport-failed', mutation_may_have_occurred=True)
    try:
        return json.loads(p.stdout)
    except ValueError:
        return dict(status='blocked', code='invalid-test-action-response', mutation_may_have_occurred=True)
