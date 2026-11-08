import json
import uuid
from datetime import datetime, timedelta
from .model import LONDON, MAIL, CAL, now, stamp, dumps


class RunRecords:
    def start_run(self, activated=False, at=None):
        at = stamp(at or now())
        rid = uuid.uuid4().hex
        checkpoints = {r["source"]: dict(r) for r in self.db.execute("SELECT * FROM checkpoints")}
        overlap = stamp((datetime.fromisoformat(at) - timedelta(hours=48)).isoformat())
        windows = {s: min(overlap, checkpoints.get(s, {}).get("reviewed") or overlap) for s in MAIL}
        from .filing import enabled, status as filing_status

        filing_required = enabled(self)
        filing = filing_status(self, at) if filing_required else []
        with self.write():
            self.db.execute(
                "INSERT INTO runs VALUES(?,?,?,?)",
                (
                    rid,
                    at,
                    "running",
                    dumps(
                        {
                            "activated": activated,
                            "mail_since": windows,
                            "filing_required": filing_required,
                            "filing_windows": filing,
                        }
                    ),
                ),
            )
            self.touch("run", rid)
            from .live import prune
            prune(self,keep=rid)
        return {"id": rid, "mail_since": windows, "until": at, "filing_windows": filing}

    def run(self, rid):
        r = self.db.execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone()
        if not r:
            raise ValueError("Unknown run")
        return dict(r, details=json.loads(r["details"]))

    def record_run(self, rid, payload):
        with self.write():
            return self._record_run(rid, payload)

    def record_runs(self, rid, payloads):
        with self.write():
            return [self._record_run(rid, payload) for payload in payloads]

    def _record_run(self, rid, payload):
        run = self.run(rid)
        if run['details'].get('working_expired'):
            raise ValueError(
                "Technical detail expired; continue pending work in a fresh monitoring pass"
            )
        if run["status"] == "complete":
            raise ValueError("Completed run is immutable")
        kind = payload["kind"]
        if kind == "source":
            source = payload["source"]
            if source not in MAIL + CAL:
                raise ValueError("Unknown or deprecated source")
            self.evidence_exists(payload.get("evidence_ids", []))
            self.evidence_exists(payload.get("freshness_evidence_ids", []))
            if not payload.get("evidence_ids") or payload.get("provenance") not in {
                "command",
                "assistant",
            }:
                raise ValueError("Source result requires evidence and provenance")
            payload=dict(payload,_run_started_at=run['started_at'])
            carried=0
            for pending in self.db.execute('SELECT id,details FROM pending_work WHERE source=?',(source,)).fetchall():
                if not self.db.execute('SELECT 1 FROM candidates WHERE run_id=? AND id=?',(rid,pending['id'])).fetchone():
                    self.db.execute('INSERT INTO candidates VALUES(?,?,?)',(rid,pending['id'],pending['details']));carried+=1
            if source in MAIL:
                payload['carried_candidate_count']=carried
                payload['candidate_count']=self.db.execute("SELECT count(*) FROM candidates WHERE run_id=? AND json_extract(details,'$.source')=?",(rid,source)).fetchone()[0]
            from .live import record
            record(self,source,payload,run['started_at'],verified=False)
            self.db.execute(
                "INSERT INTO sources VALUES(?,?,?) ON CONFLICT(run_id,source) DO UPDATE SET details=excluded.details",
                (rid, source, dumps(payload)),
            )
        elif kind == "candidate":
            self.evidence_exists(payload.get("evidence_ids", []))
            source = payload["source"]
            if source not in MAIL:
                raise ValueError("Invalid candidate source")
            from .messages import identity, changed, REVIEW_FIELDS

            cid = payload.get("id") or identity(payload)
            old = self.db.execute(
                "SELECT details FROM candidates WHERE run_id=? AND id=?", (rid, cid)
            ).fetchone()
            data = self.decode_message(old[0]) if old else {}
            if old and changed(data, payload):
                for key in REVIEW_FIELDS:
                    data.pop(key, None)
            data.update(payload, id=cid)
            if data.get("body_evidence_id"):
                self.evidence_exists([data["body_evidence_id"]])
            if data.get("case_id"):
                self.case(data["case_id"])
            self.db.execute(
                "INSERT INTO candidates VALUES(?,?,?) ON CONFLICT(run_id,id) DO UPDATE SET details=excluded.details",
                (rid, cid, self.encode_message(data)),
            )
            if self.candidate_complete(data):self.db.execute('DELETE FROM pending_work WHERE id=?',(cid,))
        elif kind == "gate":
            self.evidence_exists(payload.get("evidence_ids", []))
            run["details"][payload["name"]] = payload
            self.db.execute("UPDATE runs SET details=? WHERE id=?", (dumps(run["details"]), rid))
        elif kind == "handoff":
            # Capture what was delivered, not whatever revisions exist at finalize.
            payload = json.loads(dumps(payload))
            snapshots = {}
            report = {i["id"]: i for i in self.report_items(rid) if i["type"] == "case"}
            for cid in set(payload.get("case_ids", [])):
                c = self.case(cid)
                expected = payload.get("case_revisions", {}).get(cid, c["revision"])
                if expected != c["revision"]:
                    raise ValueError(
                        "Case changed before handoff recording; refresh report: " + cid
                    )
                snapshots[cid] = {
                    "revision": c["revision"],
                    "required_actions": report.get(cid, {}).get("required_actions", []),
                }
            quiet = payload.get('quiet_cases', [])
            if not isinstance(quiet, list):
                raise ValueError('quiet_cases must be a list')
            seen = set(payload.get('case_ids', []))
            for entry in quiet:
                if not isinstance(entry, dict) or not isinstance(entry.get('case_id'), str):
                    raise ValueError('Quiet acknowledgement requires a case_id')
                if set(entry) - {'case_id','revision','reason','evidence_ids'}:
                    raise ValueError('Quiet acknowledgement stores only revision, reason and evidence, not card copies')
                cid = entry['case_id']
                if cid in seen:
                    raise ValueError('Duplicate or both reported and quiet case: ' + cid)
                seen.add(cid)
                c = self.case(cid)
                if type(entry.get('revision')) is not int or entry['revision'] != c['revision']:
                    raise ValueError('Case changed before quiet acknowledgement: ' + cid)
                if cid not in report or report[cid]['required_actions']:
                    raise ValueError('Quiet acknowledgement cannot hide required work: ' + cid)
                self.limited(entry, 'reason', 300)
                if not entry.get('evidence_ids'):
                    raise ValueError('Quiet acknowledgement requires scoped evidence')
                self.evidence_exists(entry['evidence_ids'])
                if not set(entry['evidence_ids']).intersection(c['evidence_ids']):
                    raise ValueError('Quiet evidence must be linked to this case: ' + cid)
            payload["reported_case_snapshots"] = snapshots
            run["details"]["handoff"] = payload
            self.db.execute("UPDATE runs SET details=? WHERE id=?", (dumps(run["details"]), rid))
        else:
            raise ValueError("Unknown run record kind")

        self.touch("run", rid)
        return {"id": rid, "recorded": kind}

    def report_items(self, rid):
        with self.read():
            return self._report_items(rid)

    def _report_items(self, rid):
        run = self.run(rid)
        if run['details'].get('working_expired'):
            raise ValueError(
                "Historical technical detail expired; consult the retained run receipt"
            )
        items = []
        at = getattr(self,'_query_at',None) or now()
        from .approvals import items as approval_items
        items.extend(approval_items(self, at=at, due_only=True))
        from .followups import reminder_due, due_at
        reported_cases = {
            row["case_id"]: dict(row) for row in self.db.execute("SELECT * FROM reported_cases")
        }
        for c in self.all_cases():
            reported = reported_cases.get(c["id"])
            handled = max(reported['revision'] if reported else 0, c.get('last_quiet_revision', 0))
            changed = (
                (handled != c["revision"])
                if handled
                else (bool(c.get("created_at")) or c["status"] == "open")
            )
            required = [
                a["id"]
                for a in c["actions"]
                if (a["status"] == "needs-user" or reminder_due(a, at))
                and not (
                    a.get("snooze_until")
                    and a["snooze_until"] > at
                    and not a.get("snooze_override")
                )
            ]
            if required or changed:
                items.append(
                    {
                        "type": "case",
                        "id": c["id"],
                        "revision": c["revision"],
                        "previous_context": (
                            'Use dated card context/evidence through the last-reported marker.' if reported else 'Not previously reported'
                        ),
                        'context':c.get('context'),
                        'purpose':c.get('purpose'),
                        'last_reported_at':c.get('last_reported_at'),
                        "changed": changed,
                        "current_position": c["position"],
                        "required_actions": required,
                        "quiet_allowed": not required,
                    }
                )
                items.extend(
                    {"type": "action", "case": c["id"], **a,
                     "follow_up_due": reminder_due(a, at),
                     "follow_up_due_at": due_at(a)}
                    for a in c["actions"]
                    if a["id"] in required or changed
                )
        start = datetime.fromisoformat(run["started_at"]).astimezone(LONDON).date()
        days = [(start + timedelta(days=n)).isoformat() for n in range(4)]
        sources = {
            r[0]: json.loads(r[1])
            for r in self.db.execute("SELECT source,details FROM sources WHERE run_id=?", (rid,))
        }
        for s in CAL:
            source = sources.get(s, {})
            items.append(
                {
                    "type": "calendar_source",
                    "source": s,
                    "status": source.get("status", "unavailable"),
                    "days": days,
                }
            )
            prev = self.db.execute(
                "SELECT details FROM calendar_reports WHERE source=?", (s,)
            ).fetchone()
            previous_events = (
                {e["id"]: e for e in json.loads(prev[0]).get("events", [])} if prev else {}
            )
            current_ids = set()
            for event in source.get("events", []):
                current_ids.add(event["id"])
                state = (
                    "Unverified"
                    if source.get("status") != "current"
                    else (
                        "New"
                        if event["id"] not in previous_events
                        else ("Unchanged" if event == previous_events[event["id"]] else "Updated")
                    )
                )
                items.append({"type": "event", "source": s, "change_state": state, **event})
            if source.get("status") == "current" and source.get("complete"):
                cancelled_ids = set()
                for eid, event in previous_events.items():
                    if (
                        eid not in current_ids
                        and event.get("start")
                        and event.get("end")
                        and stamp(event["start"]) < stamp((start + timedelta(days=4)).isoformat())
                        and stamp(event["end"]) > stamp(start.isoformat())
                    ):
                        items.append({"type": "cancellation", "source": s, **event})
                        cancelled_ids.add(eid)
                for event in source.get("explicit_cancellations", []):
                    if (
                        event["id"] not in cancelled_ids
                        and event.get("start")
                        and event.get("end")
                        and stamp(event["start"]) < stamp((start + timedelta(days=4)).isoformat())
                        and stamp(event["end"]) > stamp(start.isoformat())
                    ):
                        items.append({"type": "cancellation", "source": s, **event})
                        cancelled_ids.add(event["id"])
        if start.weekday() == 6:
            items.extend(
                {"type": "roundup", "id": r[0], **json.loads(r[1])}
                for r in self.db.execute(
                    "SELECT id,details FROM roundup WHERE reported_run IS NULL"
                )
            )
        return items

    def candidate_complete(self, candidate):
        disposition = candidate.get("disposition")
        if disposition not in {"routine", "material", "suppressed", "duplicate"}:
            return False
        if not candidate.get("evidence_ids"):
            return False
        if disposition != "material":
            return bool(candidate.get("review_reason"))
        body = candidate.get("body_evidence_id")
        if (
            not body
            or not self.db.execute(
                "SELECT 1 FROM evidence WHERE id=? AND length(body)>0", (body,)
            ).fetchone()
        ):
            return False
        if candidate.get("no_update_reason"):
            return True
        cid = candidate.get("case_id")
        row = self.db.execute("SELECT revision FROM cases WHERE id=?", (cid,)).fetchone()
        return bool(
            row
            and isinstance(candidate.get("case_revision"), int)
            and 0 < candidate["case_revision"] <= row[0]
        )

    def run_status(self, rid):
        run = self.run(rid)
        d = run["details"]
        if d.get('working_expired'):
            return dict(d.get('last_verification',{}),status=run['status'],fully_verified=False,technical_detail_expired=True,started_at=run['started_at'],instruction='Working data expired; start a fresh pass. Unresolved candidates remain in pending_work.')
        if run["status"] == "complete" and d.get("final_verification"):
            return d["final_verification"]
        failures = []
        if d.get("filing_required"):
            for window in d["filing_windows"]:
                r = self.db.execute(
                    "SELECT value FROM meta WHERE key=?", ("filing:" + window["source"],)
                ).fetchone()
                success = (json.loads(r[0]).get("last_success") or {}) if r else {}
                if (
                    not success
                    or success["completed_at"] < run["started_at"]
                    or success["since"] > window["since"]
                    or success["until"] < window["until"]
                ):
                    failures.append(window["source"] + ": filing incomplete/unverified")
        if d.get("activated"):
            expected = "Mail check " + datetime.fromisoformat(run["started_at"]).astimezone(
                LONDON
            ).strftime("%d/%m/%Y")
            if d.get("title", {}).get("value") != expected or not d.get("title", {}).get(
                "evidence_ids"
            ):
                failures.append("title not verified")
        refresh = d.get("refresh", {})
        if (
            not refresh.get("success")
            or not refresh.get("evidence_ids")
            or not refresh.get("completed_at")
        ):
            failures.append("Send/Receive not verified")
        sources = {
            r[0]: json.loads(r[1])
            for r in self.db.execute("SELECT source,details FROM sources WHERE run_id=?", (rid,))
        }
        for s in MAIL + CAL:
            source = sources.get(s, {})
            if (
                source.get("status") != "current"
                or not source.get("complete")
                or not source.get("evidence_ids")
            ):
                failures.append(s + ": incomplete/unavailable")
            if s in MAIL:
                if not source.get("freshness_evidence_ids"):
                    failures.append(s + ": freshness evidence missing")
                if (
                    not source.get("newest")
                    or not isinstance(source.get("count"), int)
                    or source.get("count", -1) < 0
                ):
                    failures.append(s + ": freshness/count missing")
                if (
                    not source.get("since")
                    or stamp(source["since"]) > d["mail_since"][s]
                    or not source.get("until")
                    or stamp(source["until"]) < run["started_at"]
                ):
                    failures.append(s + ": scan window incomplete")
                if refresh.get("completed_at") and (
                    not source.get("started_at")
                    or stamp(source["started_at"]) < stamp(refresh["completed_at"])
                ):
                    failures.append(s + ": scan preceded completed refresh")
            else:
                start = datetime.fromisoformat(run["started_at"]).astimezone(LONDON).date()
                if source.get("days") != [
                    (start + timedelta(days=n)).isoformat() for n in range(4)
                ]:
                    failures.append(s + ": calendar window incomplete")
        pending = 0
        for r in self.db.execute("SELECT details FROM candidates WHERE run_id=?", (rid,)):
            c = self.decode_message(r[0])
            if not self.candidate_complete(c):
                pending += 1
        for s in MAIL:
            count = self.db.execute(
                "SELECT count(*) FROM candidates WHERE run_id=? AND json_extract(details,'$.source')=?",
                (rid, s),
            ).fetchone()[0]
            if sources.get(s, {}).get("candidate_count") != count:
                failures.append(s + ": candidate ledger mismatch")
        if pending:
            failures.append(f"{pending} candidates awaiting review")
        handoff = d.get("handoff", {})
        report = self.report_items(rid)
        coverage = self.action_coverage(report, handoff.get("action_ids", []))
        if coverage["missing_action_ids"]:
            failures.append("Required actions missing from report")
        approval_ids = {i['id'] for i in report if i['type'] == 'approval_review'}
        if not approval_ids.issubset(set(handoff.get('approval_ids', []))):
            failures.append('Approval reviews missing from report')
        changed = {item["id"] for item in report if item["type"] == "case" and item["changed"]}
        cases = {i['id']: i for i in report if i['type'] == 'case'}
        quiet_ids = set()
        for entry in handoff.get('quiet_cases', []):
            cid = entry['case_id']
            current = self.db.execute('SELECT revision FROM cases WHERE id=?', (cid,)).fetchone()
            if not current or current[0] != entry['revision']:
                failures.append('Case changed after quiet acknowledgement: ' + cid)
            elif not cases.get(cid, {}).get('required_actions'):
                quiet_ids.add(cid)
        if not changed.issubset(set(handoff.get("case_ids", [])) | quiet_ids):
            failures.append("Changed cases missing from report")
        for cid, snapshot in handoff.get("reported_case_snapshots", {}).items():
            current = self.db.execute("SELECT revision FROM cases WHERE id=?", (cid,)).fetchone()
            if current and current[0] != snapshot["revision"]:
                failures.append("Case changed after delivered report: " + cid)
        events = {s + ":" + str(e["id"]) for s in CAL for e in sources.get(s, {}).get("events", [])}
        if not events.issubset(set(handoff.get("event_ids", []))):
            failures.append("Calendar events missing from report")
        start = datetime.fromisoformat(run["started_at"]).astimezone(LONDON).date()
        if handoff.get("days") != [(start + timedelta(days=n)).isoformat() for n in range(4)]:
            failures.append("Four-day calendar table missing")
        if not handoff.get("delivered"):
            failures.append("Handoff not delivered")
        roundups = {x["id"] for x in report if x["type"] == "roundup"}
        if not roundups.issubset(set(handoff.get("roundup_ids", []))):
            failures.append("Weekly roundup missing from report")
        cancellations = {
            x["source"] + ":" + str(x["id"]) for x in report if x["type"] == "cancellation"
        }
        if not cancellations.issubset(set(handoff.get("cancellation_ids", []))):
            failures.append("Calendar cancellations missing from report")
        issues = {r[0] for r in self.db.execute("SELECT id FROM issues WHERE resolved=0")}
        if not issues.issubset(set(handoff.get("issue_ids", []))):
            failures.append("Unresolved record issues missing from report")
        if not d.get("records", {}).get("success") or not d.get("records", {}).get("evidence_ids"):
            failures.append("Record validation not verified")
        return {
            "id": rid,
            "status": run["status"],
            "fully_verified": not failures,
            "failures": failures,
            "pending_candidates": pending,
            "action_coverage": coverage,
        }

    @staticmethod
    def action_coverage(report, reported_ids):
        from collections import Counter

        required = {
            a for item in report if item["type"] == "case" for a in item["required_actions"]
        }
        counts = Counter(reported_ids)
        reported = set(counts)
        missing = sorted(required - reported)
        labels = {i["id"]: i["next_step"] for i in report if i["type"] == "action"}
        return {
            "required_count": len(required),
            "included_required_count": len(required & reported),
            "missing_action_ids": missing,
            "missing_actions": [{"id": a, "next_step": labels.get(a, "")} for a in missing],
            "additional_action_ids": sorted(reported - required),
            "additional_count": len(reported - required),
            "unique_reported_count": len(reported),
            "duplicate_entries": {a: n - 1 for a, n in sorted(counts.items()) if n > 1},
            "duplicate_entry_count": sum(n - 1 for n in counts.values()),
        }

    def finalize(self, rid):
        if self.run(rid)['details'].get('working_expired'):
            raise ValueError(
                "Compacted checks cannot be re-finalized; start a fresh monitoring pass"
            )
        status = self.run_status(rid)
        if status["status"] == "complete":
            run = self.run(rid)
            if not run["details"].get("final_verification"):
                with self.write():
                    run["details"]["final_verification"] = status
                    self.db.execute(
                        "UPDATE runs SET details=? WHERE id=?", (dumps(run["details"]), rid)
                    )
            return status
        with self.write():
            # Revalidate after acquiring the writer lock: another connection may
            # have changed a case since the initial status read.
            status = self.run_status(rid)
            state = "complete" if status["fully_verified"] else "partial"
            self.db.execute("UPDATE runs SET status=? WHERE id=?", (state, rid))
            run = self.run(rid)
            run["details"]["last_verification"] = dict(status, status=state)
            self.db.execute("UPDATE runs SET details=? WHERE id=?", (dumps(run["details"]), rid))
            from .live import record as record_health
            current={r[0]:json.loads(r[1]) for r in self.db.execute('SELECT source,details FROM sources WHERE run_id=?',(rid,))}
            for s in MAIL+CAL:
                p=current.get(s,{'status':'unavailable','complete':False,'errors':['Source not collected in this pass']})
                valid=p.get('status')=='current' and p.get('complete') is True and not any(f.startswith(s+':') for f in status['failures'])
                if s in MAIL and 'Send/Receive not verified' in status['failures']:valid=False
                record_health(self,s,p,run['started_at'],valid)
            for r in self.db.execute(
                "SELECT source,details FROM sources WHERE run_id=?", (rid,)
            ).fetchall():
                s, source = r[0], json.loads(r[1])
                if source.get("status") != "current" or not source.get("complete"):
                    continue
                if any(f.startswith(s + ":") for f in status["failures"]) or (
                    s in MAIL and "Send/Receive not verified" in status["failures"]
                ):
                    continue
                candidates = [
                    self.decode_message(c[0])
                    for c in self.db.execute(
                        "SELECT details FROM candidates WHERE run_id=? AND json_extract(details,'$.source')=?",
                        (rid, s),
                    )
                ]
                reviewed = s in CAL or (
                    source.get("candidate_count") == len(candidates)
                    and all(self.candidate_complete(c) for c in candidates)
                )
                old = self.db.execute("SELECT * FROM checkpoints WHERE source=?", (s,)).fetchone()
                advance = lambda value, previous: max(value or "", previous or "") or None
                self.db.execute(
                    "INSERT INTO checkpoints VALUES(?,?,?,?) ON CONFLICT(source) DO UPDATE SET discovered=excluded.discovered,reviewed=excluded.reviewed,reported=excluded.reported",
                    (
                        s,
                        advance(run["started_at"], old["discovered"] if old else None),
                        advance(
                            run["started_at"] if reviewed else None,
                            old["reviewed"] if old else None,
                        ),
                        advance(
                            run["started_at"] if state == "complete" else None,
                            old["reported"] if old else None,
                        ),
                    ),
                )
                if s in CAL:
                    previous = self.db.execute(
                        "SELECT COALESCE(json_extract(c.details,'$._run_started_at'),r.started_at) FROM calendar_state c LEFT JOIN runs r ON r.id=c.run_id WHERE c.source=?",
                        (s,),
                    ).fetchone()
                    if not previous or run["started_at"] >= previous[0]:
                        self.db.execute(
                            "INSERT OR REPLACE INTO calendar_state VALUES(?,?,?)",
                            (s, rid, dumps(source)),
                        )
                for c in candidates:
                    if self.candidate_complete(c) and c.get("disposition") != "duplicate":
                        previous = self.db.execute(
                            "SELECT COALESCE(json_extract(m.details,'$._run_started_at'),r.started_at) FROM message_reviews m LEFT JOIN runs r ON r.id=json_extract(m.details,'$.reviewed_run') WHERE m.id=?",
                            (c["id"],),
                        ).fetchone()
                        if previous and previous[0] > run["started_at"]:
                            continue
                        self.db.execute(
                            "INSERT OR REPLACE INTO message_reviews VALUES(?,?,?)",
                            (c["id"], s, self.encode_message(dict(c,reviewed_run=rid,_run_started_at=run['started_at']))),
                        )
            handoff = run["details"].get("handoff", {})
            expected_days = [
                (
                    datetime.fromisoformat(run["started_at"]).astimezone(LONDON).date()
                    + timedelta(days=n)
                ).isoformat()
                for n in range(4)
            ]
            if handoff.get("delivered") and handoff.get("days") == expected_days:
                for source_name in CAL:
                    source_row = self.db.execute(
                        "SELECT details FROM sources WHERE run_id=? AND source=?",
                        (rid, source_name),
                    ).fetchone()
                    if not source_row:
                        continue
                    source = json.loads(source_row[0])
                    required_events = {
                        source_name + ":" + str(e["id"]) for e in source.get("events", [])
                    }
                    required_cancellations = {
                        i["source"] + ":" + str(i["id"])
                        for i in self.report_items(rid)
                        if i["type"] == "cancellation" and i["source"] == source_name
                    }
                    previous = self.db.execute(
                        "SELECT COALESCE(json_extract(c.details,'$._run_started_at'),r.started_at) FROM calendar_reports c LEFT JOIN runs r ON r.id=c.run_id WHERE c.source=?",
                        (source_name,),
                    ).fetchone()
                    source_valid = not any(
                        f.startswith(source_name + ":") for f in status["failures"]
                    )
                    if (
                        source_valid
                        and source.get("status") == "current"
                        and source.get("complete")
                        and required_events.issubset(handoff.get("event_ids", []))
                        and required_cancellations.issubset(handoff.get("cancellation_ids", []))
                        and (not previous or run["started_at"] >= previous[0])
                    ):
                        self.db.execute(
                            "INSERT OR REPLACE INTO calendar_reports VALUES(?,?,?)",
                            (source_name, rid, dumps(source)),
                        )
            records = run["details"].get("records", {})
            if handoff.get("delivered") and records.get("success") and records.get("evidence_ids"):
                # Quiet is a reviewed disposition, never a claim of delivery.
                report = {i['id']: i for i in self.report_items(rid) if i['type'] == 'case'}
                for entry in handoff.get('quiet_cases', []):
                    cid = entry['case_id']
                    c = self.case(cid)
                    if c['revision'] != entry['revision'] or report.get(cid, {}).get('required_actions'):
                        continue
                    candidates = [self.decode_message(r[0]) for r in self.db.execute(
                        "SELECT details FROM candidates WHERE run_id=? AND json_extract(details,'$.case_id')=?", (rid,cid))]
                    if any(not self.candidate_complete(c) for c in candidates):
                        continue
                    if entry['revision'] < c.get('last_quiet_revision', 0):
                        continue
                    if entry['revision'] == c.get('last_quiet_revision') and run['started_at'] < c.get('last_quiet_at', ''):
                        continue
                    card = json.loads(self.db.execute('SELECT details FROM cases WHERE id=?',(cid,)).fetchone()[0])
                    card.update(last_quiet_revision=entry['revision'],last_quiet_at=run['started_at'])
                    self.db.execute('UPDATE cases SET details=? WHERE id=?',(dumps(card),cid))
                action_ids = set(handoff.get("action_ids", []))
                for cid, snapshot in handoff.get("reported_case_snapshots", {}).items():
                    if cid not in handoff.get("case_ids", []) or not set(
                        snapshot["required_actions"]
                    ).issubset(action_ids):
                        continue
                    candidates = [
                        self.decode_message(r[0])
                        for r in self.db.execute(
                            "SELECT details FROM candidates WHERE run_id=? AND json_extract(details,'$.case_id')=?",
                            (rid, cid),
                        )
                    ]
                    if any(not self.candidate_complete(c) for c in candidates):
                        continue
                    self.db.execute(
                        "INSERT INTO reported_cases VALUES(?,?) ON CONFLICT(case_id) DO UPDATE SET revision=excluded.revision WHERE excluded.revision>=reported_cases.revision",
                        (cid, snapshot["revision"]),
                    )
                    row=self.db.execute('SELECT details FROM cases WHERE id=?',(cid,)).fetchone();card=json.loads(row[0])
                    if card.get('last_reported_at','')<=run['started_at']:
                        card['last_reported_at']=run['started_at'];self.db.execute('UPDATE cases SET details=? WHERE id=?',(dumps(card),cid))
            if state == "complete":
                for eid in run["details"].get("handoff", {}).get("roundup_ids", []):
                    self.db.execute("DELETE FROM roundup WHERE id=?", (eid,))
                details = run["details"]
                details["final_verification"] = dict(status, status="complete")
                self.db.execute("UPDATE runs SET details=? WHERE id=?", (dumps(details), rid))
            self.db.execute(
                "INSERT OR REPLACE INTO metrics VALUES(?,?)",
                (
                    rid,
                    dumps(
                        {
                            "at": now(),
                            "pending_candidates": status["pending_candidates"],
                            "validation_failures": len(status["failures"]),
                            "state": state,
                            "action_coverage": status["action_coverage"],
                            "current_briefing_characters": len(dumps(self.current_items(True))),
                        }
                    ),
                ),
            )
            remaining = self.db.execute(
                "SELECT value FROM meta WHERE key='observation_remaining'"
            ).fetchone()
            if state == "complete" and remaining and int(remaining[0]) > 0:
                self.db.execute(
                    "UPDATE meta SET value=? WHERE key='observation_remaining'",
                    (str(int(remaining[0]) - 1),),
                )
            self.touch("run", rid)
        result = self.run_status(rid)
        with self.write():
            previous=self.db.execute("SELECT value FROM meta WHERE key='last_pass'").fetchone()
            if not previous or json.loads(previous[0]).get('started_at','')<=self.run(rid)['started_at']:
                self.db.execute("INSERT INTO meta VALUES('last_pass',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(dumps({'started_at':self.run(rid)['started_at'],'status':result['status'],'failures':result['failures']}),))
        from .maintenance import daily

        daily(self)
        return result
