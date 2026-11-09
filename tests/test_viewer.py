"""Current-only projections and HTTP reads must not mutate operational records."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from monitor.core import Harness
from monitor.viewer import ReadStore, overview, detail, colour, action_dates

AT = '2026-11-09T12:00:00+00:00'


class ViewerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'records.db'
        self.h = Harness(self.path, create=True)
        self.h.put_evidence(dict(id='fixture', source='user', body='Synthetic fixture'))
        self.create('alpha')

    def tearDown(self):
        self.h.close()
        self.tmp.cleanup()

    def create(self, cid, **overrides):
        action = dict(id=cid+'-action', status='waiting', owner='Other person', next_step='Await a reply',
                      completion_condition='Reply received', reopening_condition='Further question',
                      deadline='2026-11-12T16:00:00+00:00',
                      follow_up=dict(mode='reminder', due_at='2026-11-10T09:00:00+00:00',
                                     reason='Check for a reply', evidence_ids=['fixture']))
        action.update(overrides)
        self.h.update(dict(id=cid, expected_revision=0, idempotency_key='create-'+cid, evidence_ids=['fixture'],
                           case=dict(title=cid+' <script>alert(1)</script>', purpose='Recognise this conversation',
                                     context='Private card context', position='Waiting for an answer', status='open'),
                           actions=[action]))

    def dump(self):
        return '\n'.join(self.h.db.iterdump())

    def test_grid_and_detail_use_current_records_without_any_writes(self):
        before = self.dump()
        cards = overview(self.path, AT)
        card = detail(self.path, 'alpha')
        self.assertEqual(before, self.dump())
        self.assertEqual(cards['cards'][0]['purpose'], card['purpose'])
        self.assertEqual(cards['cards'][0]['colour'], card['colour'])
        self.assertNotIn('context', cards['cards'][0])
        self.assertEqual(card['context'], 'Private card context')
        self.assertTrue(cards['read_only'])
        self.assertEqual(card['revision'], self.h.case('alpha')['revision'])
        store = ReadStore(self.path)
        try:
            with self.assertRaises(sqlite3.OperationalError):store.db.execute("DELETE FROM cases")
        finally:store.close()

    def test_closed_cards_and_resolved_actions_disappear_without_history(self):
        c = self.h.case('alpha')
        action = dict(c['actions'][0], status='done', completion_reason='Synthetic completion', completion_evidence=['fixture'])
        self.h.update(dict(id='alpha', expected_revision=c['revision'], idempotency_key='close',
                           evidence_ids=['fixture'], case=dict(title=c['title'], context='Closed',position='Resolved',status='closed'), actions=[action]))
        self.assertEqual(overview(self.path, AT)['cards'], [])
        self.assertEqual(overview(self.path, AT)['events'], [])
        with self.assertRaisesRegex(ValueError, 'no longer open'):detail(self.path,'alpha')

    def test_timeline_preserves_distinct_clocks_sorted_and_grouped(self):
        result = overview(self.path, AT)
        events = result['events']
        self.assertEqual([e['at'] for e in events], sorted(e['at'] for e in events))
        self.assertTrue(any('deadline' in e['kinds'] for e in events))
        self.assertTrue(any('follow-up' in e['kinds'] for e in events))
        action = dict(self.h.case('alpha')['actions'][0], next_review='2026-11-10T09:00:00+00:00')
        grouped = action_dates(action,AT)
        self.assertIn(('2026-11-10T09:00:00+00:00',['follow-up','review']), grouped)
        self.assertTrue(all(e['case_id']=='alpha' for e in events))

    def test_snooze_suppresses_early_checks_but_does_not_move_deadline(self):
        action = dict(self.h.case('alpha')['actions'][0], snooze_until='2026-11-15T09:00:00+00:00')
        dates = action_dates(action,AT)
        self.assertIn(('2026-11-12T16:00:00+00:00',['deadline']), dates)
        self.assertFalse(any('follow-up' in kinds for _, kinds in dates))
        self.assertIn(('2026-11-15T09:00:00+00:00',['snooze-end']), dates)

    def test_undated_work_and_stable_colours_survive_new_cards(self):
        before = colour('alpha')
        self.create('beta', deadline=None, follow_up=dict(mode='quiet', reason='No date yet', evidence_ids=['fixture']))
        # Unknown timing is not replaced with the viewer's current time.
        with self.h.write():
            row=self.h.db.execute("SELECT details FROM actions WHERE id='beta-action'").fetchone()
            a=json.loads(row[0]);a.pop('next_review',None)
            self.h.db.execute("UPDATE actions SET next_review=NULL,details=? WHERE id='beta-action'",(json.dumps(a),))
        result=overview(self.path,AT)
        self.assertEqual(before, next(c['colour'] for c in result['cards'] if c['id']=='alpha'))
        self.assertNotEqual(colour('alpha'),colour('beta'))
        self.assertEqual(result['unscheduled'][0]['case_id'],'beta')

    def test_working_day_followup_spans_dst_using_existing_harness_rule(self):
        action = dict(id='a', status='waiting', follow_up=dict(mode='reminder',anchor_at='2026-10-23T08:00:00Z',interval_days=1,basis='working'))
        self.assertEqual(action_dates(action,AT), [('2026-10-26T09:00:00+00:00',['follow-up'])])

    def test_review_uses_current_clock_not_old_tracking_anchor(self):
        action = dict(id='a',status='watch',next_review='2026-11-16T09:00:00Z',
                      follow_up=dict(mode='review',anchor_at='2026-09-01T09:00:00Z',interval_days=7))
        self.assertEqual(action_dates(action,AT), [('2026-11-16T09:00:00+00:00',['review'])])

    def test_grid_sorts_by_message_dates_not_edits_or_cache_access(self):
        from monitor.threadcache import put_message
        from monitor.card_input import apply
        self.create('beta');self.create('gamma')
        for cid, received in [('alpha','2026-11-08T10:00:00Z'),('beta','2026-11-09T09:00:00Z')]:
            put_message(self.h,dict(case_id=cid,message=dict(EntryId=cid,StoreId='fixture',Received=received,Body='Synthetic mail')))
        apply(self.h,dict(id='alpha',expected_revision=self.h.case('alpha')['revision'],idempotency_key='rename-old',title='Recently renamed'))
        # Re-reading the older email must not make it newer than beta.
        put_message(self.h,dict(case_id='alpha',message=dict(EntryId='alpha',StoreId='fixture',Received='2026-11-08T10:00:00Z',Body='Synthetic mail')))
        result=overview(self.path,AT)
        self.assertEqual([c['id'] for c in result['cards']],['beta','alpha','gamma'])
        self.assertEqual(result['cards'][0]['latest_email_at'],'2026-11-09T09:00:00+00:00')
        self.assertIsNone(result['cards'][-1]['latest_email_at'])

    def test_email_reference_fallback_ignores_review_and_note_timestamps(self):
        self.create('beta')
        evidence=self.h.put_evidence(dict(kind='mail-source-reference',source='thread-cache',case_id='alpha',
            body=json.dumps(dict(locator=dict(Received='2026-11-09 09:00:00')))))['id']
        c=self.h.case('alpha')
        with self.h.write():
            raw=json.loads(self.h.db.execute("SELECT details FROM cases WHERE id='alpha'").fetchone()[0])
            raw['evidence_index'].append(dict(id=evidence,label='Email',at='2026-11-09T15:00:00Z'))
            self.h.db.execute("UPDATE cases SET details=? WHERE id='alpha'",(json.dumps(raw),))
        result=overview(self.path,AT)
        self.assertEqual(result['cards'][0]['id'],'alpha')
        self.assertEqual(result['cards'][0]['latest_email_at'],'2026-11-09T09:00:00+00:00')
        self.assertIsNone(result['cards'][1]['latest_email_at'])

    def test_missing_or_wrong_schema_is_not_initialised(self):
        missing=Path(self.tmp.name)/'missing.db'
        with self.assertRaises(sqlite3.Error):overview(missing)
        self.assertFalse(missing.exists())
        bad=Path(self.tmp.name)/'bad.db'
        sqlite3.connect(bad).close()
        with self.assertRaisesRegex(ValueError,'format'):overview(bad)

    def test_viewer_cli_does_not_construct_writable_harness(self):
        from unittest.mock import patch
        from monitor.__main__ import main
        with patch('sys.argv',['monitor','--db',str(self.path),'ui','--no-browser']),patch('monitor.__main__.Harness') as harness,patch('monitor.ui.serve') as serve:
            main()
        harness.assert_not_called()
        serve.assert_called_once_with(str(self.path),0,False)

    @unittest.skipUnless(importlib.util.find_spec('flask'), 'Optional viewer dependencies not installed')
    def test_http_requires_token_disallows_writes_and_never_changes_records(self):
        from monitor.ui import create_app
        client=create_app(self.path,token='fixture-token').test_client()
        headers={'X-Viewer-Token':'fixture-token'}
        before=self.dump()
        with client.get('/') as shell_response:
            self.assertEqual(shell_response.status_code,200)
        self.assertEqual(client.get('/api/cards').status_code,401)
        self.assertEqual(client.get('/api/cards?token=fixture-token').status_code,401)
        self.assertEqual(client.get('/api/cards',headers=dict(headers,Host='malicious.invalid')).status_code,400)
        self.assertEqual(client.get('/api/cards',headers=dict(headers,**{'Sec-Fetch-Site':'cross-site'})).status_code,403)
        self.assertEqual(client.post('/api/cards',headers=dict(headers,Origin='http://localhost'),json={}).status_code,405)
        result=client.get('/api/cards',headers=headers)
        self.assertEqual(result.status_code,200)
        self.assertEqual(result.headers['Cache-Control'],'no-store')
        self.assertIn("frame-ancestors 'none'",result.headers['Content-Security-Policy'])
        self.assertEqual(client.get('/api/card/alpha',headers=headers).json['id'],'alpha')
        self.assertEqual(client.get('/api/card/unknown',headers=headers).status_code,404)
        self.assertEqual(before,self.dump())
        with client.get('/') as shell_response:
            shell=shell_response.text
        self.assertNotIn('Private card context',shell)
        self.assertNotIn('<script>alert(1)</script>',shell)


if __name__ == '__main__':
    unittest.main()
