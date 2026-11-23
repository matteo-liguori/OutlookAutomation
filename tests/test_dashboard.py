from datetime import date
import json
import sqlite3
import unittest

from monitor.dashboard import SCHEMA, event_identity, event_occurrence_identity, prune, record_calendar


class CalendarArchiveTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.executescript(SCHEMA)

    def test_multiday_event_preserves_exclusive_end_and_newer_verified_snapshot_wins(self):
        days = ['2026-11-23', '2026-11-24', '2026-11-25']
        birthday = dict(id='birthday', title='Birthday', all_day=True, event_type='birthday',
                        start='2026-11-23', end='2026-11-25', description='Not needed in the local calendar')
        source = 'calendar:primary'
        record_calendar(self.db, source, dict(days=days, events=[birthday]), '2026-11-23T09:00:00Z')
        events = [json.loads(row[0]) for row in self.db.execute('SELECT events FROM dashboard_calendar ORDER BY day')]
        self.assertEqual([len(day) for day in events], [1, 1, 0])
        self.assertEqual(events[0][0]['event_type'], 'birthday')
        self.assertNotIn('description', events[0][0])

        record_calendar(self.db, source, dict(days=days[1:], events=[], explicit_cancellations=[birthday]),
                        '2026-11-24T09:00:00Z')
        record_calendar(self.db, source, dict(days=days, events=[birthday]), '2026-11-23T14:00:00Z')
        events = [json.loads(row[0]) for row in self.db.execute('SELECT events FROM dashboard_calendar ORDER BY day')]
        self.assertEqual([len(day) for day in events], [1, 0, 0])

    def test_retention_keeps_only_current_365_calendar_days_and_future_coverage(self):
        record_calendar(self.db, 'calendar:work', dict(days=['2025-11-23', '2025-11-24', '2026-11-26']),
                        '2026-11-23T09:00:00Z')
        prune(self.db, date(2026, 11, 23))
        days = [row[0] for row in self.db.execute('SELECT day FROM dashboard_calendar ORDER BY day')]
        self.assertEqual(days, ['2025-11-24', '2026-11-26'])

    def test_icloud_occurrence_matches_current_and_archived_event_ids(self):
        event = dict(id='fixture:uid', calendar='Family', title='Catch-up',
                     start='2026-11-24T09:00:00+00:00', end='2026-11-24T10:00:00+00:00')
        identity = event_identity(event)
        source = 'calendar:icloud'
        reviewed = event_occurrence_identity(source, event, identity)
        for suffix in ('', '2026-11-24T09:00:00+00:00'):
            archived = dict(event, id=f"Family:{event['id']}:{suffix}")
            self.assertEqual(event_occurrence_identity(source, archived, identity), reviewed)
        self.assertNotEqual(event_occurrence_identity(source, dict(event, calendar='Work'), identity), reviewed)
