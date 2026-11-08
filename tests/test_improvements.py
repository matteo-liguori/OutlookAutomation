import tempfile
import unittest
from pathlib import Path
from monitor.core import Harness
from monitor.improvements import reflect, review, items


class ImprovementTests(unittest.TestCase):
    def test_reflection_dedup_validation_and_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = Harness(Path(tmp) / "test.db", create=True)
            run = h.start_run()["id"]
            h.finalize(run)
            finding = {
                "id": "test-observation",
                "observation": "Observed friction",
                "proposal": "Consider a change",
                "validation": "Replay the failure",
                "references": ["run:" + run],
            }
            before = h.current_items(briefing=True)
            reflect(h, run, {"findings": [finding]})
            reflect(h, run, {"findings": [finding]})
            self.assertEqual(items(h)[0]["occurrences"], 1)
            self.assertEqual(before, h.current_items(briefing=True))
            review(
                h,
                {
                    "id": finding["id"],
                    "expected_revision": 1,
                    "status": "deferred",
                    "reason": "Needs review",
                },
            )
            with self.assertRaises(ValueError):
                review(
                    h,
                    {
                        "id": finding["id"],
                        "expected_revision": 1,
                        "status": "accepted",
                        "reason": "Stale",
                    },
                )
            run2 = h.start_run()["id"]
            h.finalize(run2)
            reflect(h, run2, {"findings": [finding]})
            self.assertEqual(items(h)[0]["occurrences"], 2)
            self.assertEqual(items(h)[0]["status"], "deferred")
            run3 = h.start_run()["id"]
            h.finalize(run3)
            with self.assertRaises(ValueError):
                reflect(h, run3, {"findings": [{**finding, "proposal": "x" * 601}]})
            reflect(h, run3, {"findings": []})
            h.close()
            h = Harness(Path(tmp) / "test.db")
            self.assertEqual(len(items(h)), 1)
            h.close()
