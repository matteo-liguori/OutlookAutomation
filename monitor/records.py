import json
from .model import OPEN, now, stamp, dumps, digest
from .scheduling import next_review


def validate_match_keys(keys):
    if not isinstance(keys, dict) or set(keys) - {'sources', 'conversation_ids', 'references'}:
        raise ValueError('match_keys must be an object with sources, conversation_ids and/or references')
    for name, values in keys.items():
        if not isinstance(values, list) or any(not isinstance(v, str) or not v.strip() for v in values):
            raise ValueError('match_keys.' + name + ' must be a list of nonempty strings')
    return keys


class CaseRecords:
    def all_cases(self, open_only=False):
        """Load current rows in three queries, without historical/evidence bodies."""
        where = " WHERE status='open'" if open_only else ""
        cases = {}
        for row in self.db.execute("SELECT * FROM cases" + where + " ORDER BY id"):
            item = json.loads(row["details"])
            item.update(
                {
                    key: row[key]
                    for key in ("id", "title", "position", "status", "revision", "updated_at")
                }
            )
            item.update(actions=[], decisions=[])
            if 'evidence_index' in item:item['evidence_ids']=[e['id'] for e in item['evidence_index']]
            cases[row["id"]] = item
        for table, field in [("actions", "actions"), ("decisions", "decisions")]:
            for row in self.db.execute("SELECT case_id,details FROM " + table + " ORDER BY id"):
                if row["case_id"] in cases:
                    cases[row["case_id"]][field].append(json.loads(row["details"]))
        return list(cases.values())

    def _save_case(self, case, cid, revision, at):
        """Persist validated case metadata inside its caller's transaction."""
        if not self.db.in_transaction:
            raise ValueError("Case persistence requires a transaction")
        if 'match_keys' in case:validate_match_keys(case['match_keys'])
        from .threadcache import lifecycle

        old = self.db.execute("SELECT details,status FROM cases WHERE id=?", (cid,)).fetchone()
        prior = dict(json.loads(old[0]), status=old[1]) if old else None
        from .evidence import scope
        original_actions, original_decisions = case.get('actions'), case.get('decisions')
        case = dict(case, id=cid)
        lifecycle(self, case, prior, at)
        from .cards import prepare
        prior_card = scope(self, cid, self.case(cid)) if old else None
        case = scope(self, cid, case)
        prepare(self,case,prior_card,at)
        if original_actions is not None:
            original_actions[:] = case.get('actions', [])
        if original_decisions is not None:
            original_decisions[:] = case.get('decisions', [])
        self.limited(case, "title", 180)
        self.limited(case, "position", 1200)
        details = {
            key: value
            for key, value in case.items()
            if key
            not in {
                "id",
                "title",
                "position",
                "status",
                "revision",
                "updated_at",
                "actions",
                "decisions",
            }
        }
        details.pop('evidence_ids',None)
        self.db.execute(
            "INSERT INTO cases VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title,position=excluded.position,status=excluded.status,revision=excluded.revision,updated_at=excluded.updated_at,details=excluded.details",
            (
                cid,
                case["title"],
                case["position"],
                case.get("status", "open"),
                revision,
                at,
                dumps(details),
            ),
        )
        from .approvals import lifecycle as approval_lifecycle
        approval_lifecycle(self, at)

    def case(self, cid):
        with self.read():
            return self._case(cid)

    def _case(self, cid):
        row = self.db.execute("SELECT * FROM cases WHERE id=?", (cid,)).fetchone()
        if not row:
            raise ValueError(f"Unknown case {cid}")
        data = json.loads(row["details"])
        data.update(
            id=cid,
            title=row["title"],
            position=row["position"],
            status=row["status"],
            revision=row["revision"],
            updated_at=row["updated_at"],
        )
        data["actions"] = [
            json.loads(r[0])
            for r in self.db.execute(
                "SELECT details FROM actions WHERE case_id=? ORDER BY id", (cid,)
            )
        ]
        data["decisions"] = [
            json.loads(r[0])
            for r in self.db.execute(
                "SELECT details FROM decisions WHERE case_id=? ORDER BY id", (cid,)
            )
        ]
        if 'evidence_index' in data:data['evidence_ids']=[e['id'] for e in data['evidence_index']]
        return data

    @staticmethod
    def limited(item, field, cap, required=True):
        value = item.get(field, "")
        if not isinstance(value, str) or len(value) > cap or (required and not value.strip()):
            raise ValueError(f"{field}: required text, maximum {cap} characters")

    def update(self, payload):
        from .configuration import resolve,snapshot
        from .extensions import effective
        cid=payload['id']
        prior=self.case(cid) if self.db.execute('SELECT 1 FROM cases WHERE id=?',(cid,)).fetchone() else {}
        card=dict(prior,**payload.get('case',{}));card['id']=cid
        with snapshot(effective(resolve(card=card),self,card)):
            return self._update(payload)

    def _update(self, payload):
        payload = json.loads(dumps(payload))
        key = payload["idempotency_key"]
        fingerprint = digest(payload)
        with self.write():
            previous = self.db.execute(
                "SELECT hash,result FROM mutations WHERE key=?", (key,)
            ).fetchone()
            if previous:
                if previous[0] != fingerprint:
                    raise ValueError("Idempotency key reused for different content")
                return json.loads(previous[1])
            cid = payload["id"]
            exists = self.db.execute("SELECT revision FROM cases WHERE id=?", (cid,)).fetchone()
            revision = exists[0] if exists else 0
            if revision != payload["expected_revision"]:
                raise ValueError(f"Stale revision: expected {revision}")
            from .evidence import scope
            payload = scope(self, cid, payload)
            old = self.case(cid) if exists else None
            case = payload["case"]
            if not old:
                case["created_at"] = stamp(payload.get("effective_at") or now())
            elif old.get("created_at"):
                case["created_at"] = old["created_at"]
            self.limited(case, "title", 180)
            self.limited(case, "position", 1200)
            if case.get("status", "open") not in {"open", "closed"}:
                raise ValueError("Case status must be open or closed")
            evidence = payload.get("evidence_ids", [])
            if not evidence:
                raise ValueError("Updates require supporting evidence IDs")
            self.evidence_exists(evidence)
            if (
                old
                and old["status"] == "closed"
                and case.get("status", "open") == "open"
                and not payload.get("reopen_reason")
            ):
                raise ValueError("Reopening requires a reason and evidence")
            actions = payload["actions"]
            ids = [a["id"] for a in actions]
            if len(set(ids)) != len(ids):
                raise ValueError("Duplicate action ID")
            if old and not {a["id"] for a in old["actions"] if a["status"] in OPEN}.issubset(ids):
                raise ValueError("Unresolved actions cannot disappear; resolve or dismiss them with evidence")
            # Previously resolved rows may be omitted. Validate their proof and
            # retain source identifiers in the card before retiring the rows.
            omitted = [a for a in (old or {}).get("actions", []) if a["id"] not in ids]
            actions += omitted
            for a in actions:
                owner = self.db.execute("SELECT case_id FROM actions WHERE id=?", (a["id"],)).fetchone()
                if owner and owner[0] != cid:
                    raise ValueError("Action ID belongs to another case")
                if a["status"] not in OPEN | {"done", "dismissed"}:
                    raise ValueError("Invalid action status")
                for field, cap in [
                    ("owner", 120),
                    ("next_step", 400),
                    ("completion_condition", 300),
                    ("reopening_condition", 300),
                ]:
                    self.limited(a, field, cap)
                if a["status"] not in OPEN:
                    self.limited(a, "completion_reason", 400)
                    if not a.get("completion_evidence"):
                        raise ValueError("Closure requires completion evidence")
                    self.evidence_exists(a["completion_evidence"])
                for field in (
                    "deadline",
                    "snooze_until",
                    "explicit_review",
                    "promised_at",
                    "reviewed_at",
                ):
                    if a.get(field):
                        a[field] = stamp(a[field])
                if a.get("reviewed_at") and not a.get("review_outcome"):
                    raise ValueError("Reviewed actions require review_outcome")
                if a.get("review_policy"):
                    from .scheduling import validate

                    validate(self, a["review_policy"])
                if a.get("snooze_override"):
                    override = a["snooze_override"]
                    if not override.get("reason") or not override.get("evidence_ids"):
                        raise ValueError(
                            "Early snooze override requires a concrete consequence and evidence"
                        )
                    self.evidence_exists(override["evidence_ids"])
                prior = next(
                    (x for x in (old or {}).get("actions", []) if x["id"] == a["id"]), None
                )
                # Existing actions without a trustworthy creation date remain unknown.
                if prior:
                    if prior.get("first_recorded_at"):
                        a["first_recorded_at"] = prior["first_recorded_at"]
                    else:
                        a.pop("first_recorded_at", None)
                else:
                    a["first_recorded_at"] = stamp(payload.get("effective_at") or now())
                from .followups import prepare

                prepare(self, a, prior, evidence, payload.get("effective_at"))
                if (
                    prior
                    and prior["status"] not in OPEN
                    and a["status"] in OPEN
                    and not payload.get("reopen_reason")
                ):
                    raise ValueError("Reopening an action requires a reason and evidence")
                scheduling = (
                    "status",
                    "deadline",
                    "snooze_until",
                    "explicit_review",
                    "promised_at",
                    "reviewed_at",
                    "review_policy",
                    "follow_up",
                )
                if prior and all(prior.get(k) == a.get(k) for k in scheduling):
                    a["next_review"], a["review_rule"] = prior.get("next_review"), prior.get(
                        "review_rule"
                    )
                else:
                    a["next_review"], a["review_rule"] = (
                        next_review(a, payload.get("effective_at"))
                        if a["status"] in OPEN
                        else (None, "closed")
                    )
            if case.get("status", "open") == "closed" and any(a["status"] in OPEN for a in actions):
                raise ValueError("Cannot close case with unresolved actions")
            decisions = payload.get("decisions", (old or {}).get("decisions", []))
            for d in decisions:
                self.limited(d, "text", 600)
                self.evidence_exists(d.get("evidence_ids", evidence))
            resolved = {a["id"] for a in actions if a["status"] not in OPEN}
            approvals = [(r["id"], json.loads(r["details"])) for r in self.db.execute(
                "SELECT id,details FROM approvals WHERE case_id=?", (cid,))]
            from .threadcache import pending_approval
            if resolved and any(pending_approval(a) for _,a in approvals):
                for table,column,owner_key in [('thread_messages','metadata','CacheActionIds'),('thread_attachments','details','action_ids')]:
                    for row in self.db.execute(f'SELECT {column} FROM {table} WHERE case_id=?',(cid,)):
                        owners=set(json.loads(row[0]).get(owner_key,[]))
                        if owners and owners.issubset(resolved):
                            raise ValueError('Reconcile pending send approvals before completing the final cache-owning action')
            spent_approvals = [(aid, a) for aid, a in approvals if not pending_approval(a)]
            if resolved or spent_approvals:
                self.limited(case, "context", 6000)
            # A prerequisite disappearing is not permission to start a reply
            # timer. The caller must explicitly reconcile the dependent action.
            remaining = [a for a in actions if a["status"] in OPEN]
            known = {r[0] for r in self.db.execute("SELECT id FROM actions")}
            known = (known | set(ids)) - resolved
            for a in remaining:
                for dependency in (a.get("follow_up") or {}).get("depends_on", []):
                    if dependency not in known:
                        raise ValueError("Reconcile dependency before retiring prerequisite: " + a["id"] + " -> " + dependency)
            if resolved:
                for row in self.db.execute("SELECT id,details FROM actions WHERE case_id<>?", (cid,)):
                    a = json.loads(row["details"])
                    if a["status"] in OPEN and resolved.intersection((a.get("follow_up") or {}).get("depends_on", [])):
                        raise ValueError("Reconcile dependency in " + row["id"] + " before retiring prerequisite")
            live_decisions = []
            for d in decisions:
                scope = d.get("action_ids")
                if scope is not None:
                    if not isinstance(scope, list) or not scope or any(not isinstance(x, str) or x not in {a['id'] for a in actions} for x in scope):
                        raise ValueError("Decision action_ids must name actions in this thread")
                    if set(scope).issubset(resolved):
                        continue
                    d["action_ids"] = [x for x in scope if x not in resolved]
                live_decisions.append(d)
            removed_decisions = {d["id"] for d in (old or {}).get("decisions", [])} - {d["id"] for d in live_decisions}
            if removed_decisions:
                self.limited(case, "context", 6000)
            # The agent has reviewed context in this same replacement. Preserve
            # compact evidence links, never a second copy of the spent records.
            for a in actions:
                evidence = list(dict.fromkeys(evidence + a.get("completion_evidence", [])))
            for d in (old or {}).get("decisions", []) + decisions:
                evidence = list(dict.fromkeys(evidence + d.get("evidence_ids", [])))
            for _, a in spent_approvals:
                evidence = list(dict.fromkeys(evidence + a.get("evidence_ids", [])))
            self.evidence_exists(evidence)
            at = now()
            self._save_case(dict(case, evidence_ids=evidence,actions=remaining,decisions=live_decisions), cid, revision + 1, at)
            self.db.execute("DELETE FROM actions WHERE case_id=?", (cid,))
            for a in remaining:
                self.db.execute(
                    "INSERT INTO actions VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,next_review=excluded.next_review,details=excluded.details",
                    (a["id"], cid, a["status"], a.get("next_review"), dumps(a)),
                )
            self.db.execute("DELETE FROM decisions WHERE case_id=?", (cid,))
            for d in live_decisions:
                self.db.execute("INSERT INTO decisions VALUES(?,?,?)", (d["id"], cid, dumps(d)))
            for aid, _ in spent_approvals:
                self.db.execute("DELETE FROM approvals WHERE id=?", (aid,))
            from .threadcache import queue_action_cleanup
            queue_action_cleanup(self, cid)
            # Reviewing/rescheduling unchanged work must not manufacture a new
            # user-facing case change. Never clear an older unreported change.
            after = self.case(cid)
            before_report = dict(old, decisions=[d for d in old["decisions"] if d["id"] not in removed_decisions]) if old else None
            if old and self.report_signature(before_report) == self.report_signature(after):
                self.db.execute(
                    "UPDATE reported_cases SET revision=? WHERE case_id=? AND revision=?",
                    (revision + 1, cid, revision),
                )
                if old.get('last_quiet_revision') == revision and old.get('context') == after.get('context'):
                    card = json.loads(self.db.execute('SELECT details FROM cases WHERE id=?',(cid,)).fetchone()[0])
                    card['last_quiet_revision'] = revision + 1
                    self.db.execute('UPDATE cases SET details=? WHERE id=?',(dumps(card),cid))
            result = {"id": cid, "revision": revision + 1}
            if payload.get("request_hash"):
                result["request_hash"] = payload["request_hash"]
            self.db.execute(
                "INSERT INTO mutations VALUES(?,?,?)", (key, fingerprint, dumps(result))
            )
            return result

    @staticmethod
    def report_signature(case):
        action_fields = (
            "id",
            "status",
            "owner",
            "next_step",
            "completion_condition",
            "reopening_condition",
            "deadline",
            "snooze_until",
            "completion_reason",
            "completion_evidence",
        )
        return dumps(
            {
                "title": case["title"],
                "position": case["position"],
                "status": case["status"],
                "actions": [
                    {k: a.get(k) for k in action_fields}
                    for a in sorted(case["actions"], key=lambda a: a["id"])
                    if a["status"] in OPEN
                ],
                "decisions": case.get("decisions", []),
            }
        )

    def suggest_cases(self, message):
        """Suggestions only. Exact locators/conversations/references, never sender-only."""
        suggestions = []
        for row in self.db.execute("SELECT id,details FROM cases").fetchall():
            current = json.loads(row["details"])
            try:keys = validate_match_keys(current.get('match_keys', {}))
            except ValueError:keys = {}  # Legacy bad hints cannot stop unrelated ingestion.
            reasons = []
            if (
                message.get("conversation_id")
                and message["conversation_id"] in keys.get("conversation_ids", [])
                and message.get("source") in keys.get("sources", [])
            ):
                reasons.append("verified conversation link")
            for ref in keys.get("references", []):
                if ref and ref in message.get("subject", ""):
                    reasons.append("exact reference: " + ref)
            for eid in current.get("evidence_ids", [e['id'] for e in current.get('evidence_index',[])]):
                e = self.db.execute("SELECT metadata FROM evidence WHERE id=?", (eid,)).fetchone()
                if not e:
                    continue
                metadata = json.loads(e[0])
                if (
                    message.get("entry_id")
                    and metadata.get("entry_id") == message["entry_id"]
                    and metadata.get("store_id") == message.get("store_id")
                ):
                    reasons.append("exact store-qualified message link")
            if reasons:
                suggestions.append({"case_id": row["id"], "reasons": sorted(set(reasons))})
        return suggestions

    def put_record(self, cmd, payload):
        h = self
        if cmd == 'approval':
            from .approvals import put
            return put(h, payload)
        with self.write():
            if cmd in {'context', 'issue'}:
                from .evidence import scope
                payload = scope(h, payload.get('case_id') if cmd != 'context' else None, payload)
            if cmd == "context":
                h.evidence_exists(payload.get("evidence_ids", []))
                h.limited(payload, "text", 1200)
                h.db.execute(
                    "INSERT INTO context VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET entity=excluded.entity,details=excluded.details",
                    (payload["id"], payload["entity"], dumps(payload)),
                )
            elif cmd == "issue":
                if payload.get("resolved") and not payload.get("resolution"):
                    raise ValueError("Issue resolution requires explanation")
                h.db.execute(
                    "INSERT INTO issues VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET details=excluded.details,resolved=excluded.resolved",
                    (
                        payload["id"],
                        payload.get("case_id"),
                        dumps(payload),
                        int(payload.get("resolved", False)),
                    ),
                )
            else:
                h.db.execute(
                    "INSERT INTO roundup VALUES(?,?,NULL) ON CONFLICT(id) DO UPDATE SET details=excluded.details",
                    (payload["id"], dumps(payload)),
                )

        return {"id": payload["id"]}
