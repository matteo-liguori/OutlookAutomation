"""Regression cases from the code review; all databases and messages are synthetic."""

import copy, json, sqlite3, tempfile, unittest
from pathlib import Path
from monitor.core import Harness, CAL, MAIL
from monitor.collect import import_pages
from monitor.messages import identity
from monitor.sources import MAIL_STORES


class SimplificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.h = Harness(Path(self.tmp.name) / "monitor.db", create=True)
        self.h.put_evidence({"id": "e", "source": "test", "body": "Fixture source"})

    def tearDown(self):
        self.h.close()
        self.tmp.cleanup()

    def new_run(self, at):
        return self.h.start_run(at=at)["id"]

    def event(self):
        return dict(
            id="event",
            title="Appointment",
            start="2026-10-14T10:00:00Z",
            end="2026-10-14T11:00:00Z",
        )

    def calendar(self, rid, events):
        self.h.record_run(
            rid,
            dict(
                kind="source",
                source=CAL[0],
                status="current",
                complete=True,
                evidence_ids=["e"],
                provenance="command",
                events=events,
                days=["2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16"],
            ),
        )

    def handoff(self, rid, events=(), cancellations=()):
        self.h.record_run(
            rid,
            dict(
                kind="handoff",
                delivered=True,
                event_ids=list(events),
                cancellation_ids=list(cancellations),
                days=["2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16"],
            ),
        )

    def mail(self):
        page = dict(
            Store=MAIL_STORES["mail:work"],
            StoreId="store",
            Offset=0,
            FirstId="entry",
            SnapshotId="snapshot",
            StartedAt="2026-10-13T08:00:00Z",
            Since="2026-10-11T08:00:00Z",
            Until="2026-10-13T08:00:00Z",
            Complete=True,
            NextOffset=None,
            Errors=[],
            Items=[
                dict(
                    StoreId="store",
                    EntryId="entry",
                    Received="2026-10-13T07:30:00Z",
                    MessageClass="IPM.Note",
                    Subject="Original",
                    SenderEmail="test@example.invalid",
                )
            ],
        )
        fresh = dict(
            Store=page["Store"],
            LatestStoreId="store",
            CheckedAt="2026-10-13T08:02:00Z",
            LatestReceived="2026-10-13T07:30:00Z",
            Status="current",
            Current=True,
        )
        return page, fresh

    def test_older_partial_cannot_regress_source_or_calendar(self):
        old = self.new_run("2026-10-13T08:00:00Z")
        self.calendar(old, [])
        self.h.finalize(old)
        new = self.new_run("2026-10-13T12:00:00Z")
        self.calendar(new, [self.event()])
        self.h.finalize(new)
        with self.assertRaises(ValueError):self.h.finalize(old)
        self.assertEqual(
            self.h.db.execute(
                "SELECT reviewed FROM checkpoints WHERE source=?", (CAL[0],)
            ).fetchone()[0],
            "2026-10-13T12:00:00+00:00",
        )
        state = json.loads(
            self.h.db.execute(
                "SELECT details FROM calendar_state WHERE source=?", (CAL[0],)
            ).fetchone()[0]
        )
        self.assertEqual(state["events"], [self.event()])

    def test_unreported_cancellation_survives_partial_then_acknowledges(self):
        baseline = self.new_run("2026-10-13T06:00:00Z")
        self.calendar(baseline, [self.event()])
        self.handoff(baseline, [CAL[0] + ":event"])
        self.h.finalize(baseline)
        partial = self.new_run("2026-10-13T08:00:00Z")
        self.calendar(partial, [])
        self.h.finalize(partial)
        later = self.new_run("2026-10-13T12:00:00Z")
        self.calendar(later, [])
        self.assertTrue(any(i["type"] == "cancellation" for i in self.h.report_items(later)))
        self.handoff(later, cancellations=[CAL[0] + ":event"])
        self.h.finalize(later)
        newest = self.new_run("2026-10-13T14:00:00Z")
        self.calendar(newest, [])
        self.assertFalse(any(i["type"] == "cancellation" for i in self.h.report_items(newest)))

    def test_changed_candidate_invalidates_previous_review_without_losing_proof(self):
        rid = self.new_run("2026-10-13T08:00:00Z")
        page, fresh = self.mail()
        import_pages(self.h, rid, MAIL[0], [page], "2026-10-13T08:00:00Z", fresh)
        candidate = json.loads(
            self.h.db.execute("SELECT details FROM candidates WHERE run_id=?", (rid,)).fetchone()[0]
        )
        self.h.record_run(
            rid,
            dict(
                id=candidate["id"],
                kind="candidate",
                source=MAIL[0],
                disposition="routine",
                review_reason="Original reviewed",
                evidence_ids=["e"],
            ),
        )
        page["Items"][0]["Subject"] = "Changed content"
        import_pages(self.h, rid, MAIL[0], [page], "2026-10-13T08:00:00Z", fresh)
        updated = json.loads(
            self.h.db.execute("SELECT details FROM candidates WHERE run_id=?", (rid,)).fetchone()[0]
        )
        self.assertFalse(self.h.candidate_complete(updated))
        self.assertFalse(
            self.h.db.execute("SELECT 1 FROM sqlite_master WHERE name='history'").fetchone()
        )

    def test_identity_shared_by_direct_and_page_recording(self):
        rid = self.new_run("2026-10-13T08:00:00Z")
        page, fresh = self.mail()
        import_pages(self.h, rid, MAIL[0], [page], "2026-10-13T08:00:00Z", fresh)
        self.h.record_run(
            rid,
            dict(
                kind="candidate",
                source=MAIL[0],
                entry_id="entry",
                store_id="store",
                evidence_ids=["e"],
            ),
        )
        self.assertEqual(
            self.h.db.execute("SELECT count(*) FROM candidates WHERE run_id=?", (rid,)).fetchone()[
                0
            ],
            1,
        )

    def test_wrong_store_window_and_stale_freshness_rejected(self):
        rid = self.new_run("2026-10-13T08:00:00Z")
        page, fresh = self.mail()
        wrong = copy.deepcopy(page)
        wrong["Store"] = "wrong-store@example.invalid"
        with self.assertRaises(ValueError):
            import_pages(self.h, rid, MAIL[0], [wrong], "2026-10-13T08:00:00Z", fresh)
        wrong = copy.deepcopy(page)
        wrong["Items"][0]["Received"] = "2026-10-01T00:00:00Z"
        with self.assertRaises(ValueError):
            import_pages(self.h, rid, MAIL[0], [wrong], "2026-10-13T08:00:00Z", fresh)
        fresh["CheckedAt"] = "2026-10-12T08:00:00Z"
        result = import_pages(self.h, rid, MAIL[0], [page], "2026-10-13T08:00:00Z", fresh)
        self.assertEqual(result["status"], "unavailable")

    def test_identical_import_does_not_duplicate_evidence(self):
        rid = self.new_run("2026-10-13T08:00:00Z")
        page, fresh = self.mail()
        for _ in range(2):
            import_pages(self.h, rid, MAIL[0], [page], "2026-10-13T08:00:00Z", fresh)
        self.assertEqual(self.h.db.execute("SELECT count(*) FROM evidence").fetchone()[0], 3)

    def test_new_waiting_case_is_reported_even_before_run_start(self):
        self.h.update(
            dict(
                id="new",
                expected_revision=0,
                idempotency_key="new",
                evidence_ids=["e"],
                case=dict(title="New matter", position="Unreported development"),
                actions=[
                    dict(
                        id="wait",
                        status="waiting",
                        owner="Other party",
                        next_step="Await reply",
                        completion_condition="Reply",
                        reopening_condition="New issue",
                    )
                ],
            )
        )
        rid = self.new_run("2026-10-13T08:00:00Z")
        self.assertTrue(
            any(i["type"] == "case" and i["id"] == "new" for i in self.h.report_items(rid))
        )

    def test_no_run_case_copies_and_batch_records_atomic(self):
        self.test_new_waiting_case_is_reported_even_before_run_start()
        rid = self.new_run("2026-10-13T12:00:00Z")
        self.assertIsNone(
            self.h.db.execute("SELECT name FROM sqlite_master WHERE name='run_cases'").fetchone()
        )
        with self.assertRaises(ValueError):
            self.h.record_runs(
                rid,
                [
                    dict(kind="candidate", source=MAIL[0], entry_id="good", evidence_ids=["e"]),
                    dict(kind="source", source="deprecated", evidence_ids=["e"]),
                ],
            )
        self.assertFalse(
            self.h.db.execute("SELECT 1 FROM candidates WHERE run_id=?", (rid,)).fetchone()
        )

    def test_recording_does_not_revalidate_entire_run(self):
        rid = self.new_run("2026-10-13T08:00:00Z")

        def forbidden(_):
            raise AssertionError("Full validation should be explicit")

        self.h.run_status = forbidden
        self.h.record_runs(
            rid,
            [
                dict(kind="candidate", source=MAIL[0], entry_id=str(n), evidence_ids=["e"])
                for n in range(30)
            ],
        )


    def test_single_page_reads_do_not_create_disposable_database_copies(self):
        self.h.page("small", [{"value": 1}])
        self.assertFalse(self.h.db.execute("SELECT 1 FROM sqlite_master WHERE name='snapshots'").fetchone())
        self.assertTrue(self.h.page("paged", [{"value": 1}, {"value": 2}], limit=1)["next_cursor"])

    def test_older_partial_cannot_overwrite_a_newer_message_review(self):
        runs = []
        for hour, label in [(8, "old"), (12, "new")]:
            rid = self.new_run(f"2026-10-13T{hour:02}:00:00Z")
            self.h.record_run(
                rid,
                dict(
                    kind="gate",
                    name="refresh",
                    success=True,
                    completed_at=f"2026-10-13T{hour:02}:00:00Z",
                    evidence_ids=["e"],
                ),
            )
            self.h.record_run(
                rid,
                dict(
                    kind="candidate",
                    id="same-message",
                    source=MAIL[0],
                    entry_id="same",
                    disposition="routine",
                    review_reason=label,
                    evidence_ids=["e"],
                ),
            )
            self.h.record_run(
                rid,
                dict(
                    kind="source",
                    source=MAIL[0],
                    status="current",
                    complete=True,
                    count=1,
                    candidate_count=1,
                    newest="2026-10-13T07:00:00Z",
                    since="2026-10-11T08:00:00Z",
                    until=f"2026-10-13T{hour:02}:00:00Z",
                    started_at=f"2026-10-13T{hour:02}:00:00Z",
                    freshness_evidence_ids=["e"],
                    evidence_ids=["e"],
                    provenance="command",
                ),
            )
            self.h.finalize(rid)
            runs.append(rid)
        with self.assertRaises(ValueError):self.h.finalize(runs[0])
        review = json.loads(
            self.h.db.execute(
                "SELECT details FROM message_reviews WHERE id='same-message'"
            ).fetchone()[0]
        )
        self.assertEqual(review["review_reason"], "new")
