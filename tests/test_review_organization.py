import copy, json, sqlite3, tempfile, unittest
from pathlib import Path
from monitor.core import Harness, dumps, next_review
from monitor.briefing import relevant
from monitor.organize import organize
from monitor.scheduling import review


class ReviewOrganizationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.h = Harness(Path(self.tmp.name) / "monitor.db", create=True)
        self.h.put_evidence({"id": "e", "body": "verified source", "source": "test"})

    def tearDown(self):
        self.h.close()
        self.tmp.cleanup()

    def proof_values(self, value):
        if isinstance(value, dict):
            return {k:([self.h.db.execute('SELECT body FROM evidence WHERE id=?',(eid,)).fetchone()[0] for eid in v]
                       if k.endswith('evidence_ids') or k=='completion_evidence' else self.proof_values(v)) for k,v in value.items()}
        if isinstance(value,list):return [self.proof_values(v) for v in value]
        return value

    def add(self, cid, title="Deposit claim", status="waiting"):
        p = dict(
            id=cid,
            expected_revision=0,
            idempotency_key="create-" + cid,
            evidence_ids=["e"],
            case=dict(
                title=title,
                position="Current position",
                status="open",
                match_keys={"references": [cid + "-REF"]},
            ),
            actions=[
                dict(
                    id=cid + "-a",
                    owner="Other party",
                    status=status,
                    next_step="Await reply",
                    completion_condition="Verified resolution",
                    reopening_condition="New evidence",
                    explicit_review="2026-09-01T08:00:00Z",
                )
            ],
            decisions=[
                dict(id=cid + "-d", text="Do not send without approval", evidence_ids=["e"])
            ],
        )
        self.h.update(p)

    def policy(self, days=14, urgency="normal"):
        return dict(
            interval_days=days,
            basis="calendar",
            urgency=urgency,
            reason="Expected turnaround",
            evidence_ids=["e"],
        )

    def test_policy_deadlines_promises_snoozes_and_user_work(self):
        a = dict(status="waiting", reviewed_at="2026-10-13T19:00:00Z", review_policy=self.policy())
        self.assertEqual(next_review(a)[0], "2026-10-27T09:00:00+00:00")
        a["deadline"] = "2026-10-18T20:00:00Z"
        self.assertEqual(next_review(a)[0], "2026-10-16T20:00:00+00:00")
        a["explicit_review"] = "2026-10-30T09:00:00Z"
        self.assertEqual(next_review(a)[0], "2026-10-16T20:00:00+00:00")
        a["snooze_until"] = "2026-11-01T09:00:00Z"
        self.assertEqual(next_review(a)[1], "user-snooze")
        a.pop("snooze_until")
        a.pop("deadline")
        a.pop("explicit_review")
        a["status"] = "needs-user"
        self.assertEqual(next_review(a)[1], "each-handoff")
        a["status"] = "waiting"
        a["promised_at"] = "2026-10-15T12:00:00Z"
        self.assertEqual(next_review(a)[1], "promised-date")

    def test_review_metadata_does_not_manufacture_case_change(self):
        self.add("A")
        with self.h.db:
            self.h.db.execute("INSERT INTO reported_cases VALUES('A',1)")
        p = dict(
            action_id="A-a",
            expected_revision=1,
            idempotency_key="review-1",
            review_kind="records",
            outcome="Current records checked; no new mailbox read",
            evidence_ids=["e"],
            review_policy=self.policy(),
        )
        result = review(self.h, p)
        self.assertEqual(review(self.h, p), result)
        self.assertFalse(
            any(i.get("reason") == "changed-since-report" for i in self.h.current_items(True))
        )
        self.assertEqual(self.h.case("A")["actions"][0]["status"], "waiting")
        p["outcome"] = "Different"
        with self.assertRaises(ValueError):
            review(self.h, p)

    def test_review_does_not_hide_preexisting_unreported_change(self):
        self.add("A")
        with self.h.db:
            self.h.db.execute("INSERT INTO reported_cases VALUES('A',0)")
        review(
            self.h,
            dict(
                action_id="A-a",
                expected_revision=1,
                idempotency_key="review",
                review_kind="records",
                outcome="Reviewed records",
                evidence_ids=["e"],
                review_policy=self.policy(),
            ),
        )
        self.assertTrue(
            any(i.get("reason") == "changed-since-report" for i in self.h.current_items(True))
        )

    def test_high_urgency_long_interval_rejected(self):
        self.add("A")
        with self.assertRaises(ValueError):
            review(
                self.h,
                dict(
                    action_id="A-a",
                    expected_revision=1,
                    idempotency_key="bad",
                    review_kind="records",
                    outcome="Checked",
                    evidence_ids=["e"],
                    review_policy=self.policy(30, "high"),
                ),
            )
        self.assertEqual(self.h.case("A")["revision"], 1)

    def test_relevant_retrieval_wakes_future_case_without_history(self):
        self.add("A", "DPS statutory declaration")
        review(
            self.h,
            dict(
                action_id="A-a",
                expected_revision=1,
                idempotency_key="review",
                review_kind="records",
                outcome="Checked",
                evidence_ids=["e"],
                review_policy=self.policy(30),
            ),
        )

        def guard(code, table, column, *rest):
            if code == sqlite3.SQLITE_READ and (
                table == "history" or (table == "evidence" and column == "body")
            ):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        self.h.db.set_authorizer(guard)
        self.assertTrue(
            any(i.get("action") == "A-a" for i in relevant(self.h, task="statutory declaration"))
        )
        self.assertTrue(
            any(
                i.get("action") == "A-a"
                for i in relevant(self.h, message={"subject": "Update A-REF"})
            )
        )
        self.assertEqual(relevant(self.h, message={"sender": "random@example.com"}), [])

    def merge_payload(self):
        return dict(
            target_id="A",
            source_ids=["B"],
            expected_revisions={"A": 1, "B": 1},
            idempotency_key="merge",
            evidence_ids=["e"],
            reason="Same deposit obligation, distinct actions retained",
            target={
                "title": "Combined deposit case",
                "position": "Both independent obligations remain",
            },
        )

    def test_merge_preserves_ids_schedules_evidence_decisions_and_approval_scope(self):
        self.add("A")
        self.add("B", status="needs-user")
        with self.h.db:
            self.h.db.execute("INSERT INTO approvals VALUES('approval','B','{}')")
        before = {a["id"]: a for cid in ["A", "B"] for a in self.h.case(cid)["actions"]}
        p = self.merge_payload()
        result = organize(self.h, "merge", p)
        self.assertEqual(organize(self.h, "merge", p), result)
        self.assertEqual(self.proof_values({a["id"]: a for a in self.h.case("A")["actions"]}), self.proof_values(before))
        self.assertEqual(self.h.case("B")["merged_into"], "A")
        self.assertEqual(self.h.case("B")["status"], "closed")
        self.assertEqual(len(self.h.case("A")["decisions"]), 2)
        self.assertEqual(
            self.h.db.execute("SELECT case_id FROM approvals WHERE id='approval'").fetchone()[0],
            "B",
        )
        self.assertTrue(any(i.get("action") == "B-a" for i in relevant(self.h, case_ids=["B"])))
        self.assertTrue(self.h.doctor()["ok"])

    def test_split_preserves_all_action_values_and_original_history(self):
        self.add("A")
        self.add("B")
        organize(self.h, "merge", self.merge_payload())
        old = self.h.case("A")
        moving = next(a for a in old["actions"] if a["id"] == "B-a")
        p = dict(
            source_id="A",
            target_id="C",
            action_ids=["B-a"],
            expected_revisions={"A": 2},
            idempotency_key="split",
            evidence_ids=["e"],
            reason="Different responsibilities",
            target={"title": "Separate deposit obligation", "position": "Independent followup"},
        )
        organize(self.h, "split", p)
        self.assertEqual(self.proof_values(self.h.case("C")["actions"]), self.proof_values([moving]))
        self.assertEqual([a["id"] for a in self.h.case("A")["actions"]], ["A-a"])
        self.assertEqual(self.h.case("C")["split_from"], "A")
        self.assertEqual(len(self.h.case("C")["decisions"]), 2)
        self.assertTrue(self.h.doctor()["ok"])

    def test_stale_and_invalid_organization_rolls_back(self):
        self.add("A")
        self.add("B")
        before = [self.h.case(x) for x in ["A", "B"]]
        p = self.merge_payload()
        p["expected_revisions"]["B"] = 0
        with self.assertRaises(ValueError):
            organize(self.h, "merge", p)
        self.assertEqual([self.h.case(x) for x in ["A", "B"]], before)
        p = self.merge_payload()
        p["target"]["position"] = "x" * 1201
        with self.assertRaises(ValueError):
            organize(self.h, "merge", p)
        self.assertEqual([self.h.case(x) for x in ["A", "B"]], before)

    def test_split_late_database_failure_rolls_back_action_movement(self):
        self.add("A")
        self.add("D", "Unrelated case")
        with self.h.db:
            self.h.db.execute(
                "UPDATE decisions SET id='C::A-d',details=? WHERE id='D-d'",
                (
                    dumps(
                        {"id": "C::A-d", "text": "Existing unique decision", "evidence_ids": ["e"]}
                    ),
                ),
            )
        before = self.h.case("A")
        p = dict(
            source_id="A",
            target_id="C",
            action_ids=["A-a"],
            expected_revisions={"A": 1},
            idempotency_key="split-fails",
            evidence_ids=["e"],
            reason="Independent obligation",
            target={"title": "Split case", "position": "Current split position"},
        )
        with self.assertRaises(sqlite3.IntegrityError):
            organize(self.h, "split", p)
        self.assertEqual(self.h.case("A"), before)
        self.assertIsNone(self.h.db.execute("SELECT id FROM cases WHERE id='C'").fetchone())
        self.assertEqual(
            self.h.db.execute("SELECT case_id FROM actions WHERE id='A-a'").fetchone()[0], "A"
        )

    def test_closed_cases_not_loaded_by_broad_task_terms(self):
        self.add("A", "DPS deposit")
        with self.h.db:
            self.h.db.execute("UPDATE cases SET status='closed' WHERE id='A'")
        self.assertEqual(relevant(self.h, task="DPS"), [])
        self.assertTrue(relevant(self.h, case_ids=["A"]))

    def test_cli_task_cursor_scope_and_review_routes(self):
        from monitor.__main__ import parser, execute

        self.add("A", "DPS claim")
        self.add("B", "Other case")
        args = parser().parse_args(["--limit", "1", "briefing", "--task", "DPS"])
        page = execute(self.h, args)
        self.assertIsNotNone(page["next_cursor"])
        args.cursor = page["next_cursor"]
        args.task = "Other"
        with self.assertRaises(ValueError):
            execute(self.h, args)


if __name__ == "__main__":
    unittest.main()
