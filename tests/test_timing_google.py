import copy, json, shutil, subprocess, tempfile, unittest
from pathlib import Path
from monitor import timing, google_calendar as google
from monitor.core import Harness, dumps


class TimingTests(unittest.TestCase):
    def test_bst_gmt_and_expired_continuations(self):
        self.assertIn("BYHOUR=13,18", timing.plan(at="2026-10-13T09:00:00Z")["rrule"])
        self.assertIn("BYHOUR=14,19", timing.plan(at="2026-10-26T09:00:00Z")["rrule"])
        self.assertEqual(
            [x["time"] for x in timing.plan(at="2026-10-13T14:00:00Z")["eligible"]], ["19:00"]
        )
        self.assertFalse(timing.plan(at="2026-10-13T19:00:00Z")["eligible"])
        self.assertFalse(timing.plan(day="2026-10-14", at="2026-10-13T09:00:00Z")["eligible"])

    def test_clock_changes(self):
        with self.assertRaises(ValueError):
            timing.london_instant("2026-10-25", "01:30")
        self.assertNotEqual(
            timing.london_instant("2026-10-25", "01:30", 0),
            timing.london_instant("2026-10-25", "01:30", 1),
        )
        with self.assertRaises(ValueError):
            timing.london_instant("2026-03-29", "01:30")

    def test_exact_readback(self):
        p = timing.plan("2026-10-14", ["09:00"], "reminder", "2026-10-13T20:00:00Z")
        self.assertTrue(timing.verify({"plan": p, "actual_rrule": p["rrule"]})["verified"])
        with self.assertRaises(ValueError):
            timing.verify({"plan": p, "actual_rrule": "FREQ=DAILY;COUNT=1;BYHOUR=9;BYMINUTE=0"})
        altered = copy.deepcopy(p)
        altered["eligible"][0]["time"] = "10:00"
        with self.assertRaises(ValueError):
            timing.verify({"plan": altered, "actual_rrule": p["rrule"]})
        self.assertIsNone(
            timing.plan("2026-10-16", ["09:00"], "reminder", "2026-10-13T20:00:00Z")["rrule"]
        )
        self.assertIsNone(
            timing.plan("2026-10-14", ["09:15", "10:30"], "reminder", "2026-10-13T20:00:00Z")[
                "rrule"
            ]
        )


class GoogleTests(unittest.TestCase):
    config = {"calendar:primary": "primary", "calendar:shared": "flat-510"}

    def export(self):
        p = google.prepare(self.config, "2026-10-13")
        p.update(started_at="2026-10-13T09:00:00Z", completed_at="2026-10-13T09:01:00Z")
        for s in p["sources"]:
            s.update(
                complete=True,
                pages=[
                    dict(
                        requested_page_token=None,
                        next_page_token=None,
                        events=[],
                        fetched_at="2026-10-13T09:00:30Z",
                    )
                ],
            )
        return p

    def event(self):
        return dict(
            id="one",
            summary="Appointment",
            start={"dateTime": "2026-10-14T10:00:00+01:00"},
            end={"dateTime": "2026-10-14T11:00:00+01:00"},
        )

    def test_empty_and_authentication(self):
        p = self.export()
        self.assertTrue(google.normalize(p, self.config)["fully_verified"])
        p["sources"][0].update(
            pages=[],
            complete=False,
            error="Authentication unavailable",
            error_code="authentication-unavailable",
        )
        r = google.normalize(p, self.config)
        self.assertFalse(r["fully_verified"])
        self.assertEqual(r["sources"][0]["access"], "unavailable")
        self.assertTrue(r["sources"][1]["complete"])

    def test_connector_strings_preserve_raw_evidence_and_date_semantics(self):
        p = self.export()
        p['sources'][0]['pages'][0]['events'] = [
            dict(id='timed', start='2026-10-14T09:00:00Z', end='2026-10-14T10:00:00Z'),
            dict(id='day', start='2026-10-15', end='2026-10-16'),
            dict(id='cancelled', status='cancelled', start='2026-10-14T12:00:00+01:00', end='2026-10-14T13:00:00+01:00'),
        ]
        original = copy.deepcopy(p)
        with tempfile.TemporaryDirectory() as tmp:
            h = Harness(Path(tmp)/'monitor.db', create=True)
            try:
                rid = h.start_run(at='2026-10-13T08:00:00Z')['id']
                result = google.ingest(h, rid, p, self.config)
                self.assertTrue(result['fully_verified'])
                source = result['sources'][0]
                self.assertFalse(source['events'][0]['all_day'])
                self.assertEqual(source['events'][0]['start'], '2026-10-14T09:00:00+00:00')
                self.assertTrue(source['events'][1]['all_day'])
                self.assertEqual(source['events'][1]['end'], '2026-10-16T00:00:00+01:00')
                self.assertEqual(len(source['explicit_cancellations']), 1)
                raw = json.loads(h.db.execute('SELECT body FROM evidence WHERE id=?', (source['evidence_ids'][0],)).fetchone()[0])
                self.assertEqual(raw, original)
                self.assertEqual(p, original)
            finally:
                h.close()

    def test_invalid_connector_strings_remain_partial(self):
        for value in ('2026-10-14T10:00:00', 'not-a-date', '2026-02-30', 123, None):
            with self.subTest(value=value):
                p = self.export()
                e = self.event()
                e['start'] = value
                p['sources'][0]['pages'][0]['events'] = [e]
                result = google.normalize(p, self.config)
                self.assertFalse(result['sources'][0]['complete'])
                self.assertTrue(result['sources'][1]['complete'])

    def test_paging(self):
        p = self.export()
        page = p["sources"][0]["pages"][0]
        page["next_page_token"] = "next"
        self.assertFalse(google.normalize(p, self.config)["fully_verified"])
        second = copy.deepcopy(page)
        second.update(requested_page_token="next", next_page_token=None)
        p["sources"][0]["pages"].append(second)
        self.assertTrue(google.normalize(p, self.config)["fully_verified"])
        second["requested_page_token"] = "wrong"
        self.assertFalse(google.normalize(p, self.config)["fully_verified"])

    def test_all_day_recurrence_and_cancellations(self):
        p = self.export()
        instance = self.event()
        instance["recurringEventId"] = "series"
        cancelled = self.event()
        cancelled.update(id="cancelled", status="cancelled")
        p["sources"][0]["pages"][0]["events"] = [
            instance,
            dict(id="day", start={"date": "2026-10-14"}, end={"date": "2026-10-16"}),
            cancelled,
        ]
        r = google.normalize(p, self.config)
        self.assertTrue(r["fully_verified"])
        s = r["sources"][0]
        self.assertEqual(len(s["events"]), 2)
        self.assertEqual(len(s["explicit_cancellations"]), 1)
        self.assertEqual(s["events"][0]["end"], "2026-10-16T00:00:00+01:00")

    def test_invalid_events_are_incomplete(self):
        for change in ("series", "naive", "outside"):
            p = self.export()
            e = self.event()
            if change == "series":
                e["recurrence"] = ["RRULE:FREQ=DAILY"]
            if change == "naive":
                e["start"]["dateTime"] = "2026-10-14T10:00:00"
            if change == "outside":
                e["start"]["dateTime"] = "2026-11-14T10:00:00+00:00"
                e["end"]["dateTime"] = "2026-11-14T11:00:00+00:00"
            p["sources"][0]["pages"][0]["events"] = [e]
            self.assertFalse(google.normalize(p, self.config)["fully_verified"], change)

    def test_dst_window_and_identity(self):
        p = google.prepare(self.config, "2026-10-24")
        self.assertTrue(p["start"].endswith("+01:00"))
        self.assertTrue(p["end"].endswith("+00:00"))
        p = self.export()
        p["sources"][0]["calendar_id"] = "deprecated-IM"
        with self.assertRaises(ValueError):
            google.normalize(p, self.config)

    def test_capture_times_and_terminal_cursor(self):
        p = self.export()
        p["sources"][0]["pages"].append(copy.deepcopy(p["sources"][0]["pages"][0]))
        self.assertFalse(google.normalize(p, self.config)["fully_verified"])
        p = self.export()
        p["sources"][0]["pages"][0]["fetched_at"] = "2026-10-13T08:00:00Z"
        self.assertFalse(google.normalize(p, self.config)["fully_verified"])
        p = self.export()
        p["completed_at"] = "2026-10-13T09:01:00"
        with self.assertRaises(ValueError):
            google.normalize(p, self.config)

    def test_ingest_and_cancellation_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = Harness(Path(tmp) / "monitor.db", create=True)
            try:
                rid = h.start_run(at="2026-10-13T08:00:00Z")["id"]
                p = self.export()
                e = self.event()
                e["status"] = "cancelled"
                p["sources"][0]["pages"][0]["events"] = [e]
                google.ingest(h, rid, p, self.config)
                self.assertEqual(h.db.execute("SELECT count(*) FROM sources").fetchone()[0], 2)
                self.assertEqual(h.db.execute("SELECT count(*) FROM evidence").fetchone()[0], 1)
                self.assertEqual(
                    len([r for r in h.report_items(rid) if r["type"] == "cancellation"]), 1
                )
                with h.db:
                    h.db.execute(
                        "INSERT INTO calendar_state VALUES(?,?,?)",
                        (
                            p["sources"][0]["source"],
                            "old",
                            dumps(
                                {
                                    "events": [
                                        dict(
                                            id="one",
                                            title="Appointment",
                                            start="2026-10-14T10:00:00+01:00",
                                            end="2026-10-14T11:00:00+01:00",
                                        )
                                    ]
                                }
                            ),
                        ),
                    )
                self.assertEqual(
                    len([r for r in h.report_items(rid) if r["type"] == "cancellation"]), 1
                )
                newer = h.start_run(at="2026-10-13T10:00:00Z")["id"]
                with self.assertRaises(ValueError):
                    google.ingest(h, newer, p, self.config)
            finally:
                h.close()

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_connector_collector_faults(self):
        p = subprocess.run(
            ["node", "tests/Test-GoogleCollector.js"],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)["scenarios"], 6)
