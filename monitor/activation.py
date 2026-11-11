from .configuration import resolve
from .configuration import zone
"""One pending mail-chat activation checklist per thread and London day."""
import json
import os
from datetime import datetime
from .model import dumps, now
from .timing import plan, verify


def _observe(h, state, user_active=False):
    from .extensions import event
    event(h, 'activation', dict(state=state, user_active=user_active))


def identity(at=None):
    thread = os.environ.get('CODEX_THREAD_ID', '')
    if not thread:
        raise ValueError('Chat activation requires CODEX_THREAD_ID')
    day = datetime.fromisoformat(at or now()).astimezone(zone()).date()
    return thread, day, 'mail-activation:' + thread


def arm(h, user_active=False):
    thread, day, key = identity()
    with h.write():
        row = h.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        if row and json.loads(row[0]).get('detached'):
            return
        if not row or json.loads(row[0]).get('date') != str(day):
            h.db.execute('INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                         (key, dumps(dict(thread_id=thread, date=str(day), armed_at=now()))))
        elif user_active:
            state = json.loads(row[0])
            state.pop('deferred_until_user', None)
            h.db.execute('UPDATE meta SET value=? WHERE key=?', (dumps(state), key))
        state = json.loads(h.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()[0])
        _observe(h, state, user_active)


def reminder(h):
    if not os.environ.get('CODEX_THREAD_ID'):
        return None
    _, day, key = identity()
    row = h.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    state = json.loads(row[0]) if row else {}
    if state.get('detached') or state.get('date') != str(day) or state.get('completed_at') or state.get('deferred_until_user'):
        return None
    missing = state.get('missing')
    if missing:
        return 'Pending: '+', '.join(missing)+'. See help activation.'
    return 'Pending: verify chat title and same-day mail follow-ups; then activation complete --input -. See help activation.'


def startup(h, at):
    """Supply only this mail session's initial timing and next instructions."""
    if not os.environ.get('CODEX_THREAD_ID'):
        return None
    _, day, key = identity(at)
    row = h.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    state = json.loads(row[0]) if row else {}
    if state.get('detached'):
        return None
    arm(h)
    row = h.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    state = json.loads(row[0])
    if state.get('first_interaction_at'):
        return None
    with h.write():
        state['first_interaction_at'] = at
        h.db.execute('UPDATE meta SET value=? WHERE key=?', (dumps(state),key))
    return dict(first_interaction_london=datetime.fromisoformat(at).astimezone(zone()).isoformat(),
                title=resolve().get('schedule.title').format(date=day),
                remaining_followups=[p['time'] for p in plan(at=at)['eligible']],
                instruction='Use help activation to complete chat setup now; then TRIAGE_RULES.md and MONITOR_REPORT_TEMPLATE.md. Retrieve other instructions only for relevant operations.')


def defer(h, payload):
    _, day, key = identity()
    if not payload.get('reason') or not payload.get('evidence_ids'):
        raise ValueError('Deferral requires the actual failed attempt reason and evidence')
    with h.write():
        h.evidence_exists(payload['evidence_ids'])
        row = h.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        state = json.loads(row[0]) if row else {}
        if state.get('date') != str(day) or state.get('completed_at'):
            raise ValueError('No pending checklist to defer')
        state.update(deferred_until_user=True, failure_reason=payload['reason'],
                     failure_evidence_ids=payload['evidence_ids'])
        h.db.execute('UPDATE meta SET value=? WHERE key=?', (dumps(state), key))
        _observe(h, state)
    return dict(deferred_until_user=True)


def detach(h, payload):
    thread, _, key = identity()
    if not payload.get('reason') or not payload.get('evidence_ids'):
        raise ValueError('Detaching a mail chat requires user instruction evidence')
    with h.write():
        h.evidence_exists(payload['evidence_ids'])
        h.db.execute('INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                     (key, dumps(dict(thread_id=thread, detached=True, reason=payload['reason'],
                                      evidence_ids=payload['evidence_ids']))))
    return dict(detached=True, thread_id=thread)


def reconcile(h):
    """Check facts at briefing time; never infer human presence from a running agent."""
    if not os.environ.get('CODEX_THREAD_ID'):
        return
    thread, day, key = identity()
    row = h.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    state = json.loads(row[0]) if row else {}
    if state.get('detached') or state.get('date') != str(day):
        return
    from .app_state import inspect
    observed = inspect(thread, resolve().get('schedule.title').format(date=day))
    checks = {'title':observed['title'], 'same-day follow-ups':observed['followups']}
    if state.get('followup',{}).get('status') == 'declined':
        checks['same-day follow-ups'] = True
    missing = [name for name, value in checks.items() if value is False]
    if observed.get('duplicate_followups'):
        missing = ['duplicate same-day follow-ups' if name == 'same-day follow-ups' else name for name in missing]
    with h.write():
        state.update(checked_at=now(), observed=observed, missing=missing)
        if missing:
            state.pop('completed_at', None)
        elif all(value is True for value in checks.values()):
            state['completed_at'] = now()
        h.db.execute('UPDATE meta SET value=? WHERE key=?', (dumps(state), key))
        _observe(h, state)


def complete(h, payload):
    thread, day, key = identity()
    title = resolve().get('schedule.title').format(date=day)
    if payload.get('title_readback') != title:
        raise ValueError('Exact current London-date title readback required')
    evidence_ids = payload.get('evidence_ids', [])
    if not evidence_ids:
        raise ValueError('Activation completion requires readback evidence')
    current_plan = plan()
    followup = payload.get('followup', {})
    decline_evidence = payload.get('decline_evidence_ids', [])
    if current_plan['eligible']:
        if followup.get('status') == 'declined':
            if not decline_evidence:
                raise ValueError('Declined follow-ups require explicit user-instruction evidence')
        else:
            if followup.get('status') != 'ACTIVE' or followup.get('target_thread_id') != thread:
                raise ValueError('Active follow-up must target this chat')
            verify(dict(plan=current_plan, actual_rrule=followup.get('rrule', '')))
    elif followup.get('status') != 'none-needed':
        raise ValueError('Record none-needed when no same-day follow-ups remain')
    with h.write():
        h.evidence_exists(evidence_ids)
        h.evidence_exists(decline_evidence)
        row = h.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        if not row or json.loads(row[0]).get('date') != str(day):
            raise ValueError('Use briefing --user-active to start the checklist')
        state = json.loads(row[0])
        state.update(completed_at=now(), title_readback=title,
                     evidence_ids=evidence_ids, followup=followup, decline_evidence_ids=decline_evidence)
        h.db.execute('UPDATE meta SET value=? WHERE key=?', (dumps(state), key))
        _observe(h, state)
    return dict(completed=True, thread_id=thread, date=str(day))
