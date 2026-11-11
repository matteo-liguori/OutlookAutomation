"""Whole-card review coverage, action deduplication and resumable CLI workflow."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from monitor.core import Harness
from monitor.commands import execute
from monitor.__main__ import parser, markdown
from monitor.reviews import due
from monitor.guidance import RELEVANCE_REVIEW


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.h = Harness(Path(self.tmp.name)/'monitor.db', create=True)
        self.h.put_evidence(dict(id='e', source='user', body='Synthetic review evidence'))

    def tearDown(self):
        self.h.close()
        self.tmp.cleanup()

    def test_briefing_sorts_unscoped_issue_alongside_run(self):
        self.h.put_record('issue', dict(id='unscoped', text='Check collector', resolved=False))
        self.h.start_run()
        items = self.h.current_items(briefing=True)
        self.assertTrue(any(i.get('issue') == 'unscoped' for i in items))

    def create(self, cid, action=None):
        p = dict(id=cid, expected_revision=0, idempotency_key='create-'+cid, evidence_ids=['e'],
                 case=dict(title=cid, purpose='Recognise '+cid, context='Full private context '+cid,
                           position='Current '+cid, status='open'), actions=[])
        if action is not None:
            p['actions']=[dict(id=cid+'-a', owner='Agent', status='watch', next_step='Watch',
                               completion_condition='Resolved', reopening_condition='New problem', **action)]
        with patch('monitor.records.now', return_value='2026-10-01T09:00:00+00:00'):
            self.h.update(p)

    def call(self, *words, payload=None, at='2026-10-15T09:00:00+00:00'):
        with patch('monitor.commands.now', return_value=at), patch('monitor.views.now', return_value=at), patch('monitor.commands.read_payload', return_value=payload):
            return execute(self.h, parser().parse_args(list(words)))

    def keep(self, cid, key=None):
        c=self.h.case(cid)
        return dict(id=cid, expected_revision=c['revision'], idempotency_key=key or 'keep-'+cid,
                    outcome='Concrete current obligation remains', evidence_ids=c['evidence_ids'])

    def test_four_sections_and_idle_reviews_without_cards_or_instructions(self):
        self.create('A')
        result=self.call('briefing')
        self.assertEqual([i['title'] for i in result['items'] if i['type']=='briefing_section'],
                         ['Open threads','Due actions and alerts','Periodic reviews','Commands'])
        self.assertEqual([i['case'] for i in result['items'] if i['type']=='review_due'],['A'])
        self.assertNotIn('Full private context',json.dumps(result))
        self.assertNotIn(RELEVANCE_REVIEW,json.dumps(result))
        self.assertIn('**Periodic reviews**',markdown(result))

    def test_instructions_return_only_after_more_than_two_hours_and_are_chat_scoped(self):
        self.create('A')
        first=self.call('review','next',at='2026-10-15T09:00:00+00:00')
        self.assertIn(RELEVANCE_REVIEW,json.dumps(first))
        boundary=self.call('review','next',at='2026-10-15T11:00:00+00:00')
        self.assertNotIn(RELEVANCE_REVIEW,json.dumps(boundary))
        later=self.call('review','next',at='2026-10-15T13:00:01+00:00')
        self.assertIn(RELEVANCE_REVIEW,json.dumps(later))
        with patch.dict('os.environ',{'CODEX_THREAD_ID':'another-chat'}):
            self.assertIn(RELEVANCE_REVIEW,json.dumps(self.call('review','next',at='2026-10-15T13:01:00+00:00')))

    def test_recording_review_completion_counts_as_review_activity(self):
        self.create('A');self.create('B')
        self.call('review','next',at='2026-10-15T09:00:00+00:00')
        self.call('review','complete','--input','-',payload=self.keep('A'),at='2026-10-15T10:59:00+00:00')
        result=self.call('review','next',at='2026-10-15T12:00:00+00:00')
        self.assertNotIn(RELEVANCE_REVIEW,json.dumps(result))
        self.assertEqual(result['items'][0]['id'],'B')

    def test_one_at_a_time_repeat_until_reviewed_then_finish(self):
        self.create('A'); self.create('B')
        first=self.call('review','next')
        self.assertIn(RELEVANCE_REVIEW,first['items'][0]['instruction'])
        self.assertEqual(first['items'][1]['id'],'A')
        following=self.call('review','next')
        self.assertEqual(following['items'][0]['id'],'A')
        self.assertEqual(len(following['items']),1)
        self.assertNotIn(RELEVANCE_REVIEW,json.dumps(following))
        p=self.keep('A')
        r=self.call('review','complete','--input','-',payload=p)
        self.assertEqual(r,self.call('review','complete','--input','-',payload=p))
        self.assertEqual(self.call('review','next')['items'][0]['id'],'B')
        self.call('review','complete','--input','-',payload=self.keep('B'))
        self.assertTrue(self.call('review','next')['complete'])
        self.assertEqual(self.call('review','next')['items'],[])
        self.assertEqual([x['case'] for x in due(self.h,'2026-10-22T09:00:00+00:00')],['A','B'])

    def test_action_due_is_not_also_periodic_and_review_resets_card_clock(self):
        self.create('A',dict(explicit_review='2026-10-10T09:00:00+00:00'))
        result=self.call('briefing')
        self.assertEqual(sum(i['type']=='due_action' for i in result['items']),1)
        self.assertFalse(any(i['type']=='review_due' for i in result['items']))
        c=self.h.case('A')
        p=dict(action_id='A-a',expected_revision=c['revision'],idempotency_key='action-review',
               review_kind='records',outcome='Whole card reviewed; concrete request remains',evidence_ids=c['evidence_ids'],
               reviewed_at='2026-10-15T09:00:00+00:00',review_policy=dict(urgency='normal',interval_days=30,
               basis='calendar',reason='Low activity',evidence_ids=c['evidence_ids']))
        self.call('action','review','--input','-',payload=p)
        self.assertEqual(self.h.case('A')['card_reviewed_at'],'2026-10-15T09:00:00+00:00')
        self.assertEqual(due(self.h,'2026-10-16T09:00:00+00:00'),[])

    def test_quiet_and_blocked_actions_receive_periodic_review_without_active_work(self):
        self.create('Q',dict(follow_up=dict(mode='quiet',interval_days=90,basis='calendar',reason='Quiet',evidence_ids=['e'])))
        self.create('B',dict(follow_up=dict(mode='blocked',interval_days=7,basis='calendar',reason='Depends on Q',evidence_ids=['e'],depends_on=['Q-a'])))
        result=self.call('briefing')
        self.assertEqual({i['case'] for i in result['items'] if i['type']=='review_due'},{'B','Q'})
        self.assertFalse(any(i['type']=='due_action' for i in result['items']))

    def test_reading_does_not_count_and_completion_preserves_all_action_clocks(self):
        self.create('A',dict(deadline='2026-12-01T09:00:00+00:00'))
        c=self.h.case('A'); old=copy.deepcopy(c['actions'])
        self.call('case','show','A')
        self.assertEqual(self.h.case('A')['card_reviewed_at'],c['card_reviewed_at'])
        self.call('review','complete','--input','-',payload=self.keep('A'))
        self.assertEqual(self.h.case('A')['actions'],old)
        self.assertEqual(due(self.h,'2026-10-16T09:00:00+00:00'),[])

    def test_stale_invalid_and_changed_retry_rejected(self):
        self.create('A')
        p=self.keep('A');p['expected_revision']=0
        with self.assertRaises(ValueError):self.call('review','complete','--input','-',payload=p)
        p=self.keep('A');p['interval_days']=0
        with self.assertRaises(ValueError):self.call('review','complete','--input','-',payload=p)
        p=self.keep('A');self.call('review','complete','--input','-',payload=p)
        p['outcome']='Different reason'
        with self.assertRaises(ValueError):self.call('review','complete','--input','-',payload=p)

    def test_explicit_snooze_and_closed_cards_do_not_enter_queue(self):
        self.create('A',dict(snooze_until='2099-01-01T09:00:00+00:00'))
        self.create('B')
        c=self.h.case('B')
        self.h.update(dict(id='B',expected_revision=c['revision'],idempotency_key='close-B',evidence_ids=c['evidence_ids'],
                           case=dict(title='B',context='No longer relevant',position='Closed',status='closed'),actions=[]))
        self.assertEqual(due(self.h,'2026-10-15T09:00:00+00:00'),[])

    def test_large_card_is_paged_with_instructions_before_content(self):
        self.create('A')
        c=self.h.case('A')
        case={k:v for k,v in c.items() if k not in {'id','revision','actions','decisions','updated_at','evidence_ids'}}
        case['context']='Large context '*400
        with patch('monitor.records.now',return_value='2026-10-01T09:00:00+00:00'):
            self.h.update(dict(id='A',expected_revision=1,idempotency_key='large',evidence_ids=c['evidence_ids'],case=case,actions=[]))
        r=self.call('--limit','1','review','next')
        self.assertEqual(r['items'][0]['type'],'review_instructions')
        self.assertTrue(r['next_cursor'])
        r=self.call('--limit','1','--cursor',r['next_cursor'],'review','next')
        self.assertIn('A',json.dumps(r))
