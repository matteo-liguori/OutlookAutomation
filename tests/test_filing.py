import tempfile
import unittest
from pathlib import Path
from monitor.core import Harness, MAIL
from monitor.filing import record, status


class FilingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.h = Harness(Path(self.tmp.name) / "monitor.db", create=True)
        self.h.put_evidence({"id": "filing-proof", "body": "Verified mailbox collection and moves"})

    def tearDown(self):
        self.h.close()
        self.tmp.cleanup()

    def result(self, **changes):
        p = dict(
            source=MAIL[0],
            status="complete",
            complete=True,
            since="2026-09-10T09:00:00Z",
            until="2026-09-17T09:00:00Z",
            completed_at="2026-09-17T09:05:00Z",
            reviewed_count=4,
            moved_count=2,
            verified_move_count=2,
            failed_count=0,
            unread_preserved=True,
            evidence_ids=["filing-proof"],
            provenance="command",
        )
        p.update(changes)
        return p

    def test_partial_does_not_advance_success_and_sources_are_independent(self):
        record(self.h, self.result())
        record(
            self.h,
            self.result(
                status="partial",
                complete=False,
                failed_count=1,
                until="2026-09-18T09:00:00Z",
                since="2026-09-17T09:00:00Z",
                completed_at="2026-09-18T09:05:00Z",
                verified_move_count=1,
            ),
        )
        rows = status(self.h, "2026-09-18T10:00:00Z")
        self.assertEqual(rows[0]["filed_through"], "2026-09-17T09:00:00+00:00")
        self.assertEqual(rows[0]["last_attempt_status"], "partial")
        self.assertIsNone(rows[1]["last_successful_at"])
        self.assertEqual(rows[0]["since"], rows[0]["filed_through"])

    def test_gap_is_capped_and_invalid_success_is_rejected(self):
        record(self.h, self.result())
        row = status(self.h, "2026-09-30T09:00:00Z")[0]
        self.assertEqual(row["since"], "2026-09-23T09:00:00+00:00")
        self.assertTrue(row["older_gap_outside_scope"])
        for patch in (
            {"since": "2026-09-09T09:00:00Z"},
            {"unread_preserved": False},
            {"verified_move_count": 1},
            {"evidence_ids": ["missing"]},
            {"failed_count": 1},
        ):
            with self.assertRaises(ValueError):
                record(self.h, self.result(**patch))
        with self.assertRaises(ValueError):
            record(
                self.h,
                self.result(
                    since="2026-09-18T09:00:00Z",
                    until="2026-09-19T09:00:00Z",
                    completed_at="2026-09-19T09:05:00Z",
                ),
            )

    def test_briefing_and_new_run_require_each_mailbox_filing(self):
        record(self.h, self.result())
        reminders = [
            x
            for x in self.h.current_items(True, "2026-09-18T09:00:00Z")
            if x.get("type") == "filing_checkpoint"
        ]
        self.assertEqual(len(reminders), 3)
        run = self.h.start_run(at="2026-09-18T09:00:00Z")
        self.assertEqual(len(run["filing_windows"]), 3)
        self.assertEqual(
            sum("filing incomplete" in x for x in self.h.run_status(run["id"])["failures"]), 3
        )
        for source in MAIL:
            since = next(x["since"] for x in run["filing_windows"] if x["source"] == source)
            record(
                self.h,
                self.result(
                    source=source,
                    since=since,
                    until="2026-09-18T09:00:00Z",
                    completed_at="2026-09-18T09:05:00Z",
                    reviewed_count=0,
                    moved_count=0,
                    verified_move_count=0,
                ),
            )
        self.assertFalse(
            any("filing incomplete" in x for x in self.h.run_status(run["id"])["failures"])
        )

    def test_retry_and_regression(self):
        p = self.result()
        record(self.h, p)
        self.assertTrue(record(self.h, p)["unchanged"])
        with self.assertRaises(ValueError):
            record(
                self.h,
                self.result(until="2026-09-16T09:00:00Z", completed_at="2026-09-18T09:00:00Z"),
            )


if __name__ == "__main__":
    unittest.main()
