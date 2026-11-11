"""Current permissions and grant-anchored monthly user reviews; no review diary."""

import calendar
import json
import re
from datetime import datetime
from .model import LONDON, dumps, digest, now, stamp

TABLES = {'one-off': 'approvals', 'standing': 'standing_approvals'}


def sending_notice(h, case_id, at=None):
    """Surface candidate grants; only the assistant can judge message eligibility."""
    at = stamp(at or now())
    grants = []
    from .threadcache import pending_approval
    for row in h.db.execute('SELECT case_id,details FROM standing_approvals ORDER BY id'):
        if row['case_id'] and row['case_id'] != case_id:
            continue
        grant = json.loads(row['details'])
        scope = grant.get('scope', {})
        operation = str(scope.get('operation', '')).lower()
        tokens = re.split(r'[^a-z]+', operation)
        if not any(t in {'send', 'sending', 'forward', 'forwarding', 'reply', 'respond'} for t in tokens):
            continue
        if not pending_approval(grant):
            continue
        blocked = bool(grant.get('status', 'approved') != 'approved' or grant.get('suspended') or (grant.get('expires_at') and grant['expires_at'] <= at))
        grants.append(dict(id=grant['id'], revision=grant.get('revision', 0),
                           case_id=row['case_id'], status=grant.get('status', 'approved'),
                           title=grant.get('title'), summary=grant.get('summary'),
                           binding='thread' if row['case_id'] else 'global-scope-check',
                           available=not blocked, scope=scope,
                           triggers=grant.get('triggers'), boundaries=grant.get('boundaries'),
                           expires_at=grant.get('expires_at'), suspended=bool(grant.get('suspended')),
                           evidence_ids=grant.get('evidence_ids', [])))
    if not grants:
        return {}
    return dict(standing_sending_permissions=grants,
                standing_sending_instruction='Check these SQLite standing sending permissions against the full message and relevant attachments now. Global grants are candidates, not verified matches. For an available grant whose exact trigger, sender, recipients and limits qualify, carry out the authorised send/forward without asking again, using duplicate checks and independent Sent verification. Suspended/expired or ambiguous scope does not authorise sending. A repeated cache/read notice is not a reason to resend.')


def next_monthly(granted_at, after):
    """London anniversary, clamped for short months; never drift from the grant."""
    if not granted_at:
        return None
    origin = datetime.fromisoformat(stamp(granted_at)).astimezone(LONDON)
    after = datetime.fromisoformat(stamp(after))
    months = max(1, (after.year-origin.year)*12+after.month-origin.month)
    while True:
        year, month = divmod(origin.year*12+origin.month-1+months, 12)
        month += 1
        candidate = origin.replace(year=year, month=month,
                                   day=min(origin.day, calendar.monthrange(year, month)[1]))
        result = stamp(candidate.isoformat())
        if result > stamp(after.isoformat()):
            return result
        months += 1


def find(h, aid):
    for kind, table in TABLES.items():
        row = h.db.execute(f'SELECT details FROM {table} WHERE id=?', (aid,)).fetchone()
        if row:
            return kind, table, json.loads(row[0])
    raise ValueError('Unknown approval ' + aid)


def items(h, kind=None, at=None, due_only=False):
    at = stamp(at or now())
    out = []
    for k, table in TABLES.items():
        if kind and k != kind:
            continue
        for row in h.db.execute(f'SELECT id,case_id,details FROM {table} ORDER BY id'):
            a = json.loads(row['details'])
            from .threadcache import pending_approval
            if not pending_approval(a):
                continue
            # Unknown historic grant dates are surfaced honestly, never reset to migration time.
            due = a.get('review_requested_at') or a.get('next_review_at')
            expired = bool(a.get('expires_at') and a['expires_at'] <= at)
            required = expired or bool(a.get('suspended')) or not due or due <= at
            if due_only and not required:
                continue
            out.append(dict(type='approval_review', id=row['id'], kind=k,
                            case_id=row['case_id'], revision=a.get('revision', 0),
                            title=a.get('title') or a.get('message', {}).get('subject') or row['id'],
                            summary=a.get('summary') or 'Review the exact stored permission before use.',
                            granted_at=a.get('granted_at'), next_review_at=due,
                            review_due=required, suspended=bool(a.get('suspended')) or expired,
                            status=a.get('status', 'approved'),
                            reason=a.get('review_reason') or ('Original grant date unrecorded; confirm retention.' if not due else 'Monthly permission review'),
                            question='Keep this permission, change it, or revoke it?'))
    return out


def put(h, payload):
    p = json.loads(dumps(payload))
    for field in ('granted_at', 'expires_at'):
        if field in p:
            p[field] = stamp(p[field])
    kind = p.get('kind', 'one-off')
    if kind not in TABLES:
        raise ValueError('Approval kind must be standing or one-off')
    table = TABLES[kind]
    h.limited(p, 'id', 180)
    if not p.get('evidence_ids'):
        raise ValueError('Approval requires actual user approval evidence')
    h.evidence_exists(p['evidence_ids'])
    from .evidence import scope
    with h.write():
        p = scope(h, p.get('case_id'), p)
        if p.get('case_id'):
            h.case(p['case_id'])
        if kind == 'standing':
            h.limited(p, 'title', 180); h.limited(p, 'summary', 600)
            if not isinstance(p.get('scope'), dict) or not p['scope']:
                raise ValueError('Standing approval requires exact scope, triggers and boundaries')
            if len(dumps(p['scope'])) > 12000:
                raise ValueError('Approval scope exceeds 12000 characters')
            identity = dict(case_id=p.get('case_id'), scope=p['scope'], end_on_case_close=p.get('end_on_case_close', bool(p.get('case_id'))))
        else:
            if p.get('message'):
                if not all(k in p['message'] for k in ('sender','to','cc','bcc','subject','body','attachments')):
                    raise ValueError('Approval message is incomplete')
                p['message_hash'] = digest(p['message'])
            elif not isinstance(p.get('scope'), dict) or not p['scope']:
                raise ValueError('One-off approval requires an exact message or task scope')
            identity = dict(case_id=p.get('case_id'), message=p.get('message'), scope=p.get('scope'))
        fingerprint = digest(identity)
        try:
            old_kind, _, old = find(h, p['id'])
        except ValueError:
            old_kind, old = kind, None
        if old and (old_kind != kind or old.get('scope_hash') != fingerprint):
            raise ValueError('Approval does not transfer to changed content or scope; record a fresh grant')
        if old:
            # Use/read/record retries never renew permission or move its review date.
            for key in ('granted_at','recorded_at','last_reviewed_at','next_review_at',
                        'review_requested_at','review_reason','suspended'):
                if key in p and p[key] != old.get(key):
                    raise ValueError('Use approval review/flag to change review state')
                if key in old:
                    p[key] = old[key]
            if p.get('expires_at') != old.get('expires_at'):
                raise ValueError('Expiry changes require a fresh user grant')
            if p.get('status', old.get('status', 'approved')) != old.get('status', 'approved'):
                from .threadcache import pending_approval
                if not pending_approval(old):
                    raise ValueError('Spent permission cannot be reactivated; record a fresh user grant')
        else:
            for key in ('last_reviewed_at','next_review_at','review_requested_at','suspended'):
                if key in p:
                    raise ValueError('Review state is calculated by the harness')
            p['granted_at'] = stamp(p.get('granted_at'))
            if p['granted_at'] and p['granted_at'] > now():
                raise ValueError('Grant cannot be in the future')
            p['recorded_at'] = now()
            p['next_review_at'] = next_monthly(p['granted_at'], p['granted_at']) if p['granted_at'] else None
        if p.get('expires_at'):
            p['expires_at'] = stamp(p['expires_at'])
        p.setdefault('status', (old or {}).get('status', 'approved'))
        p.update(kind=kind, scope_hash=fingerprint, revision=(old or {}).get('revision', 0)+1)
        if kind == 'standing':
            p.setdefault('end_on_case_close', bool(p.get('case_id')))
        h.db.execute(f'INSERT INTO {table} VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET details=excluded.details',
                     (p['id'], p.get('case_id'), dumps(p)))
        lifecycle(h)
    return dict(id=p['id'], kind=kind, revision=p['revision'])


def review(h, p, at=None):
    at = stamp(at or now())
    with h.write():
        kind, table, a = find(h, p['id'])
        from .evidence import scope
        p = scope(h, a.get('case_id'), p)
        if p.get('expected_revision') != a.get('revision', 0):
            raise ValueError('Stale approval revision')
        if p.get('decision') not in {'keep', 'revoke', 'spent'}:
            raise ValueError('Review decision must be keep, revoke or spent')
        if not p.get('evidence_ids') or not p.get('reason'):
            raise ValueError('Review requires user confirmation or conclusive outcome evidence and reason')
        h.evidence_exists(p['evidence_ids'])
        if p['decision'] == 'spent' and kind == 'standing' and not p.get('scope_ended'):
            raise ValueError('Standing retirement requires evidence that its scope ended')
        if p['decision'] in {'revoke', 'spent'}:
            h.db.execute(f'DELETE FROM {table} WHERE id=?', (p['id'],))
            return dict(id=p['id'], retired=True)
        if a.get('expires_at') and a['expires_at'] <= at:
            raise ValueError('Expired permission requires a fresh grant, not a retention review')
        from .threadcache import pending_approval
        if not pending_approval(a):
            raise ValueError('Spent permission requires a fresh grant')
        a['last_reviewed_at'] = at
        a['review_evidence_ids'] = list(p['evidence_ids'])
        # A missing original date gets a truthful new anchor only on explicit user confirmation.
        if not a.get('granted_at'):
            a['granted_at'] = at
            a['grant_date_basis'] = 'User reconfirmation; original date unknown'
        a['next_review_at'] = next_monthly(a['granted_at'], at)
        for key in ('review_requested_at','review_reason','suspended'):
            a.pop(key, None)
        a['revision'] = a.get('revision', 0)+1
        h.db.execute(f'UPDATE {table} SET details=? WHERE id=?', (dumps(a), p['id']))
        return dict(id=p['id'], revision=a['revision'], next_review_at=a['next_review_at'])


def flag(h, p):
    with h.write():
        _, table, a = find(h, p['id'])
        from .evidence import scope
        p = scope(h, a.get('case_id'), p)
        if p.get('expected_revision') != a.get('revision', 0):
            raise ValueError('Stale approval revision')
        if not p.get('reason') or not p.get('evidence_ids'):
            raise ValueError('Early review requires material evidence and a reason')
        h.evidence_exists(p['evidence_ids'])
        a.update(review_requested_at=now(), review_reason=p['reason'],
                 review_evidence_ids=p['evidence_ids'], suspended=bool(p.get('suspended')),
                 revision=a.get('revision', 0)+1)
        h.db.execute(f'UPDATE {table} SET details=? WHERE id=?', (dumps(a), p['id']))
        return dict(id=p['id'], revision=a['revision'])


def lifecycle(h, at=None):
    """Only explicit expiry or an explicitly case-bound scope ends automatically."""
    at = stamp(at or now())
    for row in h.db.execute('SELECT id,case_id,details FROM standing_approvals').fetchall():
        a = json.loads(row['details'])
        case = h.db.execute('SELECT status FROM cases WHERE id=?', (row['case_id'],)).fetchone()
        if ((a.get('expires_at') and a['expires_at'] <= at) or
                (case and case[0] == 'closed' and a.get('end_on_case_close'))):
            h.db.execute('DELETE FROM standing_approvals WHERE id=?', (row['id'],))
