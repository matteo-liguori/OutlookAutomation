"""Narrow user edits and a pending-note inbox in the canonical card record."""
import json
from .model import digest, dumps, now


NOTE_INSTRUCTION = (
    'Handle the pending user notes on this card using relevant context and existing permissions. '
    'A note is not proof that an external action happened. Record any resulting task/context changes, '
    'then use case note-ack with id, expected_revision, idempotency_key, note_ids and outcome. '
    'Leave notes pending when further work or clarification is needed. Acknowledge before closing or merging the card.'
)


class Conflict(ValueError):
    pass


def text(value, name, cap):
    if not isinstance(value, str) or not value.strip() or len(value) > cap:
        raise ValueError(f'{name} requires 1–{cap} characters')
    return value.strip()


def apply(h, payload, acknowledge=False):
    required = {'id', 'expected_revision', 'idempotency_key'}
    allowed = required | ({'note_ids', 'outcome'} if acknowledge else {'title', 'purpose', 'note'})
    if not isinstance(payload, dict) or not required <= payload.keys() or payload.keys() - allowed:
        raise ValueError('Invalid card input fields')
    cid = text(payload['id'], 'id', 180)
    key = text(payload['idempotency_key'], 'idempotency_key', 180)
    if type(payload['expected_revision']) is not int:
        raise ValueError('expected_revision must be an integer')
    fingerprint = digest(['card-input', acknowledge, payload])
    with h.write():
        retry = h.db.execute('SELECT hash,result FROM mutations WHERE key=?', (key,)).fetchone()
        if retry:
            if retry['hash'] != fingerprint:
                raise ValueError('Idempotency key reused for different content')
            return json.loads(retry['result'])
        row = h.db.execute('SELECT * FROM cases WHERE id=?', (cid,)).fetchone()
        if not row or row['status'] != 'open':
            raise Conflict('This card is no longer open. Reload the current view.')
        if row['revision'] != payload['expected_revision']:
            raise Conflict('This card changed since you opened it. Your text is kept; reload the latest card before saving.')
        details = json.loads(row['details'])
        title = row['title']
        notes = list(details.get('user_notes', []))
        at = now()
        evidence_id = digest(['card-input', cid, key])
        if acknowledge:
            ids = payload.get('note_ids')
            if not isinstance(ids, list) or not ids or any(not isinstance(n, str) for n in ids) or len(ids) != len(set(ids)):
                raise ValueError('note_ids requires distinct pending note IDs')
            if not set(ids) <= {n['id'] for n in notes}:
                raise Conflict('A note is no longer pending; inspect the current card.')
            outcome = text(payload.get('outcome'), 'outcome', 600)
            details['user_notes'] = [n for n in notes if n['id'] not in ids]
            body = 'Agent handled user notes ' + ', '.join(ids) + ': ' + outcome
            label = 'User notes handled'
        else:
            changed = {}
            for field, cap in [('title', 180), ('purpose', 600)]:
                if field not in payload:
                    continue
                value = text(payload[field], field, cap)
                if field == 'purpose' and len(value.splitlines()) > 3:
                    raise ValueError('Recognition/goal must have at most three lines')
                previous = title if field == 'title' else details.get(field, title)
                if value != previous:
                    changed[field] = value
                    if field == 'title': title = value
                    else: details[field] = value
            note = text(payload['note'], 'note', 2000) if 'note' in payload else None
            if note:
                notes.append(dict(id=evidence_id, text=note, created_at=at, evidence_id=evidence_id))
                details['user_notes'] = notes
            if not changed and not note:
                result = dict(id=cid, revision=row['revision'], pending_notes=len(notes))
                h.db.execute('INSERT INTO mutations VALUES(?,?,?)', (key, fingerprint, dumps(result)))
                return result
            body = dumps(dict(changed=changed, note=note))
            label = 'User card input'
        # Metadata only: action clocks, review timestamps, context and permissions
        # remain untouched. Evidence and the guarded edit commit together.
        h.db.execute('INSERT INTO evidence VALUES(?,?,?)', (evidence_id, dumps(dict(
            source='user-ui' if not acknowledge else 'agent', kind='card-input',
            owner_case_id=cid, at=at, label=label)), body))
        from .cards import evidence_index
        details['evidence_index'] = evidence_index(h, [evidence_id], details.get('evidence_index', []))
        revision = row['revision'] + 1
        h.db.execute('UPDATE cases SET title=?,details=?,revision=?,updated_at=? WHERE id=? AND revision=?',
                     (title, dumps(details), revision, at, cid, row['revision']))
        result = dict(id=cid, revision=revision, pending_notes=len(details.get('user_notes', [])))
        h.db.execute('INSERT INTO mutations VALUES(?,?,?)', (key, fingerprint, dumps(result)))
        return result


def pending(h, case_ids=None):
    selected = set(case_ids) if case_ids is not None else None
    result = []
    for row in h.db.execute("SELECT id,title,details FROM cases WHERE status='open' ORDER BY id"):
        if selected is not None and row['id'] not in selected:
            continue
        notes = json.loads(row['details']).get('user_notes', [])
        if notes:
            result.append(dict(type='user_note_prompt', case=row['id'], title=row['title'], count=len(notes),
                               preview=notes[0]['text'][:160],
                               instruction=f"User notes pending on {row['id']}: use case show {row['id']}; handle them before other work on the card. help card-input explains acknowledgement."))
    return result
