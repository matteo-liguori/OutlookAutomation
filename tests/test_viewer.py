"""Current-only projections and HTTP reads must not mutate operational records."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from datetime import datetime, time, timedelta
from unittest.mock import Mock

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

    def test_saved_item_has_no_calendar_or_action_date_and_is_idempotent(self):
        from monitor.card_input import save_item
        payload = dict(title='Reference', note='Information to keep', idempotency_key='fixture-save')
        saved = save_item(self.h, payload)
        self.assertEqual(save_item(self.h, payload), saved)
        self.assertEqual(detail(self.path, saved['id'])['context'], payload['note'])
        self.assertFalse(any(row['case_id'] == saved['id'] for row in overview(self.path)['events']))
        with self.assertRaises(ValueError):
            save_item(self.h, dict(payload, note='Different note'))

    def test_local_event_and_due_acknowledgement_stay_in_harness(self):
        from monitor.configuration import zone
        from monitor.dashboard import acknowledge_appointment, read_window, save_local_event
        now = datetime.now(zone())
        today = now.date()
        tomorrow = today + timedelta(days=1)
        payload = dict(title='Private day', start=today.isoformat(), end=tomorrow.isoformat(),
                       all_day=True, idempotency_key='private-day')
        event = save_local_event(self.h, payload)
        self.assertEqual(save_local_event(self.h, payload), event)
        self.create('important', important_appointment=True, status='needs-user',
                    explicit_review=(now - timedelta(minutes=5)).isoformat(),
                    appointment_start=(now + timedelta(days=1)).isoformat())
        data = read_window(self.path, today - timedelta(days=1), tomorrow)
        self.assertEqual(data['calendar'], [dict(day=today.isoformat(), source='harness:local', events=[event])])
        reminder, = data['reminders']
        self.assertTrue(reminder['due'])
        self.assertEqual(data['appointments'], [])
        next_day = read_window(self.path, tomorrow, tomorrow + timedelta(days=1))
        marker, = next_day['appointments']
        self.assertEqual(marker['day'], tomorrow.isoformat())
        acknowledge_appointment(self.h, {key: reminder[key] for key in ('id', 'case_id', 'revision')})
        self.assertFalse(read_window(self.path, today - timedelta(days=1), tomorrow)['reminders'])
        self.assertTrue(self.h.case('alpha')['actions'])

    def test_archive_keeps_verified_coverage_separate_from_mail_runs(self):
        from monitor.configuration import zone
        from monitor.dashboard import archive_calendar, read_window
        from monitor.model import CAL
        now = datetime.now(zone())
        start = now.date()
        end = start + timedelta(days=2)
        payload = dict(source=CAL[0], complete=True, start=start.isoformat(), end=end.isoformat(),
                       checked_at=now.isoformat(), events=[])
        self.assertEqual(archive_calendar(self.h, payload)['days'], 2)
        self.assertEqual(len(read_window(self.path, start, end)['calendar']), 2)
        self.assertEqual(self.h.db.execute('SELECT count(*) FROM runs').fetchone()[0], 0)
        with self.assertRaises(ValueError):
            archive_calendar(self.h, dict(payload, complete=False))

    def test_multiday_icloud_ribbon_keeps_one_occurrence_across_refreshes(self):
        from monitor.configuration import zone
        from monitor.dashboard import archive_calendar, read_window

        now = datetime.now(zone())
        first = now.date()
        middle = first + timedelta(days=1)
        after = first + timedelta(days=2)
        end = first + timedelta(days=3)
        event = dict(id='Family:fixture-uid:', title='Multiday fixture', calendar='Family',
                     start=first.isoformat(), end=end.isoformat(), all_day=True)
        snapshot = dict(source='calendar:icloud', complete=True, start=first.isoformat(),
                        end=end.isoformat(), checked_at=now.isoformat(), events=[event])
        archive_calendar(self.h, snapshot)
        archive_calendar(self.h, dict(snapshot, start=middle.isoformat(), end=after.isoformat(),
                                      events=[dict(event, id='fixture-uid')]))

        events = [e for row in read_window(self.path, first, end)['calendar'] for e in row['events']]
        self.assertEqual([e['id'] for e in events], ['Family:fixture-uid:', 'fixture-uid', 'Family:fixture-uid:'])
        self.assertEqual(len({e['occurrence_key'] for e in events}), 1)

    def test_ordinary_action_calendar_visibility_is_opt_in(self):
        from datetime import date
        from monitor.card_input import set_calendar_visibility
        from monitor.dashboard import read_window
        start, end = date(2026, 11, 12), date(2026, 11, 13)
        card = self.h.case('alpha')
        self.assertFalse(read_window(self.path, start, end)['local_reminders'])
        set_calendar_visibility(self.h, dict(id='alpha', action_id='alpha-action',
                                            revision=card['revision'], show_on_calendar=True))
        self.assertEqual(read_window(self.path, start, end)['local_reminders'][0]['category'], 'Deadline')
        self.assertEqual(self.h.case('alpha')['actions'][0]['next_review'], card['actions'][0]['next_review'])

    def test_archive_stays_neutral_until_verified_event_is_classified(self):
        from monitor.configuration import zone
        from monitor.dashboard import archive_calendar, classify_calendar, read_window
        from monitor.model import CAL
        now = datetime.now(zone())
        start, end = now.date(), now.date() + timedelta(days=1)
        source = CAL[0]
        event = dict(id='verified', title='Work meeting', start=start.isoformat()+'T09:00:00+00:00',
                     end=start.isoformat()+'T10:00:00+00:00', all_day=False)
        archive_calendar(self.h, dict(source=source, complete=True, start=start.isoformat(),
                                     end=end.isoformat(), checked_at=now.isoformat(), events=[event]))
        self.assertNotIn('category', read_window(self.path, start, end)['calendar'][0]['events'][0])
        rid = self.h.start_run()['id']
        self.h.record_run(rid, dict(kind='source', source=source, complete=True, status='current',
                                    events=[event], evidence_ids=['fixture'], provenance='assistant'))
        assignment = dict(source=source, id=event['id'], start=event['start'], category='work')
        label = 'Project briefing with the design team'
        classify_calendar(self.h, rid, dict(assignments=[dict(assignment, display_title=label)]))
        classify_calendar(self.h, rid, dict(assignments=[assignment]))
        imported, = read_window(self.path, start, end)['calendar'][0]['events']
        self.assertEqual(imported['category'], 'work')
        self.assertEqual(imported['title'], event['title'])
        self.assertEqual(imported['display_title'], label)
        with self.assertRaises(ValueError):
            classify_calendar(self.h, rid, dict(assignments=[dict(assignment, display_title='Two\nlines')]))
        classify_calendar(self.h, rid, dict(assignments=[dict(assignment, display_title=None)]))
        self.assertNotIn('display_title', read_window(self.path, start, end)['calendar'][0]['events'][0])

    def test_reports_group_by_local_date_and_republishing_keeps_run_order(self):
        from monitor.configuration import zone
        from monitor.dashboard import publish, read_daily_reports, read_window

        today = datetime.now(zone()).date()
        tomorrow = today + timedelta(days=1)
        runs = {
            'first': (today, 9, 'complete'),
            'later': (today, 14, 'partial'),
            'tomorrow': (tomorrow, 9, 'complete'),
        }
        harness = Mock(wraps=self.h)
        harness.run.side_effect = lambda rid: dict(
            started_at=datetime.combine(runs[rid][0], time(runs[rid][1]), zone()).isoformat(),
            status=runs[rid][2],
        )
        for rid in ('later', 'tomorrow', 'first'):
            publish(harness, rid, {'markdown': rid})
        publish(harness, 'first', {'markdown': 'Revised morning report'})

        self.assertEqual(read_window(self.path, today, tomorrow)['report_days'], [today.isoformat()])
        self.assertEqual([(r['run_id'], r['markdown'], r['status']) for r in read_daily_reports(self.path, today)],
                         [('first', 'Revised morning report', 'complete'), ('later', 'later', 'partial')])
        self.assertEqual([r['run_id'] for r in read_daily_reports(self.path, tomorrow)], ['tomorrow'])

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
            put_message(self.h,dict(case_id=cid,action_ids=[cid+'-action'],message=dict(EntryId=cid,StoreId='fixture',Received=received,Body='Synthetic mail')))
        apply(self.h,dict(id='alpha',expected_revision=self.h.case('alpha')['revision'],idempotency_key='rename-old',title='Recently renamed'))
        # Re-reading the older email must not make it newer than beta.
        put_message(self.h,dict(case_id='alpha',action_ids=['alpha-action'],message=dict(EntryId='alpha',StoreId='fixture',Received='2026-11-08T10:00:00Z',Body='Synthetic mail')))
        result=overview(self.path,AT)
        self.assertEqual([c['id'] for c in result['cards']],['beta','alpha','gamma'])
        self.assertEqual(result['cards'][0]['latest_email_at'],'2026-11-09T09:00:00+00:00')
        self.assertIsNone(result['cards'][-1]['latest_email_at'])

    def test_email_reference_fallback_ignores_review_and_note_timestamps(self):
        self.create('beta')
        evidence=self.h.put_evidence(dict(kind='mail-source-reference',source='thread-cache',case_id='alpha',
            body=json.dumps(dict(locator=dict(Received='2026-11-09 09:00:00')))))['id']
        old_reference=self.h.put_evidence(dict(kind='mail-source-reference',source='thread-cache',case_id='beta',
            body=json.dumps(dict(locator=['old-store','old-entry']))))['id']
        c=self.h.case('alpha')
        with self.h.write():
            raw=json.loads(self.h.db.execute("SELECT details FROM cases WHERE id='alpha'").fetchone()[0])
            raw['evidence_index'].append(dict(id=evidence,label='Email',at='2026-11-09T15:00:00Z'))
            self.h.db.execute("UPDATE cases SET details=? WHERE id='alpha'",(json.dumps(raw),))
            raw=json.loads(self.h.db.execute("SELECT details FROM cases WHERE id='beta'").fetchone()[0])
            raw['evidence_index'].append(dict(id=old_reference,label='Legacy email reference',at='2026-11-09T16:00:00Z'))
            self.h.db.execute("UPDATE cases SET details=? WHERE id='beta'",(json.dumps(raw),))
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
