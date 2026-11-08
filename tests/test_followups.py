import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from monitor.core import Harness
from monitor.followups import due_at, reminder_due
from monitor.scheduling import review


class FollowUpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.h = Harness(Path(self.tmp.name) / "monitor.db", create=True)
        self.h.put_evidence({"id": "e", "source": "test", "body": "Actual request"})
        self.payload = {
            "id": "A", "expected_revision": 0, "idempotency_key": "create",
            "effective_at": "2026-10-23T13:00:00+01:00", "evidence_ids": ["e"],
            "case": {"title": "Reimbursement", "position": "Await department", "status": "open"},
            "actions": [{"id": "A-a", "status": "waiting", "owner": "Department",
                         "next_step": "Await reply", "completion_condition": "Decision received",
                         "reopening_condition": "New request"}],
        }

    def tearDown(self):
        self.h.close()
        self.tmp.cleanup()

    def add(self):
        self.h.update(self.payload)
        return self.h.case("A")["actions"][0]

    def test_seven_calendar_days_and_london_dst(self):
        a = self.add()
        self.assertEqual(a["follow_up"]["interval_days"], 7)
        self.assertEqual(due_at(a), "2026-10-30T13:00:00+00:00")
        self.assertFalse(reminder_due(a, "2026-10-30T12:59:59Z"))
        self.assertTrue(reminder_due(a, "2026-10-30T13:00:00Z"))

    def test_records_and_no_reply_mail_review_cannot_postpone_chase(self):
        before = self.add()
        for n, kind in enumerate(["records", "mail"]):
            review(self.h, {"action_id": "A-a", "expected_revision": 1+n,
                           "idempotency_key": "review-"+kind, "review_kind": kind,
                           "outcome": "No new reply; bounded evidence reviewed", "evidence_ids": ["e"],
                           "review_policy": {"urgency": "normal", "interval_days": 7,
                                             "basis": "calendar", "reason": "Routine review", "evidence_ids": ["e"]}})
        after = self.h.case("A")["actions"][0]
        self.assertEqual(after["follow_up"], before["follow_up"])
        self.assertTrue(reminder_due(after, "2026-10-31T09:00:00Z"))

    def test_unchanged_waiting_reminder_required_in_report_and_validation(self):
        self.add()
        with self.h.db:
            self.h.db.execute("INSERT INTO reported_cases VALUES('A',1)")
        rid = self.h.start_run(at="2026-10-31T09:00:00Z")["id"]
        with patch("monitor.runs.now", return_value="2026-10-31T09:00:00Z"):
            report = self.h.report_items(rid)
            case = next(x for x in report if x["type"] == "case")
            self.assertFalse(case["changed"])
            self.assertEqual(case["required_actions"], ["A-a"])
            action = next(x for x in report if x["type"] == "action")
            self.assertTrue(action["follow_up_due"])
            self.assertIn("Required actions missing from report", self.h.run_status(rid)["failures"])
        items = self.h.current_items(True, "2026-10-31T09:00:00Z")
        self.assertTrue(next(x for x in items if x.get("action") == "A-a")["follow_up_due"])

    def test_snooze_quiet_blocked_and_resolved_actions_do_not_chase(self):
        a = self.add()
        a["snooze_until"] = "2026-11-15T09:00:00Z"
        self.assertFalse(reminder_due(a, "2026-11-01T09:00:00Z"))
        a.pop("snooze_until")
        for mode in ["quiet", "blocked", "review", "handoff"]:
            a["follow_up"]["mode"] = mode
            self.assertFalse(reminder_due(a, "2026-11-01T09:00:00Z"))
        a["follow_up"]["mode"] = "reminder"
        for status in ["done", "dismissed"]:
            a["status"] = status
            self.assertFalse(reminder_due(a, "2026-11-01T09:00:00Z"))

    def test_new_communication_explicitly_resets_anchor_not_routine_review(self):
        a = self.add()
        a["follow_up"]["anchor_at"] = "2026-10-29T12:00:00Z"
        c = self.h.case("A")
        self.h.update({**self.payload, "expected_revision": 1, "idempotency_key": "new-message",
                       "actions": [a]})
        self.assertEqual(due_at(self.h.case("A")["actions"][0]), "2026-11-05T12:00:00+00:00")

    def test_omitting_followup_in_replacement_preserves_existing_clock(self):
        a = self.add()
        prior = copy.deepcopy(a["follow_up"])
        a.pop("follow_up")
        self.h.update({**self.payload, "expected_revision": 1, "idempotency_key": "replace", "actions": [a]})
        self.assertEqual(self.h.case("A")["actions"][0]["follow_up"], prior)

    def test_promises_and_context_override_default_without_early_chase(self):
        a = self.add()
        a["promised_at"] = "2026-10-26T17:00:00Z"
        self.assertEqual(due_at(a), "2026-10-26T17:00:00+00:00")
        a["follow_up"].update(interval_days=10, basis="working", due_at="2026-11-09T09:00:00Z")
        self.assertFalse(reminder_due(a, "2026-11-06T09:00:00Z"))
        self.assertTrue(reminder_due(a, "2026-11-09T09:00:00Z"))

    def test_invalid_followup_rejected_atomically(self):
        a = self.add()
        a["follow_up"]["interval_days"] = 0
        with self.assertRaises(ValueError):
            self.h.update({**self.payload, "expected_revision": 1, "idempotency_key": "bad", "actions": [a]})
        self.assertEqual(self.h.case("A")["revision"], 1)
