from .configuration import resolve
"""Independent thread evidence and short-lived unclaimed inputs; no shared owners."""

import json
from datetime import datetime, timedelta
from .model import digest, dumps, now, stamp


def references(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key == 'evidence_index':
                yield from (e['id'] for e in child)
            elif key.endswith('evidence_ids') or key == 'completion_evidence':
                yield from child
            elif key.endswith('evidence_id') and isinstance(child, str):
                yield child
            else:
                yield from references(child)
    elif isinstance(value, list):
        for child in value:
            yield from references(child)


def rewrite(value, mapping):
    """Only evidence identifiers change; never action IDs, clocks or message text."""
    if isinstance(value, dict):
        out = {}
        for key, child in value.items():
            if key == 'evidence_index':
                out[key] = [dict(e, id=mapping.get(e['id'], e['id'])) for e in child]
            elif key.endswith('evidence_ids') or key == 'completion_evidence':
                out[key] = list(dict.fromkeys(mapping.get(e, e) for e in child))
            elif key.endswith('evidence_id') and isinstance(child, str):
                out[key] = mapping.get(child, child)
            else:
                out[key] = rewrite(child, mapping)
        return out
    if isinstance(value, list):
        return [rewrite(child, mapping) for child in value]
    return value


def scope(h, case_id, value):
    """The first owner keeps the ID; another thread gets its own independent copy."""
    mapping = {}
    for eid in set(references(value)):
        row = h.db.execute('SELECT metadata,body FROM evidence WHERE id=?', (eid,)).fetchone()
        if not row:
            raise ValueError('Unknown evidence ' + eid)
        meta = json.loads(row[0])
        if meta.get('kind') == 'expired-thread-reference':
            target = digest(['thread-evidence', case_id, eid])
            own = h.db.execute('SELECT metadata FROM evidence WHERE id=?', (target,)).fetchone()
            if not own or json.loads(own[0]).get('owner_case_id') != case_id:
                raise ValueError('Expired evidence cannot support new work; retrieve the source again')
            mapping[eid] = target
            continue
        owner = meta.get('owner_case_id', meta.get('case_id'))
        standing = meta.get('scope') == 'standing'
        from .live import OPERATIONAL
        operational = meta.get('scope') == 'operational' or meta.get('kind') in OPERATIONAL or str(meta.get('kind','')).startswith('filing')
        if case_id is not None and (standing or operational):
            continue  # A preference is separate current context, not a thread asset.
        if owner == case_id and (case_id is not None or standing):
            continue
        if owner is None and not standing:
            meta['owner_case_id'] = case_id
            if case_id is None:
                meta['scope'] = 'standing'
            h.db.execute('UPDATE evidence SET metadata=? WHERE id=?', (dumps(meta), eid))
            continue
        # No ownership list or reference counting. Expiring either case only
        # touches that owner's row, including when the other copy stays open.
        origin = meta.get('source_evidence_id', eid)
        target = digest(['thread-evidence', case_id, origin])
        source = h.db.execute('SELECT metadata FROM evidence WHERE id=?', (origin,)).fetchone()
        if source:
            source_meta = json.loads(source[0])
            if source_meta.get('owner_case_id', source_meta.get('case_id')) == case_id and source_meta.get('kind') != 'expired-thread-reference':
                target = origin
        meta['owner_case_id'] = case_id
        meta['source_evidence_id'] = meta.get('source_evidence_id', eid)
        if case_id is None:
            meta['scope'] = 'standing'
            meta.pop('case_id', None)
        else:
            meta.pop('scope', None)
        existing = h.db.execute('SELECT metadata,body FROM evidence WHERE id=?', (target,)).fetchone()
        if existing and (json.loads(existing[0]).get('owner_case_id') != case_id or existing[1] != row[1]):
            raise ValueError('Conflicting independent evidence copy')
        h.db.execute('INSERT OR IGNORE INTO evidence VALUES(?,?,?)', (target, dumps(meta), row[1]))
        h.touch('evidence', target)
        mapping[eid] = target
    return rewrite(value, mapping)


def has_card(value):
    if isinstance(value, dict):
        if isinstance(value.get('actions'), list) and 'position' in value:
            return True
        return any(has_card(child) for child in value.values())
    return isinstance(value, list) and any(has_card(child) for child in value)


def prune(h, at=None):
    """Retain current roots, independent case assets and at most 24h of new inputs."""
    at = stamp(at or now())
    cutoff = stamp((datetime.fromisoformat(at) - timedelta(days=resolve().get('maintenance.cache_days'))).isoformat())
    roots = set()
    for table, column in [('cases','details'),('actions','details'),('decisions','details'),
                          ('approvals','details'),('standing_approvals','details'),('context','details'),('issues','details'),
                          ('roundup','details'),('runs','details'),('sources','details'),
                          ('candidates','details'),('pending_work','details'),
                          ('message_reviews','details'),('calendar_state','details'),
                          ('calendar_reports','details'),('thread_messages','metadata'),('meta','value')]:
        for row in h.db.execute(f'SELECT {column} FROM {table}'):
            try:
                value = json.loads(row[0])
            except (ValueError, TypeError):
                continue
            roots.update(references(value))
            # Cached correspondence uses this public receipt field.
            if isinstance(value, dict) and value.get('CacheEvidenceId'):
                roots.add(value['CacheEvidenceId'])
    activity = {r[0]:r[1] for r in h.db.execute("SELECT id,at FROM retention_activity WHERE kind='evidence'")}
    owners = {r[0] for r in h.db.execute('SELECT id FROM cases')}
    deleted = 0
    for row in h.db.execute('SELECT id,metadata FROM evidence').fetchall():
        meta = json.loads(row['metadata'])
        owner = meta.get('owner_case_id', meta.get('case_id'))
        if row['id'] in roots or owner in owners or activity.get(row['id'], '') >= cutoff:
            continue
        h.db.execute('DELETE FROM evidence WHERE id=?', (row['id'],))
        h.db.execute("DELETE FROM retention_activity WHERE kind='evidence' AND id=?", (row['id'],))
        deleted += 1
    h.db.execute("DELETE FROM retention_activity WHERE kind='evidence' AND id NOT IN (SELECT id FROM evidence)")
    h.db.execute('DELETE FROM roundup WHERE reported_run IS NOT NULL')
    for row in h.db.execute("SELECT id,details FROM improvements WHERE status IN ('implemented','rejected')").fetchall():
        item = json.loads(row['details'])
        marker = {key:item[key] for key in ('id','occurrences','last_seen','review') if key in item}
        h.db.execute('UPDATE improvements SET details=? WHERE id=?', (dumps(marker),row['id']))
    for row in h.db.execute('SELECT path FROM maintenance_files').fetchall():
        from pathlib import Path
        if not Path(row[0]).exists():
            h.db.execute('DELETE FROM maintenance_files WHERE path=?', (row[0],))
    return deleted
