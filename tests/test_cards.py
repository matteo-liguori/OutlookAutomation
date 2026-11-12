"""Current-card and live-health contracts, including migration and pending-work recovery."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from datetime import datetime,timedelta
from unittest.mock import patch
from monitor.core import Harness
from monitor.model import now,dumps,MAIL,CAL
from monitor.cards import show
from monitor.live import record,items
from monitor.threadcache import put_message


class CardTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.h=Harness(Path(self.tmp.name)/'monitor.db',create=True)
        self.h.put_evidence(dict(id='source',source='user',body='Track two independent obligations',captured_at='2026-10-01T09:00:00Z'))

    def tearDown(self):self.h.close();self.tmp.cleanup()

    def payload(self,cid='A'):
        return dict(id=cid,expected_revision=0,idempotency_key='create-'+cid,evidence_ids=['source'],case=dict(title='Two obligations',purpose='Obtain approval and payment',position='Both outstanding',context='A request was submitted; approval and payment are distinct.',status='open'),actions=[dict(id=cid+'-approval',owner='Other party',status='waiting',next_step='Obtain approval',completion_condition='Written approval',reopening_condition='Approval withdrawn'),dict(id=cid+'-payment',owner='Other party',status='waiting',next_step='Verify payment',completion_condition='Receipt confirmed',reopening_condition='Payment reversed')])

    def test_repeated_checks_keep_one_card_and_preserve_independent_timers(self):
        p=self.payload();self.h.update(p)
        original=copy.deepcopy(self.h.case('A')['actions'])
        for n in range(5):
            c=self.h.case('A');p.update(expected_revision=c['revision'],idempotency_key='check-'+str(n));p['actions']=c['actions'];self.h.update(p)
        c=self.h.case('A');self.assertEqual(c['actions'],original)
        self.assertEqual(self.h.db.execute('SELECT count(*) FROM cases').fetchone()[0],1)
        self.assertFalse(self.h.db.execute("SELECT 1 FROM sqlite_master WHERE name='history'").fetchone())
        self.assertEqual(len(show(self.h,'A')['actions']),2)
        c['actions'][0]['snooze_until']='2099-01-01T00:00:00Z'
        p.update(expected_revision=c['revision'],idempotency_key='snooze',actions=c['actions']);self.h.update(p)
        c=self.h.case('A');self.assertNotIn('snooze_until',c['actions'][1])
        self.assertEqual(c['actions'][1]['follow_up'],original[1]['follow_up'])

    def test_context_condensation_keeps_actions_approvals_and_evidence_order(self):
        p=self.payload();self.h.update(p)
        self.h.put_evidence(dict(id='older',source='mail',captured_at='2026-09-01T09:00:00Z',body='Older request'))
        before=self.h.case('A')['actions']
        p.update(expected_revision=1,idempotency_key='tidy',evidence_ids=['source','older','source']);p['case']['context']='Concise relevant context.'
        self.h.update(p);c=self.h.case('A')
        self.assertEqual(c['actions'],before)
        self.assertEqual([e['id'] for e in c['evidence_index']],['older','source'])
        self.assertEqual(c['context'],'Concise relevant context.')
        with self.assertRaises(ValueError):self.h.update(dict(p,idempotency_key='stale'))
        self.assertEqual(self.h.case('A')['context'],'Concise relevant context.')
        with self.assertRaises(ValueError):self.h.put_evidence(dict(source='snapshot',body=dumps(c)))


    def test_health_is_single_current_state_and_failure_cannot_erase_success(self):
        good=dict(status='current',complete=True,newest='2026-10-01T08:00:00Z',since='2026-09-29',until='2026-10-01')
        with self.h.write():
            record(self.h,MAIL[0],good,'2026-10-01T09:00:00Z',True)
            record(self.h,MAIL[0],dict(status='unavailable',complete=False,error='Authentication required'),'2026-10-02T09:00:00Z',False)
            record(self.h,MAIL[0],good,'2026-09-30T09:00:00Z',True)
        state=items(self.h)[0]
        self.assertEqual(state['last_verified_at'],'2026-10-01T09:00:00+00:00')
        self.assertEqual(state['failure_since'],'2026-10-02T09:00:00+00:00')
        self.assertEqual(self.h.db.execute('SELECT count(*) FROM source_health').fetchone()[0],1)
        with self.h.write():record(self.h,MAIL[0],dict(good,since='2026-10-03',until=now()),now(),True)
        self.assertEqual(items(self.h)[0]['status'],'partial')
        self.assertIsNotNone(items(self.h)[0]['coverage_gap_from'])
        with self.h.write():record(self.h,MAIL[0],dict(good,until=now()),now(),True)
        self.assertIsNone(items(self.h)[0]['failure_since'])

    def test_retiring_old_pass_carries_unreviewed_work_and_replaces_source_history(self):
        old=self.h.start_run(at=(datetime.fromisoformat(now())-timedelta(days=3)).isoformat())['id']
        self.h.record_run(old,dict(kind='candidate',id='unreviewed',source=MAIL[0],entry_id='message',store_id='store',received='2026-10-01T08:00:00Z',evidence_ids=['source']))
        self.h.finalize(old)
        new=self.h.start_run()['id']
        self.assertFalse(self.h.db.execute('SELECT 1 FROM runs WHERE id=?',(old,)).fetchone())
        self.assertTrue(self.h.db.execute("SELECT 1 FROM pending_work WHERE id='unreviewed'").fetchone())
        self.h.record_run(new,dict(kind='source',source=MAIL[0],status='unavailable',complete=True,count=0,candidate_count=0,evidence_ids=['source'],provenance='command'))
        self.assertEqual(self.h.db.execute('SELECT count(*) FROM candidates WHERE run_id=?',(new,)).fetchone()[0],1)
        self.h.record_run(new,dict(kind='candidate',id='unreviewed',source=MAIL[0],disposition='suppressed',review_reason='Reviewed full message; no remaining obligation',evidence_ids=['source']))
        self.assertFalse(self.h.db.execute('SELECT 1 FROM pending_work').fetchone())
        self.assertTrue(self.h.doctor()['ok'])

    def test_handoff_contains_only_revision_markers_not_old_cards(self):
        self.h.update(self.payload());rid=self.h.start_run()['id']
        self.h.record_run(rid,dict(kind='handoff',case_ids=['A'],action_ids=['A-approval','A-payment'],delivered=True))
        snapshot=self.h.run(rid)['details']['handoff']['reported_case_snapshots']['A']
        self.assertNotIn('position',snapshot);self.assertNotIn('context',snapshot);self.assertEqual(snapshot['revision'],1)

    def test_finished_work_expires_but_current_receipt_and_unresolved_work_survive(self):
        from monitor.live import expire_finished_working_data
        rid=self.h.start_run()['id']
        self.h.record_run(rid,dict(kind='candidate',id='pending',source=MAIL[0],entry_id='message',store_id='store',evidence_ids=['source']))
        with self.h.db:self.h.db.execute("UPDATE retention_activity SET at='2000-01-01T00:00:00+00:00' WHERE kind='run'")
        with self.h.write():expire_finished_working_data(self.h,now())
        self.assertEqual(self.h.run(rid)['status'],'running')
        self.assertTrue(self.h.db.execute('SELECT 1 FROM candidates').fetchone())
        self.h.finalize(rid)
        with self.h.db:self.h.db.execute("UPDATE retention_activity SET at='2000-01-01T00:00:00+00:00' WHERE kind='run'")
        with self.h.write():expire_finished_working_data(self.h,now())
        self.assertTrue(self.h.run(rid)['details']['working_expired'])
        self.assertTrue(self.h.db.execute("SELECT 1 FROM pending_work WHERE id='pending'").fetchone())
        self.assertFalse(self.h.db.execute('SELECT 1 FROM candidates').fetchone())
        with self.assertRaises(ValueError):self.h.record_run(rid,dict(kind='gate',name='records',success=True))
        with self.assertRaises(ValueError):self.h.finalize(rid)

    def test_card_paging_has_no_saved_card_copies_and_rejects_stale_cursor(self):
        from monitor.cards import page
        p=self.payload();p['case']['context']='Useful context. '*300;self.h.update(p)
        # A genuinely large current card uses a stateless content-checked cursor.
        for n in range(50):
            self.h.put_evidence(dict(id='e'+str(n),source='mail',captured_at='2026-10-01T09:00:00Z',body='Source'))
        p.update(expected_revision=1,idempotency_key='many-sources',evidence_ids=['source']+['e'+str(n) for n in range(50)])
        self.h.update(p);first=page(self.h,'A')
        self.assertTrue(first['next_cursor'])
        self.assertFalse(self.h.db.execute("SELECT 1 FROM sqlite_master WHERE name='snapshots'").fetchone())
        p.update(expected_revision=2,idempotency_key='changed');p['case']['context']='Updated context';self.h.update(p)
        with self.assertRaises(ValueError):page(self.h,'A',first['next_cursor'])

    def test_current_query_pages_never_save_card_or_health_copies(self):
        records=[{'context':'Current card'},{'source':'mail:work','status':'unverified'}]
        first=self.h.page('case:search',records,limit=1)
        second=self.h.page('case:search',records,cursor=first['next_cursor'],limit=1)
        self.assertEqual(second['items'],records[1:])
        self.assertFalse(self.h.db.execute("SELECT 1 FROM sqlite_master WHERE name='snapshots'").fetchone())
        with self.assertRaises(ValueError):self.h.page('case:search',[{'context':'Changed'}],cursor=first['next_cursor'])

    def test_completing_one_obligation_and_failed_update_leave_sibling_intact(self):
        p=self.payload();self.h.update(p)
        original=copy.deepcopy(self.h.case('A'))
        p.update(expected_revision=1,idempotency_key='approval-done',actions=copy.deepcopy(original['actions']))
        p['actions'][0].update(status='done',completion_reason='Written approval verified',completion_evidence=['source'])
        p['case']['context']='Approval received; payment is still outstanding.'
        self.h.update(p)
        current=self.h.case('A')
        self.assertEqual(current['actions'],[original['actions'][1]])
        self.assertEqual(current['actions'][0]['status'],'waiting')
        p.update(expected_revision=2,idempotency_key='invalid-update')
        p['case']['context']='This must roll back'
        p['decisions']=[dict(id='bad',text='Invalid evidence',evidence_ids=['missing-source'])]
        with self.assertRaises(ValueError):self.h.update(p)
        self.assertEqual(self.h.case('A'),current)


    def test_watch_index_is_brief_reference_and_only_due_work_surfaces_upkeep_prompt(self):
        from monitor.cards import watch_items,upkeep_prompt
        p=self.payload();p['case']['purpose']='Obtain approval and payment.\nWatch for the approval reply or payment confirmation.'
        self.h.update(p);original=copy.deepcopy(self.h.case('A'))
        watches=watch_items(self.h)
        self.assertEqual(watches[0]['summary'],p['case']['purpose'])
        self.assertTrue(watches[0]['reference_only'])
        self.assertNotIn('context',watches[0]);self.assertNotIn('actions',watches[0])
        self.assertFalse(upkeep_prompt(self.h.current_items(True),now()))
        a=original['actions'][0];a['next_review']='2000-01-01T00:00:00+00:00'
        with self.h.db:self.h.db.execute('UPDATE actions SET next_review=?,details=? WHERE id=?',(a['next_review'],dumps(a),a['id']))
        prompt=upkeep_prompt(self.h.current_items(True),now())[0]
        self.assertEqual(prompt['case_ids'],['A'])
        self.assertEqual(prompt['trigger'],'due-harness-action')
        from monitor.guidance import guide
        self.assertEqual(prompt['prompt'],guide('card-upkeep')['instruction'])
        self.assertEqual(self.h.case('A')['context'],original['context'])
        self.assertEqual(self.h.case('A')['actions'][1],original['actions'][1])
        p.update(expected_revision=1,idempotency_key='overlong-watch')
        p['case']['purpose']='one\ntwo\nthree\nfour'
        with self.assertRaises(ValueError):self.h.update(p)

    def test_briefing_cursor_anchors_clock_and_includes_all_open_watch_entries(self):
        from monitor.__main__ import parser,execute
        for cid in ('A','B'):
            p=self.payload(cid)
            for a in p['actions']:a['snooze_until']='2099-01-01T00:00:00Z'
            self.h.update(p)
        with self.h.db:self.h.db.execute("INSERT INTO meta VALUES('filing_enabled','1')")
        args=parser().parse_args(['--limit','1','briefing']);first=execute(self.h,args)
        self.assertTrue(first['next_cursor'])
        collected=first['items'];cursor=first['next_cursor']
        later=(datetime.fromisoformat(now())+timedelta(minutes=2)).isoformat()
        with patch('monitor.commands.now',return_value=later):
            while cursor:
                args.cursor=cursor;result=execute(self.h,args)
                collected+=result['items'];cursor=result['next_cursor']
        self.assertEqual({i['case'] for i in collected if i.get('type')=='thread_watch'},{'A','B'})
        self.assertFalse(any(i.get('type')=='card_upkeep_prompt' for i in collected))
        self.assertFalse(self.h.db.execute("SELECT 1 FROM sqlite_master WHERE name='snapshots'").fetchone())

    def test_matching_new_evidence_returns_explicit_upkeep_prompt_without_rescheduling(self):
        import io
        from monitor.__main__ import parser,execute
        p=self.payload();p['case']['match_keys']={'sources':['mail:personal'],'conversation_ids':['verified-conversation']}
        self.h.update(p);before=copy.deepcopy(self.h.case('A')['actions'])
        args=parser().parse_args(['briefing','--message-input','-'])
        with patch('sys.stdin',io.StringIO(dumps({'source':'mail:personal','conversation_id':'verified-conversation'}))):
            result=execute(self.h,args)
        prompt=next(i for i in result['items'] if i.get('type')=='card_upkeep_prompt')
        self.assertEqual(prompt['case_ids'],['A']);self.assertEqual(prompt['trigger'],'related-evidence')
        self.assertEqual(self.h.case('A')['actions'],before)
        self.assertFalse(hasattr(self.h,'_query_at'))

    def test_card_write_returns_harness_retention_instruction(self):
        import io
        from monitor.__main__ import parser, execute
        args=parser().parse_args(['case', 'update', '--input', '-'])
        with patch('sys.stdin', io.StringIO(dumps(self.payload()))):
            result=execute(self.h, args)
        self.assertIn('trim completed work', result['retention_instruction'])
        self.assertNotIn('retention_instruction', self.h.case('A'))
        with patch('sys.stdin', io.StringIO(dumps(self.payload()))):
            repeated=execute(self.h, args)
        self.assertNotIn('retention_instruction', repeated)

    def test_relevant_guidance_is_once_per_chat_across_connections(self):
        import os
        from monitor.guidance import once
        from monitor.core import Harness
        with patch.dict(os.environ,CODEX_THREAD_ID='synthetic-guidance-session'):
            self.assertEqual(once(self.h,'storage','Use owned storage.'),'Use owned storage.')
            other=Harness(self.h.path)
            try:
                self.assertIsNone(once(other,'storage','Use owned storage.'))
                self.assertEqual(once(other,'storage','Use registered owned storage.'),'Use registered owned storage.')
            finally:other.close()


if __name__=='__main__':unittest.main()
