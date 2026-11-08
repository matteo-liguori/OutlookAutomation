import json
from datetime import datetime, timedelta, timezone
from .model import BUDGET, OPEN, now, stamp, dumps, digest


def cursor_time(cursor):
    """Anchor calculated fields across pages without retaining query snapshots."""
    parts=cursor.split(':')
    if len(parts)!=4 or parts[0]!='live':raise ValueError('Invalid live cursor; restart the query')
    at=datetime.fromtimestamp(int(parts[3]),timezone.utc)
    age=(datetime.fromisoformat(now())-at).total_seconds()
    if not -5<=age<=86400:raise ValueError('Live cursor expired; restart the query')
    return stamp(at.isoformat())


class Views:
    def current_items(self, briefing=False, at=None):
        with self.read():
            return self._current_items(briefing, at)

    def _current_items(self, briefing=False, at=None):
        at = stamp(at or now())
        from .followups import reminder_due, due_at
        items = []
        if briefing:
            from .live import items as health_items
            items.extend({'type':'source_health',**s} for s in health_items(self,at) if s['status']!='current')
            from .approvals import items as approval_items
            items.extend(approval_items(self, at=at, due_only=True))
            pending=self.db.execute('SELECT count(*) FROM pending_work').fetchone()[0]
            if pending:items.append({'type':'pending_work','count':pending,'instruction':'Review carried-forward candidates; current run candidates includes them'})
        if briefing:
            rotation = self.db.execute("SELECT value FROM meta WHERE key='backup_rotation'").fetchone()
            if rotation and json.loads(rotation[0]).get('failures'):
                items.append({'backup_rotation_warning': json.loads(rotation[0])})
            cleanup = self.db.execute("SELECT value FROM meta WHERE key='thread_cache_maintenance'").fetchone()
            if cleanup and json.loads(cleanup[0]).get('failures'):
                items.append({'thread_cache_warning':json.loads(cleanup[0])})
            warning = self.db.execute(
                "SELECT value FROM meta WHERE key='maintenance_warning'"
            ).fetchone()
            if warning and json.loads(warning[0]).get("reliability_affected"):
                items.append({"maintenance_warning": json.loads(warning[0])})
            from .filing import enabled, status

            if enabled(self):
                items.extend(status(self, at))
        reported_cases = {
            row["case_id"]: row["revision"]
            for row in self.db.execute("SELECT case_id,revision FROM reported_cases")
        }
        for c in self.all_cases(open_only=briefing):
            if not briefing:
                items.append({k: c[k] for k in ("id", "title", "position", "status", "revision")})
                continue
            for a in c["actions"]:
                snoozed = (
                    a.get("snooze_until")
                    and a["snooze_until"] > at
                    and not a.get("snooze_override")
                )
                if (
                    a["status"] in OPEN
                    and not snoozed
                    and (a["status"] == "needs-user" or reminder_due(a, at) or (a.get("next_review") or at) <= at)
                ):
                    items.append(
                        {
                            "case": c["id"],
                            "title": c["title"],
                            "action": a["id"],
                            "status": a["status"],
                            "owner": a["owner"],
                            "next_step": a["next_step"],
                            "next_review": a["next_review"],
                            "deadline": a.get("deadline"),
                            "urgency": a.get("review_policy", {}).get("urgency", "normal"),
                            "reason": "needs-user" if a["status"] == "needs-user" else "no-reply-follow-up" if reminder_due(a, at) else "review-due",
                            "follow_up": a.get("follow_up"),
                            "follow_up_due_at": due_at(a),
                            "follow_up_due": reminder_due(a, at),
                        }
                    )
            reported = reported_cases.get(c["id"])
            reported = max(reported or 0, c.get('last_quiet_revision', 0)) or None
            unsnoozed = any(
                a["status"] in OPEN
                and not (
                    a.get("snooze_until")
                    and a["snooze_until"] > at
                    and not a.get("snooze_override")
                )
                for a in c["actions"]
            )
            newly_unreported = (
                reported is None
                and c.get("created_at")
                and unsnoozed
                and not any(i.get("case") == c["id"] and i.get("action") for i in items)
            )
            if (reported is not None and reported != c["revision"]) or newly_unreported:
                items.append(
                    {
                        "case": c["id"],
                        "title": c["title"],
                        "position": c["position"],
                        "reason": "changed-since-report",
                    }
                )
        if briefing:
            for r in self.db.execute(
                "SELECT id,case_id,details FROM issues WHERE resolved=0 ORDER BY id"
            ):
                items.append({"issue": r[0], "case": r[1], **json.loads(r[2])})
            run = self.db.execute(
                "SELECT id,status FROM runs ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            if run and run["status"] != "complete":
                verification = self.run_status(run["id"])
                if verification.get("technical_detail_expired"):
                    verification = {
                        key: verification[key]
                        for key in (
                            "status", "fully_verified", "failures",
                            "pending_candidates", "technical_detail_expired",
                        )
                        if key in verification
                    }
                items.append({"run": run["id"], "verification": verification})
        if briefing:
            priority = {"immediate": 0, "high": 1, "normal": 2, "low": 3}
            items.sort(
                key=lambda i: (
                    (
                        0
                        if i.get("type") == "filing_checkpoint"
                        else (
                            1
                            if i.get("reason") == "needs-user"
                            else 2 if i.get("reason") == "changed-since-report" else 3
                        )
                    ),
                    priority.get(i.get("urgency", "normal"), 2),
                    i.get("next_review") or "",
                    i.get("case", ""),
                )
            )
        return items

    def page(self, kind, items=None, cursor=None, limit=50):
        cursor=cursor or getattr(self,'_page_cursor',None)
        if items is None:raise ValueError('Recompute current query results when following a live cursor')
        token=digest([kind,items])[:24]
        offset=0
        query_at=getattr(self,'_query_at',None) or now()
        if cursor:
            query_at=cursor_time(cursor)
            prefix,expected,offset,_=cursor.split(':');offset=int(offset)
            if prefix!='live' or expected!=token:raise ValueError('Query changed; restart without the cursor')
        chunks=[]
        for item in items:
            text=dumps(item)
            if len(text)>8000:
                chunks.extend({'record_id':item.get('id'),'part':i//5000+1,'parts':(len(text)+4999)//5000,'json_chunk':text[i:i+5000]} for i in range(0,len(text),5000))
            else:chunks.append(item)
        if not 0<=offset<=len(chunks):raise ValueError('Invalid page offset')
        out={'items':[],'total':len(chunks),'offset':offset,'next_cursor':None}
        for item in chunks[offset:offset+limit]:
            if len(json.dumps(dict(out,items=out['items']+[item]),ensure_ascii=False,indent=2))+100>BUDGET:break
            out['items'].append(item)
        end=offset+len(out['items'])
        if not out['items'] and offset<len(chunks):raise ValueError('Record cannot fit output budget')
        epoch=int(datetime.fromisoformat(query_at).timestamp())
        out['next_cursor']=f'live:{token}:{end}:{epoch}' if end<len(chunks) else None
        return out
