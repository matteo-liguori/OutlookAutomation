from .configuration import resolve
"""Periodic whole-card reviews, independent of action and reply clocks."""

from datetime import datetime, timedelta
import json
import os
from .model import OPEN, now, stamp, digest, dumps


def active_due(action, at):
    if action['status'] not in OPEN:
        return False
    if action.get('snooze_until') and action['snooze_until'] > at and not action.get('snooze_override'):
        return False
    from .followups import reminder_due
    if reminder_due(action, at):
        return True
    due = action.get('next_review')
    if not due or due > at:
        return False
    return action['status'] == 'needs-user' or action.get('review_rule') in {
        'explicit-date', 'user-snooze', 'deadline', 'initial-deadline-review',
        'promised-date', 'no-reply-follow-up',
    }


def due(h, at=None):
    at = stamp(at or now())
    result = []
    for c in h.all_cases(open_only=True):
        # Action work already requires a whole-card review; do not list it twice.
        if any(active_due(a, at) for a in c['actions']):
            continue
        actions = [a for a in c['actions'] if a['status'] in OPEN]
        if actions and all(a.get('snooze_until', '') > at and not a.get('snooze_override') for a in actions):
            continue
        interval = c.get('review_interval_days', resolve(card=c).get('reviews.card_days'))
        base = c.get('card_reviewed_at') or c.get('created_at')
        next_at = stamp((datetime.fromisoformat(stamp(base)) + timedelta(days=interval)).isoformat()) if base else at
        if next_at <= at:
            result.append(dict(type='review_due', case=c['id'], title=c['title'], due_at=next_at))
    return sorted(result, key=lambda x: (x['due_at'], x['case']))


def activity_key():
    return 'card-review-activity:' + os.environ.get('CODEX_THREAD_ID', 'local')


def record_activity(h, at, state=None):
    if state is None:
        row = h.db.execute('SELECT value FROM meta WHERE key=?', (activity_key(),)).fetchone()
        state = json.loads(row[0]) if row else {}
    state['last_at'] = at
    with h.write():
        h.db.execute('INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                     (activity_key(), dumps(state)))


def next_card(h, at=None, cursor=None, limit=50):
    from .cards import show
    from .guidance import guide
    at = stamp(at or now())
    row = h.db.execute('SELECT value FROM meta WHERE key=?', (activity_key(),)).fetchone()
    state = json.loads(row[0]) if row else {}
    intro = not state.get('last_at') or datetime.fromisoformat(at)-datetime.fromisoformat(state['last_at']) > timedelta(hours=2)
    if cursor:
        intro = cursor.split(':')[1] == state.get('intro_token')
    pending = due(h, at)
    items = []
    if pending:
        if intro:
            items.append(dict(type='review_instructions', instruction=guide('card-upkeep')['instruction']))
        items.append(show(h, pending[0]['case']))
    result = h.page('review:next', items, cursor=cursor, limit=limit)
    if not cursor:
        state['intro_token'] = result['next_cursor'].split(':')[1] if intro and result['next_cursor'] else None
    if pending:
        record_activity(h, at, state)
    result.update(remaining=len(pending), complete=not pending)
    return result


def complete(h, p, at=None):
    """Explicitly acknowledge a whole-card review; merely reading never counts."""
    at = stamp(at or now())
    request_hash = digest(p)
    previous = h.db.execute('SELECT result FROM mutations WHERE key=?', (p['idempotency_key'],)).fetchone()
    if previous:
        result = json.loads(previous[0])
        if result.get('request_hash') != request_hash:
            raise ValueError('Idempotency key reused for different review')
        return result
    h.limited(p, 'outcome', 600)
    c = h.case(p['id'])
    if c['status'] != 'open':
        raise ValueError('Use case update to close a card; review complete keeps it open')
    case = {k:v for k,v in c.items() if k not in {'id','revision','updated_at','actions','decisions','evidence_ids'}}
    case['card_reviewed_at'] = at
    case['review_interval_days'] = p.get('interval_days', c.get('review_interval_days', resolve(card=c).get('reviews.card_days')))
    evidence = p.get('evidence_ids', [])
    if not evidence:
        raise ValueError('Review completion needs evidence and a keep reason')
    # The outcome belongs in the current card, not a separate review diary.
    case['review_outcome'] = p['outcome']
    result = h.update(dict(id=c['id'], expected_revision=p['expected_revision'],
                        idempotency_key=p['idempotency_key'], evidence_ids=c['evidence_ids']+evidence,
                        case=case, actions=c['actions'], decisions=c['decisions'], request_hash=request_hash))
    record_activity(h, at)
    return result
