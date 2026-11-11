from .configuration import resolve
"""One replaceable thread card; actions remain canonical scheduled harness rows."""

import json
from pathlib import Path
from urllib.parse import quote,unquote
from .model import dumps, now, stamp, OPEN,digest


def watch_items(h):
    """Recognition-only index: do not load cards/evidence or schedule reviews."""
    return [dict(type='thread_watch',case=r['id'],title=r['title'],summary=r['summary'] or r['title'],reference_only=True)
            for r in h.db.execute("SELECT id,title,json_extract(details,'$.purpose') AS summary FROM cases WHERE status='open' ORDER BY id")]


def upkeep_prompt(items,at,case_ids=None,trigger='due-harness-action'):
    selected=sorted(set(case_ids) if case_ids is not None else {i['case'] for i in items if i.get('action') and (i.get('follow_up_due') or (i.get('next_review') and i['next_review']<=at))})
    if not selected:return []
    from .guidance import guide
    return [dict(type='card_upkeep_prompt',case_ids=selected,trigger=trigger,prompt=guide('card-upkeep')['instruction'])]


def evidence_index(h, ids, previous=()):
    entries={e['id']:e for e in previous}
    for eid in dict.fromkeys(ids):
        if eid in entries:continue
        row=h.db.execute('SELECT metadata FROM evidence WHERE id=?',(eid,)).fetchone()
        if not row:raise ValueError('Unknown card evidence '+eid)
        m=json.loads(row[0])
        if m.get('kind') in {'collector-page','collector-index','freshness-probe','operational-reference'}:continue
        at=m.get('received') or m.get('captured_at') or m.get('at')
        try:at=stamp(at) if at else None
        except (ValueError,TypeError):at=None
        entries[eid]={'id':eid,'at':at,'label':str(m.get('label') or m.get('kind') or m.get('source') or 'Source')[:180]}
    return sorted(entries.values(),key=lambda e:(e.get('at') or '9999',e['id']))


def prepare(h, case, prior, at):
    """Maintain one current row, without creating a diary or scheduling new actions."""
    prior=prior or {}
    if 'customisations' not in case and 'customisations' in prior:case['customisations']=prior['customisations']
    if case.get('customisations'):resolve(card=case)
    case.pop('user_note_instruction', None)
    # Only the narrow user-input/acknowledgement operations own this inbox.
    # Agent replacements (including stale copies) cannot clear or invent notes.
    if 'user_notes' in prior:case['user_notes'] = prior['user_notes']
    else:case.pop('user_notes', None)
    if case.get('status') == 'closed' and case.get('user_notes'):
        raise ValueError('Handle and acknowledge pending user notes before closing or merging this card')
    # Operational acknowledgements are written by run finalization, not card input.
    for key in ('last_quiet_revision','last_quiet_at'):
        if key in prior:case[key]=prior[key]
        else:case.pop(key,None)
    case.setdefault('status','open')
    interval = case.get('review_interval_days', prior.get('review_interval_days', resolve().get('reviews.card_days')))
    if type(interval) is not int or not 1 <= interval <= 90:
        raise ValueError('Card review interval must be 1..90 days')
    case['review_interval_days'] = interval
    explicit_context='context' in case
    case.setdefault('purpose',prior.get('purpose') or case['title'])
    case.setdefault('context',prior.get('context') or case['position'])
    h.limited(case,'purpose',600);h.limited(case,'context',6000)
    if len(case['purpose'].splitlines())>3:raise ValueError('Purpose/watch brief must have at most three lines')
    meaningful=not prior or h.report_signature(dict(prior,actions=prior.get('actions',[])))!=h.report_signature(dict(case,actions=case.get('actions',[]),decisions=case.get('decisions',[])))
    reviewed=case.get('card_reviewed_at')
    if reviewed:
        reviewed=stamp(reviewed)
        if reviewed>stamp(now()):raise ValueError('Card review cannot be in the future')
    if not prior or (explicit_context and meaningful) or case['context']!=prior.get('context'):
        reviewed=at
    case['card_reviewed_at']=reviewed or prior.get('card_reviewed_at')
    ids=list(prior.get('evidence_ids',[]))+list(case.get('evidence_ids',[]))
    for a in case.get('actions',[]):
        ids+=a.get('completion_evidence',[])+a.get('evidence_ids',[])
    for d in case.get('decisions',[]):ids+=d.get('evidence_ids',[])
    case['evidence_index']=evidence_index(h,ids,prior.get('evidence_index',[]))
    case['evidence_ids']=[e['id'] for e in case['evidence_index']]
    for key in ('development_count','developments_since_review','history','versions'):
        case.pop(key,None)


def show(h,cid):
    c=h.case(cid)
    if c.get('user_notes'):
        from .card_input import NOTE_INSTRUCTION
        c['user_note_instruction'] = NOTE_INSTRUCTION
    c.pop('evidence_ids',None)
    marker=h.db.execute('SELECT revision FROM reported_cases WHERE case_id=?',(cid,)).fetchone()
    c['last_reported_revision']=marker[0] if marker else None
    from .approvals import items as approval_items
    c['approvals']=[{'id':a['id'],'kind':a['kind'],'review_due':a['review_due']} for a in approval_items(h) if a['case_id']==cid]
    from .extensions import project
    return project(h,c)


def page(h,cid,cursor=None):
    """Page a live card by content token; never save a duplicate card in cursor storage."""
    c=show(h,cid);text=dumps(c);token=digest(text)[:16];offset=0
    if cursor:
        prefix,key,expected,offset=cursor.split(':');offset=int(offset)
        if prefix!='card' or unquote(key)!=cid or expected!=token:raise ValueError('Card changed; restart case show without the cursor')
    if len(text)<=8000:
        if offset:raise ValueError('Invalid card cursor')
        return dict(items=[c],total=1,offset=0,next_cursor=None)
    parts=[text[i:i+4500] for i in range(0,len(text),4500)]
    if not 0<=offset<len(parts):raise ValueError('Invalid card cursor')
    return dict(items=[dict(record_id=cid,part=offset+1,parts=len(parts),json_chunk=parts[offset])],total=len(parts),offset=offset,next_cursor=f'card:{quote(cid,safe="")}:{token}:{offset+1}' if offset+1<len(parts) else None)


def render(h,cid):
    import html
    from .threadcache import folder,checked_path
    c=show(h,cid);esc=lambda x:html.escape(str(x));pretty=lambda x:esc(json.dumps(x,ensure_ascii=False,indent=2))
    active=[a for a in c['actions'] if a['status'] in OPEN]
    content='<!doctype html><html><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Thread card</title><style>body{font:16px/1.55 system-ui;background:#f3f5f7;color:#192839;margin:0}main{max-width:1000px;margin:30px auto;background:white;padding:30px;border-radius:12px}h1{font-size:28px}h2{font-size:20px;margin-top:28px}.context{white-space:pre-wrap}article{border:1px solid #dce4eb;border-radius:8px;padding:16px;margin:12px 0}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.5 Consolas,monospace}summary{cursor:pointer;font-weight:600;padding:10px 0}li{overflow-wrap:anywhere;margin:8px 0}.muted{color:#5e6c77}</style><main>'
    content+=f'<h1>{esc(c["title"])}</h1><p class="muted">{esc(cid)} · {esc(c["status"])} · revision {c["revision"]} · card reviewed {esc(c.get("card_reviewed_at") or "not yet recorded")}</p><h2>Purpose</h2><p>{esc(c.get("purpose",c["title"]))}</p><h2>Relevant context</h2><div class="context">{esc(c.get("context",c["position"]))}</div><h2>Current position</h2><p>{esc(c["position"])}</p><h2>Harness actions ({len(active)})</h2>'
    for a in active:
        content+=f'<article><strong>{esc(a["next_step"])}</strong><p>{esc(a["status"])} · {esc(a["owner"])} · next review {esc(a.get("next_review"))}</p><details><summary>Scheduling, follow-up and completion fields</summary><pre>{pretty(a)}</pre></details></article>'
    if c['decisions'] or c['approvals']:
        content+='<details><summary>Active decisions and pending approvals</summary>'
        if c['decisions']:content+=f'<pre>{pretty(c["decisions"])}</pre>'
        if c['approvals']:content+=f'<pre>{pretty(c["approvals"])}</pre>'
        content+='</details>'
    content+=f'<details><summary>Ordered evidence index ({len(c.get("evidence_index",[]))})</summary><ol>'
    content+=''.join(f'<li>{esc(e.get("at") or "Date unknown")} · {esc(e["label"])}<br><code>{esc(e["id"])}</code></li>' for e in c.get('evidence_index',[]))
    content+='</ol></details><p class="muted">View of the single current SQLite card. Regenerate with case render; no previous card versions are retained.</p></main></html>'
    target=folder(h,cid)/'thread-card.html';checked_path(h.path.parent,target);target.parent.mkdir(parents=True,exist_ok=True)
    temporary=target.with_suffix('.html.tmp');temporary.write_text(content,encoding='utf-8');temporary.replace(target)
    return {'path':str(target),'id':cid,'revision':c['revision']}
