"""Current source health and bounded pass working data, never an access-history log."""

import json
from datetime import datetime,timedelta
from .model import dumps,now,stamp,MAIL,CAL

SCHEMA='''
CREATE TABLE IF NOT EXISTS source_health(source TEXT PRIMARY KEY,details TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pending_work(id TEXT PRIMARY KEY,source TEXT NOT NULL,details TEXT NOT NULL);
'''
OPERATIONAL={'collector-page','collector-index','freshness-probe','google-connector-export','calendar-collection','refresh-result','outlook-refresh','source-check','case-snapshot'}
OPERATIONAL.update({'calendar-query','calendar-read','calendar-read-failure','calendar-session','calendar-ui-read','calendar-window','calendar-caldav-read','caldav-read','calendar-browser-read','calendar-browser-unavailable','refresh','refresh-attempt','refresh-failure','refresh-and-outlook-failure','freshness-recheck','send-receive','session','session-preflight','outlook-session-check','record-validation','records-validation','doctor','doctor-validation','title-readback','title-verification','thread-title-readback','collection-failure','mail-collection-failure'})
OPERATIONAL.update({'handoff-report','tomorrow-handover'})
OPERATIONAL_SOURCES={'monitor-report','monitor-report-delivery','monitor-handoff','evening-report','evening-report-final-metrics'}


def initialize(h):
    h.db.executescript(SCHEMA)
    h.db.commit()


def record(h,source,payload,at,verified=None):
    """A failed attempt cannot erase the last successful watermark or calendar window."""
    at=stamp(payload.get('completed_at') or payload.get('checked_at') or payload.get('started_at') or at or now())
    row=h.db.execute('SELECT details FROM source_health WHERE source=?',(source,)).fetchone()
    state=json.loads(row[0]) if row else {}
    if state.get('last_attempt_at','')>at:return
    current=payload.get('status')=='current' and payload.get('complete') is True
    success=current if verified is None else verified
    state.update(source=source,last_attempt_at=at,status='current' if success else 'unverified',last_error=payload.get('errors') or payload.get('error') or (None if success else 'Access/freshness/coverage not verified'))
    if current:state['last_read_at']=at
    newest=payload.get('newest') or payload.get('latest_received')
    if newest:state['newest_received_at']=max(stamp(newest),state.get('newest_received_at') or '')
    if success:
        start=payload.get('since') or (payload.get('days') or [None])[0]
        end=payload.get('until') or (payload.get('days') or [None])[-1]
        gap=state.get('coverage_gap_from')
        through=state.get('coverage_gap_through') or gap
        contiguous=bool(start and end and gap and stamp(start)<=stamp(gap)<=stamp(end))
        recovered=not gap or source not in MAIL or (contiguous and stamp(end)>=stamp(through))
        state.update(last_verified_at=at,coverage_from=start,coverage_through=end,failure_since=None)
        if recovered:state.update(coverage_gap_from=None,coverage_gap_through=None)
        elif contiguous:state['coverage_gap_from']=stamp(end)
        state['coverage_status']='complete' if recovered else 'gap'
    else:
        state['failure_since']=state.get('failure_since') or at
        gap=stamp(payload.get('since') or at)
        if state.get('coverage_through'):gap=max(gap,stamp(state['coverage_through']))
        state['coverage_gap_from']=min(gap,stamp(state.get('coverage_gap_from') or gap))
        end=stamp(payload.get('until') or at)
        state['coverage_gap_through']=max(end,state.get('coverage_gap_through') or end)
    h.db.execute('INSERT INTO source_health VALUES(?,?) ON CONFLICT(source) DO UPDATE SET details=excluded.details',(source,dumps(state)))


def items(h,at=None):
    states=[json.loads(r[0]) for r in h.db.execute('SELECT details FROM source_health ORDER BY source')]
    cutoff=stamp((datetime.fromisoformat(at or now())-timedelta(days=1)).isoformat())
    for s in states:
        if s['status']=='current' and s.get('last_verified_at','')<cutoff:s.update(status='unverified',last_error='Last verified check is more than 24 hours old')
        if s.get('coverage_gap_from') and s['status']=='current':s.update(status='partial',last_error='Current access verified; older coverage gap still requires catch-up')
    return states


def _references(value):
    if isinstance(value,dict):
        for key,child in value.items():
            if key.endswith('evidence_ids') and isinstance(child,list):yield from child
            else:yield from _references(child)
    elif isinstance(value,list):
        for child in value:yield from _references(child)


def _retire_evidence(h,ids,active,at=None):
    removed=0
    cutoff=stamp((datetime.fromisoformat(at or now())-timedelta(days=1)).isoformat())
    activity={r[0]:r[1] for r in h.db.execute("SELECT id,at FROM retention_activity WHERE kind='evidence'")}
    for row in h.db.execute('SELECT id,metadata,body FROM evidence').fetchall():
        meta=json.loads(row['metadata'])
        if row['id'] in active:continue
        if row['id'] not in ids and activity.get(row['id'],at or now())>=cutoff:continue
        try:data=json.loads(row['body'])
        except (ValueError,TypeError):data=None
        snapshot=isinstance(data,dict) and isinstance(data.get('actions'),list) and 'position' in data
        probes=data if isinstance(data,list) else [data]
        raw_health=bool(probes) and all(isinstance(p,dict) and (
            {'Store','CheckedAt','LatestReceived','Status'}.issubset(p)
            or {'schema','success','groups','wait_seconds'}.issubset(p)
            or (p.get('source') in CAL and 'events' in p and 'complete' in p)
            or (isinstance(p.get('events'),list) and any(k in p for k in ('calendarCount','window','next_page_token','filter','days','method')))
            or {'ComAvailable','FailureKind','Session'}.issubset(p)
            or {'checked_at','com_available','session_status'}.issubset(p)
            or {'completed_at','output','success','wait_seconds'}.issubset(p)
            or {'mode','ok','problems'}.issubset(p)
            or {'Complete','Count','Items','SnapshotId','StoreId'}.issubset(p)
            or {'items','next_cursor','offset','total'}.issubset(p)
            or {'schema','sources','timezone','start','end'}.issubset(p)
        ) for p in probes)
        if meta.get('kind') not in OPERATIONAL and meta.get('source') not in OPERATIONAL_SOURCES and not str(meta.get('source','')).endswith('LIVE_VALIDATION_REPORT.md') and not snapshot and not raw_health:continue
        # IDs can be cited by current actions. Preserve only the identifier, not an old health log.
        retained = {k:meta[k] for k in ('owner_case_id', 'case_id', 'scope') if k in meta}
        h.db.execute('UPDATE evidence SET metadata=?,body=? WHERE id=?',(dumps(dict(retained,kind='operational-reference')),dumps({'working_capture_released':True}),row['id']))
        removed+=1
    return removed


def _carry_candidates(h,rid):
    """Verify each unresolved candidate has an identical live-work copy before clearing a pass."""
    from .messages import changed
    for row in h.db.execute('SELECT details FROM candidates WHERE run_id=?',(rid,)).fetchall():
        c=h.decode_message(row[0]);latest=h.db.execute('SELECT details FROM message_reviews WHERE id=?',(c['id'],)).fetchone()
        if not latest and (c.get('internet_message_id') or (c.get('entry_id') and c.get('store_id'))):
            condition="json_extract(v.details,'$.internet_message_id')=?" if c.get('internet_message_id') else "json_extract(v.details,'$.entry_id')=? AND json_extract(v.details,'$.store_id')=?"
            values=[c['internet_message_id']] if c.get('internet_message_id') else [c['entry_id'],c['store_id']]
            latest=h.db.execute("SELECT m.details FROM message_reviews m JOIN message_versions v ON v.id=json_extract(m.details,'$.message_version') WHERE m.source=? AND "+condition+" ORDER BY json_extract(m.details,'$._run_started_at') DESC LIMIT 1",[c['source'],*values]).fetchone()
        already=latest and not changed(h.decode_message(latest[0]),c) and h.candidate_complete(h.decode_message(latest[0]))
        if not h.candidate_complete(c) and not already:
            h.db.execute('INSERT INTO pending_work VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET details=excluded.details',(c['id'],c['source'],h.encode_message(c)))
            saved=h.db.execute('SELECT details FROM pending_work WHERE id=?',(c['id'],)).fetchone()
            if h.decode_message(saved[0])!=c:raise ValueError('Pending-work transfer failed; pass data retained')
        else:h.db.execute('DELETE FROM pending_work WHERE id=?',(c['id'],))


def expire_finished_working_data(h,cutoff):
    """Keep the current run receipt; only finalized, inactive working payloads may expire."""
    activity={r[0]:r[1] for r in h.db.execute("SELECT id,at FROM retention_activity WHERE kind='run'")}
    for row in h.db.execute("SELECT id,status,started_at,details FROM runs WHERE status IN ('complete','partial')").fetchall():
        d=json.loads(row['details'])
        if d.get('working_expired') or activity.get(row['id'],row['started_at'])>=cutoff:continue
        _carry_candidates(h,row['id'])
        verification=d.get('final_verification') or d.get('last_verification') or {}
        summary={k:verification[k] for k in ('status','failures','fully_verified') if k in verification}
        h.db.execute('DELETE FROM candidates WHERE run_id=?',(row['id'],))
        h.db.execute('DELETE FROM sources WHERE run_id=?',(row['id'],))
        h.db.execute('UPDATE runs SET details=? WHERE id=?',(dumps({'working_expired':True,'last_verification':summary}),row['id']))


def prune(h,keep=None,at=None):
    """Retire superseded passes after carrying unresolved candidates into current work."""
    if keep is None:
        row=h.db.execute('SELECT id FROM runs ORDER BY started_at DESC,id DESC LIMIT 1').fetchone()
        keep=row[0] if row else None
    at=at or now()
    cutoff=stamp((datetime.fromisoformat(at)-timedelta(days=1)).isoformat())
    old=[r['id'] for r in h.db.execute('SELECT id,status,started_at FROM runs ORDER BY started_at,id') if r['id']!=keep and r['status']!='running']
    released=set()
    for rid in old:
        _carry_candidates(h,rid)
        for row in h.db.execute('SELECT details FROM sources WHERE run_id=?',(rid,)):released.update(_references(json.loads(row[0])))
        for table,key in [('sources','run_id'),('candidates','run_id'),('metrics','run_id'),('reflections','run_id')]:h.db.execute(f'DELETE FROM {table} WHERE {key}=?',(rid,))
        h.db.execute('DELETE FROM runs WHERE id=?',(rid,));h.db.execute("DELETE FROM retention_activity WHERE kind='run' AND id=?",(rid,))
    expire_finished_working_data(h,cutoff)
    # Source metadata has one current version per reviewed message, only for the seven-day filing window.
    week=stamp((datetime.fromisoformat(at)-timedelta(days=7)).isoformat())
    for row in h.db.execute('SELECT id,details FROM message_reviews').fetchall():
        c=h.decode_message(row['details'])
        if c.get('received','9999')<week and not h.db.execute('SELECT 1 FROM pending_work WHERE id=?',(row['id'],)).fetchone() and not h.db.execute('SELECT 1 FROM candidates WHERE id=?',(row['id'],)).fetchone():h.db.execute('DELETE FROM message_reviews WHERE id=?',(row['id'],))
    active=set()
    for row in h.db.execute('SELECT details FROM sources'):active.update(_references(json.loads(row[0])))
    for row in h.db.execute('SELECT details FROM runs'):active.update(_references(json.loads(row[0])))
    for row in h.db.execute('SELECT details FROM pending_work'):active.update(_references(h.decode_message(row[0])))
    released_count=_retire_evidence(h,released,active,at)
    from .maintenance import references,database_values
    versions={r[0] for r in h.db.execute('SELECT id FROM message_versions')}
    used=references(database_values(h),versions)
    used.update(references((json.loads(r[0]) for r in h.db.execute('SELECT details FROM pending_work')),versions))
    h.db.executemany('DELETE FROM message_versions WHERE id=?',[(v,) for v in versions-used])
    from .evidence import prune as prune_evidence
    removed_evidence = prune_evidence(h, at)
    return {'removed_runs':len(old),'released_working_evidence':released_count,'removed_unclaimed_evidence':removed_evidence}
