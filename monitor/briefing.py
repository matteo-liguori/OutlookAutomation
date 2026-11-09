"""Task-focused current records, including relevant work not yet due."""

import re
from .model import OPEN


def compact(h, at):
    """Recognition, due work and a command reminder; no card bodies or procedures."""
    from .cards import watch_items, upkeep_prompt
    from .guidance import context_prompts
    from .reviews import active_due, due as reviews_due
    from .improvements import review_prompt
    raw = h.current_items(briefing=True, at=at)
    work_ids = {a['id'] for c in h.all_cases(open_only=True) for a in c['actions'] if active_due(a, at)}
    due = [i for i in raw if i.get('action') in work_ids]
    from .card_input import pending
    attention = pending(h)
    for prompt in context_prompts(raw):
        attention.append(dict(type=prompt['type'], topic=prompt['topic'],
                              instruction='Use help '+prompt['topic']+'.'))
    for prompt in upkeep_prompt(due, at):
        attention.append(dict(type=prompt['type'],case_ids=prompt['case_ids'],trigger=prompt['trigger'],
                              instruction='Inspect due cards with case show; help card-upkeep for review instructions. Record the whole-card review with review complete or action review.'))
    for i in due:
        attention.append(dict(type='due_action', **{k:i.get(k) for k in
                         ('case','action','status','owner','next_step','next_review','deadline')},
                         due_at=i.get('follow_up_due_at') or i.get('next_review')))
    for i in raw:
        kind=i.get('type')
        if kind == 'source_health':
            attention.append({k:i[k] for k in ('type','source','status','last_verified_at','last_error',
                                               'coverage_gap_from','coverage_gap_through') if i.get(k) is not None})
        elif kind == 'approval_review' and i.get('review_due'):
            attention.append({k:i[k] for k in ('type','id','title','next_review_at','question') if k in i})
        elif kind == 'pending_work':
            attention.append(dict(type=kind,count=i['count'],instruction='Review run candidates.'))
        elif i.get('run'):
            v=i['verification']
            attention.append(dict(type='incomplete_pass',run=i['run'],status=v.get('status'),
                                  instruction='Use run status '+i['run']+' for unfinished coverage.'))
        elif i.get('issue'):
            attention.append(dict(type='open_issue',id=i['issue'],case=i.get('case'),
                                  instruction='Use issue show '+i['issue']+'.'))
    attention += review_prompt(h,at)
    reviews = reviews_due(h, at)
    return ([dict(type='briefing_section', title='Open threads')] + watch_items(h) +
            [dict(type='briefing_section', title='Due actions and alerts')] + attention +
            [dict(type='briefing_section', title='Periodic reviews')] + reviews +
            ([dict(type='review_hint', instruction='Use review next; record keep/close, then repeat until complete.')] if reviews else []) +
            [dict(type='briefing_section', title='Commands'), dict(type='command_hint', instruction=
        'Outlook: outlook status; help outlook. Inspect: case show <id>, thread messages <id>. Passes: run start/candidates/status, report data. '
        'Reviews: review next/complete. Records: case/action/approval/context/evidence. Other: calendar/filing/health/schedule. '
        'Use help <topic or command> for details; follow paging cursors.')])


def relevant(h, task="", case_ids=(), message=None):
    from .followups import reminder_due, due_at
    at=getattr(h,'_query_at',None)
    selected = set(case_ids)
    reasons = {cid: "explicit case" for cid in case_ids}
    words = set(re.findall(r"[\w@.-]{3,}", task.lower())) - {
        "the",
        "and",
        "for",
        "with",
        "please",
        "help",
        "check",
        "this",
        "that",
        "email",
        "mail",
    }
    if words:
        for row in h.db.execute("SELECT id,title,position,details,status FROM cases"):
            if row["status"] == "closed":
                continue
            import json

            details = json.loads(row["details"])
            text = " ".join(
                [row["id"], row["title"], row["position"], details.get('purpose',''), details.get('context',''), " ".join(details.get("aliases", []))]
            ).lower()
            tokens = set(re.findall(r"[\w@.-]{3,}", text))
            if words & tokens:
                selected.add(row["id"])
                reasons[row["id"]] = "task terms: " + ", ".join(sorted(words & tokens))
    if message:
        for match in h.suggest_cases(message):
            selected.add(match["case_id"])
            reasons[match["case_id"]] = "; ".join(match["reasons"])
    items = []
    for cid in sorted(selected):
        c = h.case(cid)
        items.append(
            {
                "case": cid,
                "title": c["title"],
                "position": c["position"],
                "status": c["status"],
                "revision": c["revision"],
                "reason": "task-relevant",
                "matched_by": reasons[cid],
                "merged_into": c.get("merged_into"),
                "split_from": c.get("split_from"),
            }
        )
        for a in c["actions"]:
            if a["status"] in OPEN:
                items.append(
                    {
                        "case": cid,
                        "title": c["title"],
                        "action": a["id"],
                        "status": a["status"],
                        "owner": a["owner"],
                        "next_step": a["next_step"],
                        "next_review": a.get("next_review"),
                        "deadline": a.get("deadline"),
                        "urgency": a.get("review_policy", {}).get("urgency", "normal"),
                        "reason": "task-relevant",
                        "snooze_until": a.get("snooze_until"),
                        "follow_up": a.get("follow_up"),
                        "follow_up_due_at": due_at(a),
                        "follow_up_due": reminder_due(a,at),
                    }
                )
        # A merge tombstone routes retrieval without reading any historical body.
        target = c.get("merged_into")
        if target and target not in selected:
            items.extend(relevant(h, case_ids=[target]))
    from .approvals import items as approval_items
    items.extend(a for a in approval_items(h, at=at) if a.get('case_id') in selected)
    return items
