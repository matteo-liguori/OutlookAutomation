"""Read-only projections of current cards. No startup maintenance or Outlook access."""
from contextlib import contextmanager
import hashlib
from pathlib import Path
import sqlite3

from .model import OPEN, now, stamp
from .records import CaseRecords
from .storage import CURRENT_SCHEMA
from .followups import due_at
from .reviews import active_due, due as reviews_due


class ReadStore(CaseRecords):
    def __init__(self, path):
        self.db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA query_only=ON')
        if self.db.execute('PRAGMA user_version').fetchone()[0] != CURRENT_SCHEMA:
            self.db.close()
            raise ValueError('Database format does not match this viewer; update the code before opening it.')

    @contextmanager
    def read(self):
        nested = self.db.in_transaction
        if not nested:
            self.db.execute('BEGIN')
        try:
            yield
        finally:
            if not nested:
                self.db.rollback()

    def close(self):
        self.db.close()


def colour(case_id):
    """Stable ID-derived colour: adding, closing or sorting cards cannot reassign it."""
    value = hashlib.sha256(case_id.encode()).digest()
    hue = int.from_bytes(value[:3], 'big') / 0xffffff * 360
    return f'hsl({hue:.4f} {50 + value[3] % 25}% {38 + value[4] % 18}%)'


def action_view(action, at):
    fields = ('id', 'owner', 'status', 'next_step', 'deadline', 'promised_at',
              'next_review', 'snooze_until', 'completion_condition', 'reopening_condition')
    result = {k: action[k] for k in fields if action.get(k) is not None}
    result['snoozed'] = bool(action.get('snooze_until') and stamp(action['snooze_until']) > at and not action.get('snooze_override'))
    result['due'] = active_due(action, at)
    if (action.get('follow_up') or {}).get('mode') == 'reminder':
        result['follow_up_at'] = due_at(action)
    result['urgency'] = action.get('review_policy', {}).get('urgency', 'normal')
    result['depends_on'] = (action.get('follow_up') or {}).get('depends_on', [])
    return result


def action_dates(action, at):
    """Group coincident dates without losing the distinction between clocks."""
    dates = {}
    snooze = stamp(action.get('snooze_until')) if not action.get('snooze_override') else None
    def add(value, kind, suppress=False):
        value = stamp(value)
        if value and not (suppress and snooze and snooze > at and value < snooze):
            dates.setdefault(value, []).append(kind)
    add(action.get('deadline'), 'deadline')
    add(action.get('promised_at'), 'promise')
    policy = action.get('follow_up') or {}
    if policy.get('mode') == 'reminder':
        add(due_at(action), 'follow-up', True)
    add(action.get('next_review'), 'review', True)
    if snooze and snooze > at:
        add(snooze, 'snooze-end')
    return [(date, list(dict.fromkeys(kinds))) for date, kinds in sorted(dates.items())]


def overview(path, at=None):
    at = stamp(at or now())
    store = ReadStore(path)
    try:
        with store.read():
            cases = store.all_cases(open_only=True)
            reviews = {v['case']: v['due_at'] for v in reviews_due(store, at)}
            cards, events, unscheduled = [], [], []
            for case in cases:
                cid = case['id']
                actions = [a for a in case['actions'] if a['status'] in OPEN]
                card = dict(id=cid, title=case['title'], purpose=case.get('purpose') or case['title'],
                            position=case['position'], revision=case['revision'], colour=colour(cid),
                            updated_at=case['updated_at'], review_due_at=reviews.get(cid),
                            actions=[action_view(a, at) for a in actions])
                cards.append(card)
                for action in actions:
                    common = dict(case_id=cid, title=case['title'], colour=card['colour'],
                                  action=action_view(action, at))
                    dates = action_dates(action, at)
                    if not dates:
                        unscheduled.append(common)
                    for date, kinds in dates:
                        events.append(dict(common, id=action['id'] + '@' + date, at=date, kinds=kinds))
            events.sort(key=lambda e: (e['at'], e['case_id'], e['action']['id']))
            return dict(cards=cards, events=events, unscheduled=unscheduled,
                        as_of=at, timezone='Europe/London', read_only=True)
    finally:
        store.close()


def detail(path, case_id):
    store = ReadStore(path)
    try:
        with store.read():
            case = store.case(case_id)
            if case['status'] != 'open':
                raise ValueError('This card is no longer open. Refresh the current view.')
            fields = ('id', 'title', 'purpose', 'context', 'position', 'revision', 'updated_at',
                      'card_reviewed_at', 'review_outcome', 'related_cases')
            result = {k: case[k] for k in fields if case.get(k) is not None}
            at = stamp(now())
            result.update(colour=colour(case_id), actions=[action_view(a, at) for a in case['actions'] if a['status'] in OPEN],
                          decisions=[d.get('text', '') for d in case['decisions']],
                          evidence=[{k: e.get(k) for k in ('id', 'at', 'label')} for e in case.get('evidence_index', [])])
            return result
    finally:
        store.close()
