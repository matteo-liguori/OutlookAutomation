import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from monitor.core import BUDGET, CAL, MAIL, Harness, dumps, next_review, stamp
from monitor.sources import MAIL_STORES


class HarnessTests(unittest.TestCase):
    def test_operation_guidance_is_available_without_database(self):
        from monitor.__main__ import parser
        from monitor.commands import execute
        result = execute(None, parser().parse_args(['guide', 'permissions']))
        self.assertEqual(result['topic'], 'permissions')
        self.assertIn('Only explicit user confirmation', result['instruction'])
        self.assertNotIn('Closure deletes', result['instruction'])

    def test_briefing_guidance_only_for_matching_access_and_permission_triggers(self):
        from argparse import Namespace
        from unittest.mock import patch
        from monitor.commands import handle_briefing
        args = Namespace(command='briefing', operation=None, task='', case_ids=[], message_input=None, limit=50)
        self.h._query_at = '2026-11-08T09:00:00+00:00'
        with patch.object(self.h, 'current_items') as current, patch('monitor.cards.watch_items', return_value=[]):
            current.return_value = []
            self.assertEqual([i['type'] for i in handle_briefing(self.h, args, None, 'briefing')['items'] if i['type']!='briefing_section'], ['command_hint'])
            current.return_value = [{'type':'source_health','source':'mail:work','status':'unverified'}]
            items = handle_briefing(self.h, args, None, 'briefing')['items']
            self.assertEqual(next(i for i in items if i['type']!='briefing_section')['type'], 'mail_access_prompt')
            self.assertFalse(any(i.get('type') == 'permission_review_prompt' for i in items))
            current.return_value = [{'type':'approval_review','review_due':True}]
            items = handle_briefing(self.h, args, None, 'briefing')['items']
            self.assertEqual(next(i for i in items if i['type']!='briefing_section')['type'], 'permission_review_prompt')
            self.assertFalse(any(i.get('type') == 'mail_access_prompt' for i in items))

    def test_pass_guidance_includes_activation_only_when_activated(self):
        from argparse import Namespace
        from unittest.mock import patch
        from monitor.commands import handle_run
        args = Namespace(command='run', operation='start', activated=False)
        with patch.object(self.h, 'start_run', side_effect=lambda activated: {'id':'example'}):
            topics = [g['topic'] for g in handle_run(self.h, args, None, 'run')['guidance']]
            self.assertEqual(topics, ['pass-receipts'])
            args.activated = True
            topics = [g['topic'] for g in handle_run(self.h, args, None, 'run')['guidance']]
            self.assertEqual(topics, ['pass-receipts', 'activation'])

    def test_upgrade_review_five_day_cadence_without_reading_backlog(self):
        from unittest.mock import patch
        from monitor.improvements import checkpoint, review_prompt
        self.assertEqual(review_prompt(self.h, '2026-11-08T12:00:00+00:00'), [])
        self.assertTrue(review_prompt(self.h, '2026-11-08T19:00:00+00:00'))
        with patch('monitor.improvements.now', return_value='2026-11-08T12:00:00+00:00'):
            state = checkpoint(self.h, {'summary': 'Reviewed current proposals', 'evidence_ids': ['e1']})
        self.assertEqual(state['next_due_at'], '2026-11-13T12:00:00+00:00')
        def authorizer(code, table, column, *rest):
            if code == sqlite3.SQLITE_READ and table == 'improvements':
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        self.h.db.set_authorizer(authorizer)
        try:
            self.assertEqual(review_prompt(self.h, '2026-11-13T11:59:59+00:00'), [])
            self.assertEqual(review_prompt(self.h, '2026-11-13T12:00:00+00:00'), [])
            self.assertEqual(review_prompt(self.h, '2026-11-13T13:59:59+00:00'), [])
            self.assertTrue(review_prompt(self.h, '2026-11-13T14:00:00+00:00'))
            self.assertTrue(review_prompt(self.h, '2026-11-14T19:00:00+00:00'))
            self.assertEqual(review_prompt(self.h, '2027-06-01T12:59:59+00:00'), [])
            self.assertTrue(review_prompt(self.h, '2027-06-01T13:00:00+00:00'))
        finally:
            self.h.db.set_authorizer(None)
        with self.assertRaises(ValueError):
            checkpoint(self.h, {'summary': ''})
        with self.assertRaises(ValueError):
            checkpoint(self.h, {'summary': 'Unverified', 'evidence_ids': ['missing']})
        stored = json.loads(self.h.db.execute("SELECT value FROM meta WHERE key='upgrades_review'").fetchone()[0])
        self.assertEqual(stored, state)

    def test_upgrade_prompt_in_due_afternoon_briefing_not_finalization(self):
        from argparse import Namespace
        from unittest.mock import patch
        from monitor.commands import handle_briefing, handle_run
        args = Namespace(command='briefing', operation=None, task='', case_ids=[], message_input=None, limit=50)
        self.h._query_at = '2026-11-08T12:00:00+00:00'
        full = handle_briefing(self.h, args, None, 'briefing')['items']
        self.assertFalse(any(i.get('type') == 'upgrades_review_due' for i in full))
        again = handle_briefing(self.h, args, None, 'briefing')['items']
        self.assertEqual(again, full)
        self.h._query_at = '2026-11-08T14:00:00+00:00'
        afternoon = handle_briefing(self.h, args, None, 'briefing')['items']
        self.assertEqual(next(i for i in afternoon if i['type']!='briefing_section')['type'], 'upgrades_review_due')
        self.assertEqual(handle_briefing(self.h, args, None, 'briefing')['items'], afternoon)
        args.task = 'unrelated'
        scoped = handle_briefing(self.h, args, None, 'briefing-scoped')['items']
        self.assertFalse(any(i.get('type') == 'upgrades_review_due' for i in scoped))
        args.operation = 'finalize'
        args.id = 'example'
        with patch.object(self.h, 'finalize', return_value={'status': 'complete'}):
            with patch('monitor.commands.now', return_value='2026-11-08T09:00:00+00:00'):
                self.assertNotIn('upgrades_review', handle_run(self.h, args, None, 'run'))
            with patch('monitor.commands.now', return_value='2026-11-08T19:00:00+00:00'):
                result = handle_run(self.h, args, None, 'run')
                self.assertNotIn('upgrades_review', result)

    def test_upgrade_context_migration_is_atomic_and_preserves_existing_backlog(self):
        from monitor.improvements import add, items
        self.h.put_record('context', {'id': 'upgrades', 'entity': 'Upgrade list', 'text': 'Two requests'})
        source = self.h.db.execute("SELECT details FROM context WHERE id='upgrades'").fetchone()[0]
        def entry(item_id):
            return dict(id=item_id, observation='User request', proposal='Develop this upgrade',
                        validation='Verify preserved user data', references=['user:request'])
        add(self.h, dict(items=[entry('existing')], evidence_ids=['e1']))
        payload = dict(items=[entry('new'), entry('existing')], evidence_ids=['e1'],
                       source_context=dict(id='upgrades', expected_details=source))
        with self.assertRaises(ValueError):
            add(self.h, payload)
        self.assertEqual([i['id'] for i in items(self.h)], ['existing'])
        self.assertIsNotNone(self.h.db.execute("SELECT 1 FROM context WHERE id='upgrades'").fetchone())
        payload['items'] = [entry('new'), entry('second')]
        result = add(self.h, payload)
        self.assertEqual(result['removed_context_id'], 'upgrades')
        self.assertEqual([i['id'] for i in items(self.h)], ['existing', 'new', 'second'])
        self.assertIsNone(self.h.db.execute("SELECT 1 FROM context WHERE id='upgrades'").fetchone())

    def test_upgrade_migration_rejects_changed_source_and_missing_evidence(self):
        from monitor.improvements import add, items
        self.h.put_record('context', {'id': 'upgrades', 'entity': 'Upgrade list', 'text': 'Changed'})
        payload = dict(items=[dict(id='new', observation='Request', proposal='Upgrade',
                                  validation='Check', references=['user:request'])], evidence_ids=['e1'],
                       source_context=dict(id='upgrades', expected_details='outdated'))
        with self.assertRaises(ValueError):
            add(self.h, payload)
        self.assertEqual(items(self.h), [])
        payload.pop('source_context')
        payload['evidence_ids'] = ['missing']
        with self.assertRaises(ValueError):
            add(self.h, payload)
        self.assertEqual(items(self.h), [])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "monitor.db"
        self.h = Harness(self.path, create=True)
        self.h.put_evidence({"id": "e1", "body": "Actual source", "source": "test"})

    def tearDown(self):
        self.h.close()
        self.tmp.cleanup()

    def payload(self, cid="T1"):
        return {
            "id": cid,
            "expected_revision": 0,
            "idempotency_key": cid + "-1",
            "evidence_ids": ["e1"],
            "case": {"title": "Example", "position": "Current position", "context": "Relevant current facts", "status": "open"},
            "actions": [
                {
                    "id": cid + "-a",
                    "owner": "Example user",
                    "status": "needs-user",
                    "next_step": "Check payment",
                    "completion_condition": "Receipt confirmed",
                    "reopening_condition": "New failure",
                }
            ],
        }

    def test_current_does_not_query_history_or_body(self):
        self.h.update(self.payload())
        forbidden = []

        def authorizer(code, table, column, *rest):
            if code == sqlite3.SQLITE_READ and (
                table == "history" or (table == "evidence" and column == "body")
            ):
                forbidden.append((table, column))
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        self.h.db.set_authorizer(authorizer)
        self.h.case("T1")
        self.h.current_items(True)
        self.assertEqual(forbidden, [])

    def test_replace_retry_stale_and_no_action_loss(self):
        p = self.payload()
        self.h.update(p)
        self.assertEqual(self.h.update(p)["revision"], 1)
        q = copy.deepcopy(p)
        q.update(expected_revision=1, idempotency_key="2")
        q["case"]["position"] = "New position"
        self.h.update(q)
        self.assertEqual(self.h.case("T1")["position"], "New position")
        with self.assertRaises(ValueError):
            self.h.update(dict(q, idempotency_key="3"))
        q.update(expected_revision=2, idempotency_key="4")
        q["actions"] = []
        with self.assertRaises(ValueError):
            self.h.update(q)
        self.assertEqual(self.h.case("T1")["revision"], 2)

    def test_close_requires_evidence_and_siblings_remain(self):
        p = self.payload()
        p["actions"].append(dict(p["actions"][0], id="T1-b"))
        self.h.update(p)
        p.update(expected_revision=1, idempotency_key="2")
        p["actions"][0]["status"] = "done"
        with self.assertRaises(ValueError):
            self.h.update(p)
        p["actions"][0].update(completion_reason="User confirmed", completion_evidence=["e1"])
        self.h.update(p)
        self.assertEqual(len(self.h.current_items(True)), 1)
        p.update(expected_revision=2, idempotency_key="3")
        p["case"]["status"] = "closed"
        with self.assertRaises(ValueError):
            self.h.update(p)

    def test_current_paging_and_budget(self):
        for n in range(40):
            self.h.update(self.payload("T" + str(n)))
        records=self.h.current_items(True)
        page = self.h.page("briefing", records, limit=7)
        all_items = []
        self.h.update(self.payload("TNEW"))
        with self.assertRaises(ValueError):self.h.page('briefing',self.h.current_items(True),cursor=page['next_cursor'])
        while True:
            self.assertLessEqual(len(json.dumps(page, ensure_ascii=False, indent=2)) + 12, BUDGET)
            all_items.extend(page["items"])
            if not page["next_cursor"]:
                break
            page = self.h.page("briefing", records, cursor=page["next_cursor"], limit=7)
        self.assertEqual(len(all_items), 40)
        self.assertEqual(len({a["action"] for a in all_items}), 40)

    def test_large_history_lossless_chunks(self):
        item = {"id": "big", "text": 'quoted " unicode é\n' * 3000}
        page = self.h.page("history", [item], limit=10)
        chunks = []
        while True:
            self.assertLessEqual(len(json.dumps(page, ensure_ascii=False, indent=2)) + 12, BUDGET)
            chunks.extend(x["json_chunk"] for x in page["items"])
            if not page["next_cursor"]:
                break
            page = self.h.page("history", [item], cursor=page["next_cursor"])
        self.assertEqual(json.loads("".join(chunks)), item)


    def test_snooze_and_workdays_dst(self):
        p = self.payload()
        p["actions"][0]["snooze_until"] = "2026-10-26T09:00:00+00:00"
        self.h.update(p)
        self.assertEqual(self.h.current_items(True, "2026-10-25T10:00:00Z"), [])
        self.assertEqual(len(self.h.current_items(True, "2026-10-26T09:00:00Z")), 1)
        review, _ = next_review({"status": "waiting"}, "2026-10-23T10:00:00+01:00")
        self.assertEqual(review, "2026-10-30T09:00:00+00:00")

    def test_deadline_does_not_reset_without_review(self):
        p = self.payload()
        p["actions"][0]["deadline"] = "2026-09-23T09:00:00+01:00"
        self.h.update(p)
        date = self.h.case("T1")["actions"][0]["next_review"]
        p.update(expected_revision=1, idempotency_key="2")
        p["case"]["position"] = "New text"
        self.h.update(p)
        self.assertEqual(date, self.h.case("T1")["actions"][0]["next_review"])

    def test_discovery_and_genuine_missed_deadline_reviews(self):
        action = {
            "status": "needs-user",
            "deadline": "2026-09-19T10:00:00Z",
            "first_recorded_at": "2026-09-17T12:00:00Z",
        }
        self.assertEqual(
            next_review(action), ("2026-09-17T12:00:00+00:00", "initial-deadline-review")
        )
        action["first_recorded_at"] = "2026-09-10T12:00:00Z"
        self.assertEqual(next_review(action)[0], "2026-09-12T10:00:00+00:00")
        action["reviewed_at"] = "2026-09-17T13:00:00Z"
        self.assertEqual(next_review(action)[0], "2026-09-19T10:00:00+00:00")

    def test_coverage_extras_duplicates_missing_and_frozen(self):
        report = [
            {"type": "case", "required_actions": ["A", "B", "C"]},
            {"type": "action", "id": "B", "next_step": "Complete enrolment"},
        ]
        full = self.h.action_coverage(report, ["A", "B", "C", "D", "E", "E"])
        self.assertEqual(
            (
                full["included_required_count"],
                full["additional_count"],
                full["duplicate_entry_count"],
            ),
            (3, 2, 1),
        )
        missing = self.h.action_coverage(report, ["A", "C", "D", "E", "E"])
        self.assertEqual(
            missing["missing_actions"], [{"id": "B", "next_step": "Complete enrolment"}]
        )
        rid = self.verified_run()
        frozen = self.h.finalize(rid)["action_coverage"]
        self.h.update(self.payload())
        self.assertEqual(self.h.run_status(rid)["action_coverage"], frozen)
        metrics = json.loads(
            self.h.db.execute("SELECT details FROM metrics WHERE run_id=?", (rid,)).fetchone()[0]
        )
        self.assertEqual(metrics["action_coverage"], frozen)

    def test_backup_restore(self):
        self.h.update(self.payload())
        path = self.h.backup("restore-test")[0]
        restored = Harness(path)
        self.assertEqual(restored.case("T1"), self.h.case("T1"))
        restored.close()

    def test_failed_transaction_rolls_back(self):
        with self.assertRaises(RuntimeError):
            with self.h.write():
                self.h.db.execute("INSERT INTO issues VALUES('x',NULL,'{}',0)")
                raise RuntimeError("crash")
        self.assertEqual(self.h.db.execute("SELECT count(*) FROM issues").fetchone()[0], 0)

    def test_two_connections_reject_stale_write(self):
        self.h.update(self.payload())
        other = Harness(self.path)
        p = self.payload()
        p.update(expected_revision=1, idempotency_key="2")
        self.h.update(p)
        p.update(idempotency_key="3")
        with self.assertRaises(ValueError):
            other.update(p)
        other.close()

    def test_partial_run_cannot_complete(self):
        rid = self.h.start_run()["id"]
        status = self.h.finalize(rid)
        self.assertEqual(status["status"], "partial")
        self.assertFalse(status["fully_verified"])
        self.assertEqual(self.h.db.execute("SELECT count(*) FROM checkpoints").fetchone()[0], 0)

    def test_unreviewed_material_candidate(self):
        rid = self.h.start_run()["id"]
        self.h.record_run(
            rid,
            {
                "kind": "candidate",
                "source": MAIL[0],
                "entry_id": "x",
                "disposition": "material",
                "evidence_ids": ["e1"],
            },
        )
        self.assertEqual(self.h.run_status(rid)["pending_candidates"], 1)

    def test_missing_database_fails(self):
        with self.assertRaises(ValueError):
            Harness(Path(self.tmp.name) / "missing.db")

    def test_limits_fail_without_truncation(self):
        p = self.payload()
        p["case"]["position"] = "x" * 1201
        with self.assertRaises(ValueError):
            self.h.update(p)

    def verified_run(self):
        rid = self.h.start_run(at="2026-09-17T12:00:00Z")["id"]
        self.h.record_run(
            rid,
            {
                "kind": "gate",
                "name": "refresh",
                "success": True,
                "completed_at": "2026-09-17T11:59:00Z",
                "evidence_ids": ["e1"],
            },
        )
        days = ["2026-09-17", "2026-09-18", "2026-09-19", "2026-09-20"]
        for s in MAIL + CAL:
            value = {
                "kind": "source",
                "source": s,
                "status": "current",
                "complete": True,
                "evidence_ids": ["e1"],
                "provenance": "command",
                "days": days,
                "events": [],
            }
            if s in MAIL:
                self.h.record_run(
                    rid,
                    {
                        "kind": "candidate",
                        "source": s,
                        "entry_id": "x",
                        "disposition": "routine",
                        "review_reason": "Reviewed; no action",
                        "evidence_ids": ["e1"],
                    },
                )
                value.update(
                    count=1,
                    candidate_count=1,
                    newest="2026-09-17T11:00:00Z",
                    since="2026-09-15T12:00:00Z",
                    until="2026-09-17T12:00:00Z",
                    started_at="2026-09-17T12:00:00Z",
                    freshness_evidence_ids=["e1"],
                )
            self.h.record_run(rid, value)
        self.h.record_run(
            rid, {"kind": "gate", "name": "records", "success": True, "evidence_ids": ["e1"]}
        )
        self.h.record_run(
            rid,
            {
                "kind": "handoff",
                "delivered": True,
                "days": days,
                "action_ids": [],
                "case_ids": [],
                "event_ids": [],
            },
        )
        return rid

    def test_positive_run_and_omitted_action_gate(self):
        self.h.update(self.payload())
        rid = self.verified_run()
        self.assertIn("Required actions missing from report", self.h.run_status(rid)["failures"])
        self.h.record_run(
            rid,
            {
                "kind": "handoff",
                "delivered": True,
                "days": ["2026-09-17", "2026-09-18", "2026-09-19", "2026-09-20"],
                "action_ids": ["T1-a"],
                "case_ids": ["T1"],
                "event_ids": [],
            },
        )
        self.assertTrue(self.h.run_status(rid)["fully_verified"])
        self.assertEqual(self.h.finalize(rid)["status"], "complete")

    def report_case(self, rid, cids=("T1",), actions=("T1-a",), delivered=True):
        self.h.record_run(
            rid,
            {
                "kind": "handoff",
                "delivered": delivered,
                "days": ["2026-09-17", "2026-09-18", "2026-09-19", "2026-09-20"],
                "action_ids": list(actions),
                "case_ids": list(cids),
                "event_ids": [],
            },
        )

    def test_partial_acknowledges_only_delivered_cases_keeps_user_actions(self):
        p = self.payload()
        p["actions"][0]["status"] = "waiting"
        self.h.update(p)
        rid = self.verified_run()
        p.update(expected_revision=1, idempotency_key="changed")
        p["case"]["position"] = "Reported update"
        self.h.update(p)
        self.h.update(self.payload("T2"))
        self.report_case(rid, actions=())
        self.h.record_run(
            rid,
            {
                "kind": "source",
                "source": CAL[0],
                "status": "unavailable",
                "complete": False,
                "evidence_ids": ["e1"],
                "provenance": "command",
            },
        )
        self.assertEqual(self.h.finalize(rid)["status"], "partial")
        self.assertEqual(
            self.h.db.execute("SELECT revision FROM reported_cases WHERE case_id='T1'").fetchone()[
                0
            ],
            2,
        )
        self.assertIsNone(
            self.h.db.execute("SELECT revision FROM reported_cases WHERE case_id='T2'").fetchone()
        )
        report = self.h.report_items(self.h.start_run()["id"])
        self.assertFalse(any(i["type"] == "case" and i["id"] == "T1" for i in report))
        self.assertTrue(any(i["type"] == "action" and i["id"] == "T2-a" for i in report))
        self.assertEqual(self.h.case("T1")["actions"][0]["status"], "waiting")
        self.assertEqual(self.h.case("T2")["actions"][0]["status"], "needs-user")

    def test_acknowledged_case_still_reports_unresolved_user_action(self):
        self.h.update(self.payload())
        rid = self.verified_run()
        self.report_case(rid)
        self.h.record_run(
            rid,
            {
                "kind": "source",
                "source": CAL[0],
                "status": "unavailable",
                "complete": False,
                "evidence_ids": ["e1"],
                "provenance": "command",
            },
        )
        self.h.finalize(rid)
        item = next(i for i in self.h.report_items(rid) if i["type"] == "case" and i["id"] == "T1")
        self.assertFalse(item["changed"])
        self.assertEqual(item["required_actions"], ["T1-a"])

    def test_handoff_does_not_acknowledge_later_revision_or_regress(self):
        p = self.payload()
        p["actions"][0]["status"] = "waiting"
        self.h.update(p)
        rid = self.verified_run()
        self.report_case(rid, actions=())
        p.update(expected_revision=1, idempotency_key="later")
        p["case"]["position"] = "Later unreported"
        self.h.update(p)
        self.h.finalize(rid)
        self.assertEqual(
            self.h.db.execute("SELECT revision FROM reported_cases WHERE case_id='T1'").fetchone()[
                0
            ],
            1,
        )
        self.assertTrue(next(i for i in self.h.report_items(rid) if i["type"] == "case")["changed"])
        self.report_case(rid, actions=())
        self.h.finalize(rid)
        self.assertEqual(
            self.h.db.execute("SELECT revision FROM reported_cases WHERE case_id='T1'").fetchone()[
                0
            ],
            2,
        )

    def test_handoff_explicit_revision_guard(self):
        self.h.update(self.payload())
        rid = self.verified_run()
        with self.assertRaisesRegex(ValueError, "Case changed"):
            self.h.record_run(
                rid,
                {
                    "kind": "handoff",
                    "delivered": True,
                    "case_ids": ["T1"],
                    "case_revisions": {"T1": 0},
                },
            )


    def test_old_partial_finalize_cannot_regress_newer_acknowledgement(self):
        p = self.payload()
        p["actions"][0]["status"] = "waiting"
        self.h.update(p)
        old = self.verified_run()
        self.report_case(old, actions=())
        self.h.record_run(
            old,
            {
                "kind": "source",
                "source": CAL[0],
                "status": "unavailable",
                "complete": False,
                "evidence_ids": ["e1"],
                "provenance": "command",
            },
        )
        self.h.finalize(old)
        p.update(expected_revision=1, idempotency_key="newer")
        p["case"]["position"] = "New revision"
        self.h.update(p)
        new = self.verified_run()
        self.report_case(new, actions=())
        self.h.finalize(new)
        with self.assertRaises(ValueError):self.h.finalize(old)
        self.assertEqual(
            self.h.db.execute("SELECT revision FROM reported_cases WHERE case_id='T1'").fetchone()[
                0
            ],
            2,
        )

    def test_missing_delivery_action_or_candidate_review_blocks_case_ack(self):
        for mode in ["undelivered", "missing-action", "unreviewed", "records-failed"]:
            with self.subTest(mode=mode):
                cid = "T-" + mode
                self.h.update(self.payload(cid))
                rid = self.verified_run()
                self.report_case(
                    rid,
                    cids=(cid,),
                    actions=() if mode == "missing-action" else (cid + "-a",),
                    delivered=mode != "undelivered",
                )
                if mode == "unreviewed":
                    self.h.record_run(
                        rid,
                        {
                            "kind": "candidate",
                            "source": MAIL[0],
                            "entry_id": "unreviewed",
                            "case_id": cid,
                            "disposition": "material",
                            "evidence_ids": ["e1"],
                        },
                    )
                if mode == "records-failed":
                    self.h.record_run(
                        rid,
                        {
                            "kind": "gate",
                            "name": "records",
                            "success": False,
                            "evidence_ids": ["e1"],
                        },
                    )
                self.h.finalize(rid)
                self.assertIsNone(
                    self.h.db.execute(
                        "SELECT revision FROM reported_cases WHERE case_id=?", (cid,)
                    ).fetchone()
                )

    def quiet_case(self, cid='quiet'):
        p = self.payload(cid)
        p['actions'][0]['status'] = 'watch'
        p['actions'][0]['follow_up'] = dict(mode='quiet', interval_days=7, basis='calendar',
            reason='Routine parcel; only concrete problems require reporting', evidence_ids=['e1'])
        self.h.update(p)
        return p

    def quiet_handoff(self, rid, cid='quiet', **changes):
        c = self.h.case(cid)
        payload = dict(kind='handoff', delivered=True, days=['2026-09-17','2026-09-18','2026-09-19','2026-09-20'],
            case_ids=[], action_ids=[], event_ids=[], quiet_cases=[dict(case_id=cid,
            revision=c['revision'], reason='Standing quiet-parcel policy; no concrete problem', evidence_ids=c['evidence_ids'])])
        payload.update(changes)
        self.h.record_run(rid, payload)

    def test_scoped_quiet_acknowledgement_is_not_reported_delivery(self):
        self.quiet_case(); rid = self.verified_run()
        self.assertTrue(next(i for i in self.h.report_items(rid) if i['type']=='case')['quiet_allowed'])
        self.quiet_handoff(rid)
        self.assertEqual(self.h.finalize(rid)['status'], 'complete')
        self.assertIsNone(self.h.db.execute("SELECT 1 FROM reported_cases WHERE case_id='quiet'").fetchone())
        c = self.h.case('quiet')
        self.assertEqual(c['last_quiet_revision'],1)
        self.assertNotIn('last_reported_at',c)
        self.assertEqual(len(c['actions']),1)
        new = self.h.start_run()['id']
        self.assertFalse(any(i['type']=='case' and i['id']=='quiet' for i in self.h.report_items(new)))
        self.assertFalse(any(i.get('reason')=='changed-since-report' and i.get('case')=='quiet' for i in self.h.current_items(True)))

    def test_quiet_cannot_hide_user_work_or_due_reminder(self):
        for mode in ('user','reminder'):
            p=self.payload(mode)
            if mode=='reminder':
                p['actions'][0].update(status='waiting',follow_up=dict(mode='reminder',interval_days=7,
                    basis='calendar',due_at='2026-09-01T09:00:00Z',reason='Reply overdue',evidence_ids=['e1']))
            self.h.update(p);rid=self.verified_run()
            with self.assertRaisesRegex(ValueError,'required work'):
                self.quiet_handoff(rid,mode)

    def test_quiet_acknowledgement_requires_current_scope_and_proof(self):
        self.quiet_case();rid=self.verified_run()
        entry=dict(case_id='quiet',revision=1,reason='No report under standing policy',evidence_ids=self.h.case('quiet')['evidence_ids'])
        self.h.put_evidence(dict(id='unrelated',source='test',body='Unrelated source'))
        for edit in (dict(revision=0),dict(reason=''),dict(evidence_ids=[]),dict(evidence_ids=['missing']),dict(evidence_ids=['unrelated']),dict(context='Copied old card')):
            with self.subTest(edit=edit), self.assertRaises(ValueError):
                self.quiet_handoff(rid,quiet_cases=[dict(entry,**edit)])
        for changes in (dict(quiet_cases=[entry,entry]),dict(case_ids=['quiet'],quiet_cases=[entry])):
            with self.assertRaisesRegex(ValueError,'Duplicate'):
                self.quiet_handoff(rid,**changes)
        for value in (None, {}, [None], ['quiet']):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.quiet_handoff(rid,quiet_cases=value)

    def test_user_dismissed_closed_case_can_stay_quiet(self):
        p=self.quiet_case('closed')
        p.update(expected_revision=1,idempotency_key='user-dismissal')
        p['case'].update(status='closed',context='User dismissed this FYI; no work remains.')
        p['actions'][0].update(status='dismissed',completion_reason='User dismissal',completion_evidence=['e1'])
        self.h.update(p)
        rid=self.verified_run();self.quiet_handoff(rid,'closed')
        self.assertEqual(self.h.finalize(rid)['status'],'complete')
        self.assertEqual(self.h.case('closed')['actions'],[])
        self.assertIsNone(self.h.db.execute("SELECT 1 FROM reported_cases WHERE case_id='closed'").fetchone())

    def test_later_material_change_invalidates_quiet_snapshot(self):
        p=self.quiet_case();rid=self.verified_run();self.quiet_handoff(rid)
        p.update(expected_revision=1,idempotency_key='new-failure')
        p['case']['position']='Delivery failed; investigation required'
        self.h.update(p)
        result=self.h.finalize(rid)
        self.assertEqual(result['status'],'partial')
        self.assertIn('Case changed after quiet acknowledgement: quiet',result['failures'])
        self.assertNotIn('last_quiet_revision',self.h.case('quiet'))

    def test_unreviewed_candidate_does_not_advance_quiet_marker(self):
        self.quiet_case();rid=self.verified_run();self.quiet_handoff(rid)
        self.h.record_run(rid,dict(kind='candidate',source=MAIL[0],entry_id='unknown',case_id='quiet',
            disposition='material',evidence_ids=['e1']))
        self.assertEqual(self.h.finalize(rid)['status'],'partial')
        self.assertNotIn('last_quiet_revision',self.h.case('quiet'))

    def test_quiet_marker_survives_routine_review_but_not_new_context(self):
        p=self.quiet_case();rid=self.verified_run();self.quiet_handoff(rid);self.h.finalize(rid)
        p.update(expected_revision=1,idempotency_key='routine-review')
        p['case']['last_quiet_revision']=100
        self.h.update(p)
        self.assertEqual(self.h.case('quiet')['last_quiet_revision'],2)
        p.update(expected_revision=2,idempotency_key='new-context')
        p['case']['context']='New concrete refund problem in source evidence'
        self.h.update(p)
        self.assertEqual(self.h.case('quiet')['last_quiet_revision'],2)
        new=self.h.start_run()['id']
        self.assertTrue(any(i['type']=='case' and i['id']=='quiet' and i['changed'] for i in self.h.report_items(new)))

    def test_snoozed_user_action_resurfaces_after_quiet_acknowledgement(self):
        from unittest.mock import patch
        p=self.payload('snoozed');p['actions'][0]['snooze_until']='2099-01-01T09:00:00Z'
        self.h.update(p);rid=self.verified_run();self.quiet_handoff(rid,'snoozed');self.h.finalize(rid)
        new=self.h.start_run()['id']
        with patch('monitor.runs.now',return_value='2099-01-02T09:00:00+00:00'):
            item=next(i for i in self.h.report_items(new) if i['type']=='case' and i['id']=='snoozed')
        self.assertEqual(item['required_actions'],['snoozed-a'])
        self.assertFalse(item['quiet_allowed'])

    def test_calendar_unavailable_does_not_cancel(self):
        rid = self.verified_run()
        event = {
            "id": "ev",
            "title": "Event",
            "start": "2026-09-18T12:00:00Z",
            "end": "2026-09-18T13:00:00Z",
        }
        with self.h.db:
            self.h.db.execute(
                "INSERT INTO calendar_state VALUES(?,?,?)",
                (CAL[0], "old", dumps({"events": [event]})),
            )
        self.h.record_run(
            rid,
            {
                "kind": "source",
                "source": CAL[0],
                "status": "unavailable",
                "complete": False,
                "evidence_ids": ["e1"],
                "provenance": "command",
            },
        )
        self.assertFalse(any(i["type"] == "cancellation" for i in self.h.report_items(rid)))
        self.assertFalse(self.h.run_status(rid)["fully_verified"])

    def test_scan_caps_missing_pages_and_moved_items(self):
        from monitor.collect import import_pages

        rid = self.h.start_run(at="2026-09-17T12:00:00Z")["id"]
        item = {
            "StoreId": "store",
            "EntryId": "entry",
            "InternetMessageId": "msg",
            "ConversationId": "conv",
            "MessageClass": "IPM.Schedule.Meeting.Canceled",
            "Received": "2026-09-17T11:00:00Z",
            "Subject": "Meeting cancelled",
            "SenderEmail": "a@example.com",
        }
        page = {
            "Store": MAIL_STORES["mail:work"],
            "SnapshotId": "fixture",
            "StartedAt": "2026-09-17T12:00:00Z",
            "Offset": 0,
            "FirstId": "entry",
            "StoreId": "store",
            "Since": "2026-09-15T12:00:00Z",
            "Until": "2026-09-17T12:00:00Z",
            "Items": [item],
            "Complete": False,
            "NextOffset": 1,
            "Errors": [],
        }
        result = import_pages(self.h, rid, MAIL[0], [page], "2026-09-17T12:00:00Z")
        self.assertFalse(result["complete"])
        bad = dict(page, Offset=2)
        with self.assertRaises(ValueError):
            import_pages(self.h, rid, MAIL[0], [page, bad], "2026-09-17T12:00:00Z")
        page.update(Complete=True, NextOffset=None)
        import_pages(self.h, rid, MAIL[0], [page], "2026-09-17T12:00:00Z")
        item["EntryId"] = "moved-entry"
        import_pages(self.h, rid, MAIL[0], [page], "2026-09-17T12:00:00Z")
        self.assertEqual(self.h.db.execute("SELECT count(*) FROM candidates").fetchone()[0], 1)

    def test_outage_extends_scan_window(self):
        with self.h.db:
            self.h.db.execute(
                "INSERT INTO checkpoints VALUES(?,?,?,?)",
                (MAIL[0], "2026-09-10T12:00:00+00:00", "2026-09-10T12:00:00+00:00", None),
            )
        run = self.h.start_run(at="2026-09-17T12:00:00Z")
        self.assertEqual(run["mail_since"][MAIL[0]], "2026-09-10T12:00:00+00:00")

    def test_promised_review_advances_after_actual_review(self):
        value = {
            "status": "waiting",
            "promised_at": "2026-09-11T19:00:00+01:00",
            "reviewed_at": "2026-09-14T10:00:00+01:00",
        }
        review, rule = next_review(value)
        self.assertEqual(review, "2026-09-21T08:00:00+00:00")
        self.assertEqual(rule, "weekly")

    def test_closed_reopening_and_snooze_override(self):
        p = self.payload()
        p["actions"][0]["snooze_until"] = "2030-01-01T09:00:00Z"
        self.h.update(p)
        self.assertEqual(self.h.current_items(True), [])
        p.update(expected_revision=1, idempotency_key="2")
        p["actions"][0]["snooze_override"] = {"reason": "Concrete lockout", "evidence_ids": ["e1"]}
        self.h.update(p)
        self.assertEqual(len(self.h.current_items(True)), 1)
        p.update(expected_revision=2, idempotency_key="3")
        p["actions"][0].update(
            status="done", completion_reason="User confirmed", completion_evidence=["e1"]
        )
        p['case'].update(status='closed',context='User confirmed completion.')
        self.h.update(p)
        self.assertEqual(self.h.case('T1')['actions'],[])
        p.update(expected_revision=3, idempotency_key="4")
        p['case']['status']='open'
        p['actions'][0]['id']='T1-new-work'
        p["actions"][0]["status"] = "needs-user"
        with self.assertRaises(ValueError):
            self.h.update(p)
        p["reopen_reason"] = "New material failure"
        self.h.update(p)

    def test_decisions_survive_omitted_field(self):
        p = self.payload()
        p["decisions"] = [
            {"id": "d", "text": "Do not send alternative draft", "evidence_ids": ["e1"]}
        ]
        self.h.update(p)
        p.update(expected_revision=1, idempotency_key="2")
        p.pop("decisions")
        self.h.update(p)
        self.assertEqual(len(self.h.case("T1")["decisions"]), 1)

    def test_invalid_window_does_not_advance_checkpoint(self):
        rid = self.verified_run()
        value = json.loads(
            self.h.db.execute(
                "SELECT details FROM sources WHERE run_id=? AND source=?", (rid, MAIL[0])
            ).fetchone()[0]
        )
        value["since"] = "2026-09-17T11:59:00Z"
        self.h.record_run(rid, value)
        self.h.finalize(rid)
        self.assertIsNone(
            self.h.db.execute("SELECT * FROM checkpoints WHERE source=?", (MAIL[0],)).fetchone()
        )
        self.assertIsNotNone(
            self.h.db.execute("SELECT * FROM checkpoints WHERE source=?", (MAIL[1],)).fetchone()
        )

    def test_completed_run_is_frozen(self):
        rid = self.verified_run()
        self.h.finalize(rid)
        self.h.update(self.payload())
        self.assertTrue(self.h.run_status(rid)["fully_verified"])

    def test_suggestions_require_exact_links(self):
        p = self.payload()
        p["case"]["match_keys"] = {
            "references": ["ORDER-123"],
            "sources": [MAIL[0]],
            "conversation_ids": ["conv"],
        }
        self.h.update(p)
        self.assertEqual(
            self.h.suggest_cases({"sender": "person@example.com", "subject": "Hello"}), []
        )
        self.assertEqual(self.h.suggest_cases({"subject": "Update ORDER-123"})[0]["case_id"], "T1")

    def test_approval_does_not_transfer_to_new_content(self):
        from monitor.__main__ import parser, execute

        p = self.payload()
        self.h.update(p)
        data = {
            "id": "approval",
            "case_id": "T1",
            "evidence_ids": ["e1"],
            "message": {
                "sender": "a@example.com",
                "to": ["b@example.com"],
                "cc": [],
                "bcc": [],
                "subject": "Subject",
                "body": "Approved",
                "attachments": [],
            },
        }
        f = Path(self.tmp.name) / "approval.json"
        f.write_text(json.dumps(data))
        args = parser().parse_args(["approval", "put", "--input", str(f)])
        execute(self.h, args)
        data["message"]["body"] = "Changed"
        f.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            execute(self.h, args)

    def test_nonempty_scan_is_not_freshness_proof(self):
        from monitor.collect import import_pages

        rid = self.h.start_run()["id"]
        page = {
            "Store": MAIL_STORES["mail:work"],
            "SnapshotId": "fixture",
            "StartedAt": "2026-09-17T12:00:00Z",
            "StoreId": "store",
            "Offset": 0,
            "FirstId": "x",
            "Since": "2026-09-15T12:00:00Z",
            "Until": "2026-09-17T12:00:00Z",
            "Items": [
                {
                    "StoreId": "store",
                    "EntryId": "x",
                    "Received": "2026-09-16T00:00:00Z",
                    "MessageClass": "IPM.Note",
                    "Subject": "Old mail",
                    "SenderEmail": "a@example.com",
                }
            ],
            "Complete": True,
            "NextOffset": None,
            "Errors": [],
        }
        result = import_pages(self.h, rid, MAIL[0], [page], "2026-09-17T12:00:00Z")
        self.assertTrue(result["complete"])
        self.assertEqual(result["status"], "unavailable")

        # Filing can empty the Inbox; a separate recent, stable store probe
        # still verifies current access without inventing an Inbox message.
        page["Items"] = []
        page["FirstId"] = None
        freshness = dict(Store=MAIL_STORES[MAIL[0]], LatestStoreId="store",
                         Status="current", Current=True,
                         CheckedAt="2026-09-17T12:00:00Z",
                         LatestReceived="2026-09-17T11:00:00Z")
        result = import_pages(self.h, rid, MAIL[0], [page],
                              "2026-09-17T12:00:00Z", freshness)
        self.assertEqual(result["status"], "current")
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["newest"], "2026-09-17T11:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
