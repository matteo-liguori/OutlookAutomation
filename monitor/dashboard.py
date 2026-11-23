"""Local calendar coverage and the reports shown in the dashboard."""
from contextlib import closing
from datetime import date, datetime, time, timedelta
import json
import re

from .configuration import zone
from .model import CAL, digest, dumps, now, stamp


SCHEMA = """
CREATE TABLE IF NOT EXISTS dashboard_calendar(
    day TEXT, source TEXT, checked_at TEXT NOT NULL, events TEXT NOT NULL,
    PRIMARY KEY(day,source)
);
CREATE TABLE IF NOT EXISTS dashboard_reports(
    run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, day TEXT NOT NULL,
    status TEXT NOT NULL, markdown TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS dashboard_reports_day ON dashboard_reports(day,started_at);
CREATE TABLE IF NOT EXISTS dashboard_local_events(
    id TEXT PRIMARY KEY, start_day TEXT NOT NULL, end_day TEXT NOT NULL, event TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS dashboard_local_events_days ON dashboard_local_events(start_day,end_day);
CREATE TABLE IF NOT EXISTS dashboard_event_categories(
    identity TEXT PRIMARY KEY, end_day TEXT NOT NULL, category TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dashboard_event_titles(
    identity TEXT PRIMARY KEY, end_day TEXT NOT NULL, title TEXT NOT NULL
);
"""

RETENTION_DAYS = 365
EVENT_FIELDS = ('id', 'title', 'start', 'end', 'location', 'all_day', 'calendar', 'url', 'event_type')
EVENT_CATEGORIES = {'work', 'personal', 'social', 'travel', 'health', 'occasion', 'home'}


def cutoff(today):
    return (today - timedelta(days=RETENTION_DAYS - 1)).isoformat()


def record_calendar(db, source, snapshot, checked_at):
    excluded = {event['id'] for event in snapshot.get('explicit_cancellations', [])}
    events = [{key: event[key] for key in EVENT_FIELDS if key in event}
              for event in snapshot.get('events', []) if event['id'] not in excluded]
    tz = zone()
    def local(value):
        value = datetime.fromisoformat(value)
        return value if value.tzinfo else value.replace(tzinfo=tz)

    try:
        intervals = [(local(event['start']), local(event['end']), event) for event in events]
    except (KeyError, TypeError, ValueError):
        return  # Preserve the last verified day if a legacy receipt lacks a usable event interval.
    for day in snapshot['days']:
        start = datetime.combine(date.fromisoformat(day), time.min, tz)
        end = start + timedelta(days=1)
        day_events = [event for begins, ends, event in intervals if begins < end and ends > start]
        db.execute(
            'INSERT INTO dashboard_calendar VALUES (?,?,?,?) '
            'ON CONFLICT(day,source) DO UPDATE SET checked_at=excluded.checked_at,events=excluded.events '
            'WHERE excluded.checked_at >= dashboard_calendar.checked_at',
            (day, source, checked_at, dumps(day_events)),
        )


def prune(db, today):
    for table in ('dashboard_calendar', 'dashboard_reports'):
        db.execute(f'DELETE FROM {table} WHERE day < ?', (cutoff(today),))
    db.execute('DELETE FROM dashboard_local_events WHERE end_day < ?', (cutoff(today),))
    db.execute('DELETE FROM dashboard_event_categories WHERE end_day < ?', (cutoff(today),))
    db.execute('DELETE FROM dashboard_event_titles WHERE end_day < ?', (cutoff(today),))


def event_identity(event):
    return digest([event['title'].strip().casefold(), stamp(event['start']), stamp(event['end']),
                   bool(event.get('all_day'))])


def event_occurrence_identity(source, event, identity):
    uid = event['id']
    calendar = event.get('calendar') if source == 'calendar:icloud' else None
    if calendar and uid.startswith(calendar + ':'):
        # Older archive imports qualified CalDAV UIDs with calendar and recurrence ID.
        uid = re.sub(r':(?:\d{4}-\d{2}-\d{2}(?:[T ].*)?)?$', '', uid[len(calendar) + 1:])
    return digest([source, calendar, uid, identity])


def classify_calendar(h, run_id, payload):
    from .card_input import text

    if not isinstance(payload, dict) or not isinstance(payload.get('assignments'), list):
        raise ValueError('Provide an assignments list for reviewed calendar events.')
    with h.write():
        h.run(run_id)
        events = {}
        for source, details in h.db.execute('SELECT source, details FROM sources WHERE run_id=?', (run_id,)):
            if source not in CAL:
                continue
            snapshot = json.loads(details)
            if snapshot.get('status') == 'current' and snapshot.get('complete'):
                for event in snapshot.get('events', []):
                    events.setdefault((source, event['id']), []).append(event)
        updates, titles = {}, {}
        required = {'source', 'id', 'start', 'category'}
        for assignment in payload['assignments']:
            if (not isinstance(assignment, dict) or not required <= assignment.keys()
                    or assignment.keys() - (required | {'display_title'})
                    or any(not isinstance(assignment[key], str) for key in required)):
                raise ValueError('Each assignment needs a source, event ID, start and category.')
            category = assignment['category']
            matches = [event for event in events.get((assignment['source'], assignment['id']), [])
                       if stamp(event['start']) == stamp(assignment['start'])]
            if category not in EVENT_CATEGORIES or len(matches) != 1:
                raise ValueError('Use a supported category and a unique verified event from this run.')
            event = matches[0]
            identity = event_identity(event)
            if identity in updates and updates[identity][1] != category:
                raise ValueError('Matching copies of one event must have the same category.')
            end_day = datetime.fromisoformat(stamp(event['end'])).astimezone(zone()).date().isoformat()
            updates[identity] = (end_day, category)
            if 'display_title' in assignment:
                title = assignment['display_title']
                if title is not None:
                    title = text(title, 'Display title', 180)
                    if len(title.splitlines()) != 1:
                        raise ValueError('Use a single line for the display title.')
                key = event_occurrence_identity(assignment['source'], event, identity)
                if key in titles and titles[key][1] != title:
                    raise ValueError('Use one display title per reviewed event.')
                titles[key] = (end_day, title)
        h.db.executemany('INSERT OR REPLACE INTO dashboard_event_categories VALUES (?,?,?)',
                         [(identity, *values) for identity, values in updates.items()])
        h.db.executemany('INSERT OR REPLACE INTO dashboard_event_titles VALUES (?,?,?)',
                         [(key, end_day, title) for key, (end_day, title) in titles.items() if title is not None])
        h.db.executemany('DELETE FROM dashboard_event_titles WHERE identity=?',
                         [(key,) for key, (_, title) in titles.items() if title is None])
    return dict(classified=len(updates), allowed_categories=sorted(EVENT_CATEGORIES))


def archive_calendar(h, payload):
    """Import verified provider coverage without changing mail pass checkpoints."""
    if not isinstance(payload, dict) or payload.get('source') not in CAL or payload.get('complete') is not True:
        raise ValueError('Calendar history needs a complete configured source.')
    start, end = (date.fromisoformat(payload[key]) for key in ('start', 'end'))
    today = datetime.now(zone()).date()
    if start.isoformat() < cutoff(today) or end > today + timedelta(days=367) or end <= start:
        raise ValueError('Calendar history must stay inside the supported past and future range.')
    captured = datetime.fromisoformat(payload['checked_at'])
    if captured.tzinfo is None or captured > datetime.now(zone()) + timedelta(minutes=1):
        raise ValueError('Calendar history needs a valid capture time.')
    events = payload.get('events')
    if not isinstance(events, list):
        raise ValueError('Calendar history must contain an events list.')
    for event in events:
        if not all(isinstance(event.get(key), str) and event[key] for key in ('id', 'start', 'end')):
            raise ValueError('Calendar history contains an invalid event.')
        if not event.get('title'):
            event['title'] = '(Untitled event)'
        begins, ends = (datetime.fromisoformat(event[key]) for key in ('start', 'end'))
        if payload['source'] == 'calendar:icloud':
            # Floating CalDAV times are local to the viewer's configured calendar.
            begins = begins if begins.tzinfo else begins.replace(tzinfo=zone())
            ends = ends if ends.tzinfo else ends.replace(tzinfo=zone())
            event.update(start=begins.isoformat(), end=ends.isoformat())
        if begins.tzinfo is None or ends.tzinfo is None or begins >= ends:
            raise ValueError('Calendar history event intervals must have explicit offsets.')
        if (ends <= datetime.combine(start, time.min, zone()) or begins >= datetime.combine(end, time.min, zone())):
            raise ValueError('Calendar history contains an out-of-window event.')
    days = [(start + timedelta(days=n)).isoformat() for n in range((end - start).days)]
    with h.write():
        record_calendar(h.db, payload['source'], dict(payload, days=days), stamp(captured.isoformat()))
    return dict(source=payload['source'], start=start.isoformat(), end=end.isoformat(),
                days=len(days), events=len(events))


def save_local_event(h, payload):
    from .card_input import text
    required = {'title', 'start', 'end', 'all_day', 'idempotency_key'}
    if not isinstance(payload, dict) or not required <= payload.keys() or payload.keys() - (required | {'location'}):
        raise ValueError('Invalid local event fields.')
    title = text(payload['title'], 'Title', 180)
    location = payload.get('location', '')
    if not isinstance(location, str) or len(location) > 600 or type(payload['all_day']) is not bool:
        raise ValueError('Invalid location or all-day setting.')
    key = text(payload['idempotency_key'], 'Save key', 100)
    event_id = 'local-' + digest(key)[:24]
    tz = zone()

    def point(value):
        value = text(value, 'Date or time', 40)
        if payload['all_day']:
            return datetime.combine(date.fromisoformat(value), time.min, tz)
        result = datetime.fromisoformat(value)
        if 'T' not in value:
            raise ValueError('Timed entries need both a date and time.')
        if result.tzinfo is not None:
            return result.astimezone(tz)
        from datetime import timezone
        earlier = result.replace(tzinfo=tz, fold=0)
        later = result.replace(tzinfo=tz, fold=1)
        if (earlier.utcoffset() != later.utcoffset() or
                earlier.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None) != result):
            raise ValueError('That local time is ambiguous or unavailable. Use an explicit offset.')
        return earlier

    start, end = point(payload['start']), point(payload['end'])
    if end.timestamp() <= start.timestamp():
        raise ValueError('The end must be after the start.')
    start_day = start.date().isoformat()
    end_day = datetime.fromtimestamp(end.timestamp() - 1, tz).date().isoformat()
    if end_day < cutoff(datetime.now(tz).date()):
        raise ValueError('That event is outside calendar retention.')
    event = dict(id=event_id, title=title, start=start.isoformat(), end=end.isoformat(),
                 all_day=payload['all_day'], location=location.strip(), calendar='Harness only')
    data = dumps(event)
    with h.write():
        row = h.db.execute('SELECT event FROM dashboard_local_events WHERE id=?', (event_id,)).fetchone()
        if row and row[0] != data:
            raise ValueError('This save key already belongs to a different event.')
        h.db.execute('INSERT OR IGNORE INTO dashboard_local_events VALUES (?,?,?,?)',
                     (event_id, start_day, end_day, data))
    return event


def is_important_appointment(action):
    return (action.get('important_appointment') is True or '-important-' in action['id']
            or action.get('next_step', '').lstrip().upper().startswith('IMPORTANT:'))


def appointment_reminders(cards, at):
    from .reviews import active_due
    reminders = []
    for card in cards:
        for action in card['actions']:
            if action['status'] != 'needs-user' or not is_important_appointment(action):
                continue
            due = action.get('explicit_review') or action.get('snooze_until') or action.get('next_review')
            if not due or (action.get('appointment_start') and stamp(action['appointment_start']) <= at):
                continue
            local = datetime.fromisoformat(due).astimezone(zone())
            reminders.append(dict(id=action['id'], case_id=card['id'], revision=card['revision'],
                                  title=action.get('appointment_title') or card['title'],
                                  text=action['next_step'], day=local.date().isoformat(),
                                  due=active_due(action, at)))
    return reminders


def appointment_markers(cards, reminders):
    markers = {}
    by_id = {item['id']: item for item in reminders if item['due']}
    for card in cards:
        for action in card['actions']:
            if not action.get('appointment_start') or not is_important_appointment(action):
                continue
            start = datetime.fromisoformat(stamp(action['appointment_start'])).astimezone(zone())
            key = card['id'], start.isoformat()
            current = markers.get(key)
            if current and (current['due'] or action['status'] == 'needs-user' and action['id'] not in by_id):
                continue
            marker = dict(id=action['id'], title=action.get('appointment_title') or card['title'],
                          text=action['next_step'], due=False)
            markers[key] = dict(marker, **by_id.get(action['id'], {}))
            markers[key].update(day=start.date().isoformat(), start=start.isoformat(),
                                match_title=action.get('appointment_title'))
    return list(markers.values())


def local_reminder_markers(cards, start, until):
    from .followups import due_at
    from .model import OPEN
    markers = []
    for card in cards:
        for action in card['actions']:
            if (action['status'] not in OPEN or is_important_appointment(action)
                    or action.get('show_on_calendar') is not True):
                continue
            dates = {}
            follow_up = action.get('follow_up') or {}
            reminder = (action.get('explicit_review') or follow_up.get('due_at')
                        if follow_up.get('mode') == 'handoff' and action['status'] == 'needs-user'
                        else due_at(action) if follow_up.get('mode') == 'reminder' else None)
            if reminder:
                dates[max(stamp(reminder), stamp(action.get('snooze_until') or reminder))] = 'Reminder'
            if action.get('deadline'):
                dates[stamp(action['deadline'])] = 'Deadline'
            for at, category in dates.items():
                day = datetime.fromisoformat(at).astimezone(zone()).date()
                if start <= day < until:
                    markers.append(dict(day=day.isoformat(), title=card['title'], category=category,
                                        text=action['next_step'], case_id=card['id']))
    return markers


def acknowledge_appointment(h, payload):
    if not isinstance(payload, dict) or payload.keys() != {'id', 'case_id', 'revision'}:
        raise ValueError('Invalid acknowledgement.')
    reminder = next((item for item in appointment_reminders(h.all_cases(open_only=True), stamp(now()))
                     if item['id'] == payload['id'] and item['case_id'] == payload['case_id'] and item['due']), None)
    if not reminder or reminder['revision'] != payload['revision']:
        raise ValueError('The appointment reminder changed. Refresh before acknowledging.')
    cid = reminder['case_id']
    card = h.case(cid)
    action = next(a for a in card['actions'] if a['id'] == reminder['id'])
    evidence = h.put_evidence(dict(source='user-ui', kind='user-instruction', owner_case_id=cid,
                                   body='User acknowledged appointment reminder ' + action['id']))['id']
    action.update(status='done', completion_reason='User acknowledged this appointment reminder in the GUI.',
                  completion_evidence=[evidence])
    summary = {key: card[key] for key in ('title', 'purpose', 'position', 'context', 'status', 'aliases', 'match_keys') if key in card}
    return h.update(dict(id=cid, expected_revision=reminder['revision'],
                         idempotency_key='ui-appointment-ack:' + evidence, evidence_ids=[evidence],
                         case=summary, actions=card['actions']))


def publish(h, run_id, payload):
    body = payload.get('markdown') if isinstance(payload, dict) else None
    if not isinstance(body, str) or not body.strip() or len(body) > 30_000:
        raise ValueError('Report markdown requires 1–30,000 characters.')
    with h.write():
        run = h.run(run_id)
        if run['status'] not in {'complete', 'partial'}:
            raise ValueError('Finalize the mail check before publishing its report.')
        day = datetime.fromisoformat(run['started_at']).astimezone(zone()).date().isoformat()
        if day < cutoff(datetime.now(zone()).date()):
            raise ValueError('This report is outside dashboard retention.')
        for source, raw in h.db.execute('SELECT source,details FROM calendar_state WHERE run_id=?', (run_id,)):
            record_calendar(h.db, source, json.loads(raw), run['started_at'])
        h.db.execute(
            'INSERT INTO dashboard_reports VALUES (?,?,?,?,?) '
            'ON CONFLICT(run_id) DO UPDATE SET markdown=excluded.markdown,status=excluded.status',
            (run_id, run['started_at'], day, run['status'], body.strip()),
        )
    return {'id': run_id, 'day': day, 'published': True}


def read_window(path, start, until):
    from .viewer import ReadStore
    today = datetime.now(zone()).date()
    if not 1 <= (until - start).days <= 42:
        raise ValueError('Choose a calendar window of up to 42 days.')
    with closing(ReadStore(path)) as store, store.read():
        db = store.db
        calendar = [
            {'day': day, 'source': source, 'events': json.loads(events)}
            for day, source, events in db.execute(
                'SELECT day,source,events FROM dashboard_calendar WHERE day >= ? AND day < ? ORDER BY day,source',
                (max(start.isoformat(), cutoff(today)), until.isoformat()),
            )
        ]
        report_days = [row[0] for row in db.execute(
            'SELECT DISTINCT day FROM dashboard_reports WHERE day >= ? AND day < ? ORDER BY day DESC',
            (max(start.isoformat(), cutoff(today)), until.isoformat()),
        )]
        for from_day, through_day, raw in db.execute(
            'SELECT start_day,end_day,event FROM dashboard_local_events WHERE start_day < ? AND end_day >= ?',
            (until.isoformat(), max(start.isoformat(), cutoff(today))),
        ):
            day = max(start, date.fromisoformat(from_day), date.fromisoformat(cutoff(today)))
            last = min(until - timedelta(days=1), date.fromisoformat(through_day))
            event = json.loads(raw)
            while day <= last:
                calendar.append(dict(day=day.isoformat(), source='harness:local', events=[event]))
                day += timedelta(days=1)
        cards = store.all_cases(open_only=True)
        reminders = appointment_reminders(cards, stamp(now()))
        appointments = appointment_markers(cards, reminders)
        categories = dict(db.execute('SELECT identity,category FROM dashboard_event_categories'))
        titles = {}
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='dashboard_event_titles'").fetchone():
            titles = dict(db.execute('SELECT identity,title FROM dashboard_event_titles WHERE end_day >= ?', (start.isoformat(),)))
        for row in calendar:
            for event in row['events']:
                try:
                    identity = event_identity(event)
                    category = categories.get(identity)
                    occurrence = event_occurrence_identity(row['source'], event, identity)
                except (KeyError, TypeError, ValueError):
                    continue
                if row['source'] in CAL:
                    event['occurrence_key'] = occurrence
                if category in EVENT_CATEGORIES:
                    event['category'] = category
                if occurrence in titles:
                    event['display_title'] = titles[occurrence]
    return dict(today=today.isoformat(), timezone=zone().key, sources=CAL, calendar=calendar, report_days=report_days,
                local_reminders=local_reminder_markers(cards, start, until),
                appointments=[item for item in appointments if start.isoformat() <= item['day'] < until.isoformat()],
                reminders=[r for r in reminders if r['due'] or start.isoformat() <= r['day'] < until.isoformat()])


def read_report(path, run_id):
    from .viewer import ReadStore
    with closing(ReadStore(path)) as store:
        row = store.db.execute(
            'SELECT * FROM dashboard_reports WHERE run_id=? AND day>=?',
            (run_id, cutoff(datetime.now(zone()).date())),
        ).fetchone()
    return dict(row) if row else None


def read_daily_reports(path, day):
    from .viewer import ReadStore
    if day.isoformat() < cutoff(datetime.now(zone()).date()):
        return []
    with closing(ReadStore(path)) as store:
        return [dict(row) for row in store.db.execute(
            'SELECT * FROM dashboard_reports WHERE day=? ORDER BY julianday(started_at), run_id',
            (day.isoformat(),),
        )]
