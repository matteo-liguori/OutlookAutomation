"""Permissions have independent scope, lifecycle and user-confirmed monthly reviews."""

import copy
import tempfile
import unittest
from pathlib import Path

from monitor.core import Harness
from monitor.approvals import put, review, flag, items, find, next_monthly, lifecycle
from monitor.evidence import prune
from monitor.threadcache import sanitize_backup


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.h = Harness(Path(self.tmp.name)/'monitor.db', create=True)
        self.h.put_evidence(dict(id='user', source='user', body='Exact permission and retention confirmation'))

    def tearDown(self):
        self.h.close(); self.tmp.cleanup()

    def permission(self, kind='standing', **overrides):
        p = dict(id='permission', kind=kind, title='Exact permission', summary='Perform only the identified operation.',
                 granted_at='2026-08-31T09:00:00+01:00', evidence_ids=['user'])
        if kind == 'standing':
            p['scope'] = dict(operation='calendar-create', target='one exact calendar',
                              trigger='Confirmed service visit', boundaries='No other writes')
        else:
            p['message'] = dict(sender='me@example.invalid', to=['you@example.invalid'], cc=[], bcc=[],
                                subject='Approved', body='Exact wording', attachments=[])
        p.update(overrides)
        return p

    def test_independent_tables_and_reading_never_renews(self):
        put(self.h, self.permission())
        put(self.h, self.permission('one-off', id='once'))
        before = find(self.h, 'permission')[2]
        self.assertEqual(self.h.db.execute('SELECT count(*) FROM standing_approvals').fetchone()[0], 1)
        self.assertEqual(self.h.db.execute('SELECT count(*) FROM approvals').fetchone()[0], 1)
        self.assertEqual(len(items(self.h, at='2026-09-30T10:00:00Z', due_only=True)), 2)
        self.h.current_items(briefing=True)
        self.assertEqual(find(self.h, 'permission')[2], before)
        self.assertIsNone(self.h.db.execute('SELECT 1 FROM actions').fetchone())

    def test_anniversary_short_months_and_london_dst(self):
        grant = '2026-08-31T09:00:00+01:00'
        self.assertEqual(next_monthly(grant, grant), '2026-09-30T08:00:00+00:00')
        self.assertEqual(next_monthly(grant, '2026-10-02T12:00:00Z'), '2026-10-31T09:00:00+00:00')
        self.assertEqual(next_monthly('2024-01-31T09:00:00Z','2024-01-31T09:00:00Z'), '2024-02-29T09:00:00+00:00')

    def test_user_keep_advances_anniversary_without_scope_change(self):
        put(self.h, self.permission()); before = find(self.h, 'permission')[2]
        result = review(self.h, dict(id='permission', expected_revision=1, decision='keep', reason='User explicitly keeps it', evidence_ids=['user']), at='2026-10-02T12:00:00Z')
        after = find(self.h, 'permission')[2]
        self.assertEqual(after['scope'], before['scope'])
        self.assertEqual(after['granted_at'], before['granted_at'])
        self.assertEqual(result['next_review_at'], '2026-10-31T09:00:00+00:00')
        with self.assertRaisesRegex(ValueError, 'Stale'):
            review(self.h, dict(id='permission', expected_revision=1, decision='keep', reason='Stale reply', evidence_ids=['user']))

    def test_unknown_grant_due_now_not_migration_date(self):
        put(self.h, self.permission(granted_at=None))
        self.assertIsNone(find(self.h, 'permission')[2]['granted_at'])
        self.assertTrue(items(self.h, due_only=True)[0]['review_due'])
        review(self.h, dict(id='permission', expected_revision=1, decision='keep', reason='Explicit user reconfirmation', evidence_ids=['user']), at='2026-10-26T19:00:00Z')
        self.assertEqual(find(self.h, 'permission')[2]['next_review_at'], '2026-11-26T19:00:00+00:00')

    def test_early_material_change_flags_only_this_permission(self):
        put(self.h, self.permission(granted_at='2026-10-20T12:00:00Z'))
        put(self.h, self.permission(id='other', granted_at='2026-10-20T12:00:00Z'))
        other = find(self.h, 'other')[2]
        flag(self.h, dict(id='permission', expected_revision=1, reason='Recipient identity is now uncertain', suspended=True, evidence_ids=['user']))
        self.assertTrue(find(self.h, 'permission')[2]['suspended'])
        self.assertEqual(find(self.h, 'other')[2], other)
        self.assertEqual([a['id'] for a in items(self.h, due_only=True)], ['permission'])

    def test_scope_and_timers_cannot_be_changed_by_put(self):
        original = self.permission(); put(self.h, original)
        before = find(self.h, 'permission')[2]
        put(self.h, original)
        self.assertEqual(find(self.h, 'permission')[2]['granted_at'], before['granted_at'])
        self.assertEqual(find(self.h, 'permission')[2]['next_review_at'], before['next_review_at'])
        for change in (dict(scope={'operation':'send-anywhere'}), dict(granted_at='2026-09-01T09:00:00Z'), dict(next_review_at='2099-01-01T00:00:00Z')):
            with self.assertRaises(ValueError): put(self.h, dict(original, **change))
        p = self.permission('one-off', id='once'); put(self.h, p)
        p['message']['body'] = 'Changed wording'
        with self.assertRaises(ValueError): put(self.h, p)

    def test_retirement_requires_evidence_and_does_not_touch_sibling(self):
        put(self.h, self.permission()); put(self.h, self.permission('one-off', id='once'))
        p = dict(id='permission', expected_revision=1, decision='spent', reason='Care relationship ended', evidence_ids=['user'])
        with self.assertRaises(ValueError): review(self.h, p)
        review(self.h, dict(p, scope_ended=True))
        with self.assertRaises(ValueError): find(self.h, 'permission')
        self.assertIn('Exact wording', find(self.h, 'once')[2]['message']['body'])

    def test_case_bound_scope_closes_but_global_scope_survives(self):
        self.h.update(dict(id='A', expected_revision=0, idempotency_key='case', evidence_ids=['user'],
                           case=dict(title='Task', position='Finished', context='Outcome retained.', status='closed'), actions=[]))
        put(self.h, self.permission(case_id='A'))
        put(self.h, self.permission(id='global'))
        self.assertEqual([a['id'] for a in items(self.h)], ['global'])

    def test_pending_wording_and_permission_evidence_survive_cleanup_backup(self):
        put(self.h, self.permission()); put(self.h, self.permission('one-off', id='once'))
        with self.h.write():
            self.h.db.execute("UPDATE retention_activity SET at='2000-01-01T00:00:00Z'")
            prune(self.h)
        self.assertTrue(self.h.doctor()['ok'])
        sanitize_backup(self.h.db)
        self.assertEqual(find(self.h, 'once')[2]['message']['body'], 'Exact wording')
        self.assertEqual(find(self.h, 'permission')[2]['scope']['target'], 'one exact calendar')

    def test_due_review_is_required_in_handoff_but_delivery_does_not_renew(self):
        put(self.h, self.permission())
        rid = self.h.start_run()['id']; before = copy.deepcopy(find(self.h, 'permission')[2])
        self.assertIn('Approval reviews missing from report', self.h.run_status(rid)['failures'])
        self.h.record_run(rid, dict(kind='handoff', delivered=True, approval_ids=['permission']))
        self.assertNotIn('Approval reviews missing from report', self.h.run_status(rid)['failures'])
        self.h.finalize(rid)
        self.assertEqual(find(self.h, 'permission')[2], before)

    def test_expired_one_off_stays_protected_but_requires_immediate_review(self):
        put(self.h, self.permission('one-off', granted_at='2026-10-20T12:00:00Z', expires_at='2026-10-21T12:00:00Z'))
        due = items(self.h, due_only=True)[0]
        self.assertTrue(due['suspended'])
        self.assertEqual(find(self.h, 'permission')[2]['message']['body'], 'Exact wording')
        with self.assertRaisesRegex(ValueError, 'Expired'):
            review(self.h, dict(id='permission', expected_revision=1, decision='keep', reason='Keep', evidence_ids=['user']))

    def test_spent_one_off_cannot_be_reactivated(self):
        p = self.permission('one-off', status='sent'); put(self.h, p)
        with self.assertRaisesRegex(ValueError, 'reactivated'):
            put(self.h, dict(p, status='approved'))
        with self.assertRaisesRegex(ValueError, 'Spent'):
            review(self.h, dict(id='permission', expected_revision=1, decision='keep', reason='Keep', evidence_ids=['user']))

    def test_non_email_one_off_has_exact_scope_and_same_review_lifecycle(self):
        put(self.h, dict(id='task', kind='one-off', title='Exact non-email task', granted_at='2026-08-31T09:00:00+01:00',
                         scope={'operation':'calendar-create','event':'Specific agreed event','calendar':'Specific target'}, evidence_ids=['user']))
        self.assertEqual(items(self.h, due_only=True)[0]['id'], 'task')
        review(self.h, dict(id='task', expected_revision=1, decision='spent', reason='Task verified complete', evidence_ids=['user']))
        self.assertEqual(items(self.h), [])

    def test_failed_review_rolls_back_cross_thread_evidence_copy(self):
        self.h.update(dict(id='A', expected_revision=0, idempotency_key='a', evidence_ids=['user'],
                           case=dict(title='A', position='Open', context='Current', status='open'), actions=[]))
        put(self.h, self.permission(case_id='A'))
        before=[tuple(r) for r in self.h.db.execute('SELECT * FROM evidence ORDER BY id')]
        with self.assertRaisesRegex(ValueError, 'Stale'):
            review(self.h, dict(id='permission', expected_revision=99, decision='keep', reason='Stale', evidence_ids=['user']))
        self.assertEqual([tuple(r) for r in self.h.db.execute('SELECT * FROM evidence ORDER BY id')], before)


if __name__ == '__main__':
    unittest.main()
