"""Instruction retrieval and real-state trigger isolation; no live services."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from monitor.__main__ import parser
from monitor.commands import execute
from monitor.core import Harness
from monitor.guidance import GUIDES, COMMAND_TOPICS
from monitor.live import record
from monitor.model import now


class GuidanceTests(unittest.TestCase):
    def test_quiet_thread_review_requires_relevance_judgment_even_without_new_mail(self):
        from monitor.guidance import RELEVANCE_REVIEW
        payload=dict(id='Q',expected_revision=0,idempotency_key='quiet-review',evidence_ids=['e'],
                     case=dict(title='Old routine matter',purpose='Recognise routine mail',
                               context='Old tracking with no new correspondence',position='Quiet watch',status='open'),
                     actions=[dict(id='Q-a',owner='Agent',status='watch',next_step='Review whether useful',
                                   completion_condition='Tracking no longer useful',reopening_condition='New concrete problem',
                                   snooze_until='2099-01-01T09:00:00+00:00',
                                   follow_up=dict(mode='quiet',interval_days=7,basis='calendar',
                                                  reason='No routine chase',evidence_ids=['e']))])
        self.h.update(payload)
        self.assertNotIn('card_upkeep_prompt',self.prompt_types(self.call('briefing')))
        payload.update(expected_revision=1,idempotency_key='quiet-review-due')
        payload['actions'][0].pop('snooze_until')
        payload['actions'][0]['explicit_review']='2000-01-01T09:00:00+00:00'
        self.h.update(payload)
        before=self.h.case('Q')['actions']
        prompt=next(i for i in self.call('briefing')['items'] if i.get('type')=='card_upkeep_prompt')
        self.assertEqual(prompt['case_ids'],['Q'])
        self.assertNotIn(RELEVANCE_REVIEW,prompt['instruction'])
        self.assertIn('help card-upkeep',prompt['instruction'])
        self.assertEqual(self.h.case('Q')['actions'],before)
        self.assertIn(RELEVANCE_REVIEW,self.call('help','action','review')['instruction'])
        self.assertEqual(self.call('help','card-upkeep')['instruction'].count(RELEVANCE_REVIEW),1)

    def test_default_briefing_is_recognition_due_work_and_commands_without_card_bodies(self):
        payload=dict(id='A',expected_revision=0,idempotency_key='compact-A',evidence_ids=['e'],
                     case=dict(title='Recognisable thread',purpose='Recognise this thread and its goal.',
                               context='PRIVATE CARD BODY MUST NOT APPEAR',position='LONG POSITION MUST NOT APPEAR',status='open'),
                     actions=[dict(id='A-future',owner='User',status='needs-user',next_step='Future task',
                                   explicit_review='2099-01-01T09:00:00+00:00',
                                   completion_condition='Confirmed',reopening_condition='New request')])
        self.h.update(payload)
        result=self.call('briefing')
        text=json.dumps(result)
        self.assertNotIn('PRIVATE CARD BODY',text);self.assertNotIn('LONG POSITION',text)
        self.assertNotIn('Future task',text)
        self.assertEqual([i['type'] for i in result['items'] if i['type']!='briefing_section'],['thread_watch','command_hint'])
        payload.update(expected_revision=1,idempotency_key='compact-A-due')
        payload['actions'][0]['explicit_review']='2000-01-01T09:00:00+00:00'
        self.h.update(payload)
        result=self.call('briefing')
        self.assertEqual(result['items'][0]['title'],'Open threads')
        self.assertEqual(result['items'][-1]['type'],'command_hint')
        self.assertEqual(sum(i['type']=='due_action' for i in result['items']),1)
        prompt=next(i for i in result['items'] if i['type']=='card_upkeep_prompt')
        from monitor.guidance import RELEVANCE_REVIEW
        self.assertNotIn(RELEVANCE_REVIEW,prompt['instruction'])
        self.assertLess(len(prompt['instruction']),450)
        self.assertNotIn('PRIVATE CARD BODY',json.dumps(result))
        self.assertNotIn('### Scheduling actions',json.dumps(result))
        from monitor.__main__ import markdown
        rendered=markdown(result)
        self.assertLess(rendered.index('**Open threads**'),rendered.index('**Due actions and alerts**'))
        self.assertLess(rendered.index('**Due actions and alerts**'),rendered.index('**Periodic reviews**'))
        self.assertLess(rendered.index('**Periodic reviews**'),rendered.index('**Commands**'))
        self.assertNotIn('PRIVATE CARD BODY',rendered)

    def test_default_problem_alert_points_to_help_without_dumping_procedure(self):
        with self.h.write():
            record(self.h,'mail:work',{'status':'unavailable','error':'Synthetic failure'},
                   '2026-11-08T08:00:00+00:00',False)
        result=self.call('briefing')
        prompt=next(i for i in result['items'] if i.get('type')=='mail_access_prompt')
        self.assertEqual(prompt['instruction'],'Use help mail-access.')
        self.assertNotIn('standing-own-account-delivery-tests',json.dumps(result))
        self.assertIn('applicable delivery-test permission',self.call('help','mail-access')['instruction'])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.h = Harness(Path(self.tmp.name) / 'monitor.db', create=True)
        self.h.put_evidence({'id':'e','source':'user','body':'Synthetic instruction'})

    def tearDown(self):
        self.h.close()
        self.tmp.cleanup()

    def call(self, *words, at='2026-11-08T09:00:00+00:00'):
        with patch('monitor.commands.now', return_value=at):
            return execute(self.h, parser().parse_args(list(words)))

    def prompt_types(self, result):
        return [i.get('type') for i in result['items'] if i.get('type', '').endswith('_prompt')]

    def test_help_index_and_each_topic_are_read_only_and_specific(self):
        before = self.h.db.total_changes
        index = self.call('help')
        self.assertEqual({i['topic'] for i in index['topics']}, set(GUIDES))
        self.assertNotIn('instruction', json.dumps(index))
        for topic, instruction in GUIDES.items():
            with self.subTest(topic=topic):
                result=self.call('help', topic)
                self.assertEqual({k:result[k] for k in ('topic','instruction')}, {'topic':topic,'instruction':instruction})
        for command, topic in COMMAND_TOPICS.items():
            with self.subTest(command=command):
                self.assertEqual(self.call('help', *command.split())['topic'], topic)
        self.assertEqual(before, self.h.db.total_changes)
        with self.assertRaises(ValueError):
            self.call('help', 'not-an-operation')

    def test_actual_help_cli_works_without_database_and_does_not_create_it(self):
        absent = Path(self.tmp.name) / 'absent.db'
        for args in (['help'], ['help','case','update'], ['guide','permissions']):
            result = subprocess.run([sys.executable,'-X','utf8','-m','monitor','--format','json',
                                     '--db',str(absent),*args], capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            json.loads(result.stdout)
        self.assertFalse(absent.exists())

    def test_access_prompt_tracks_actual_failure_recovery_and_refresh_only_failure(self):
        self.assertEqual(self.prompt_types(self.call('briefing')), [])
        at = '2026-11-08T08:00:00+00:00'
        with self.h.write():
            record(self.h,'mail:work',{'status':'unavailable','error':'Synthetic failure'},at,False)
        self.assertEqual(self.prompt_types(self.call('briefing')), ['mail_access_prompt'])
        with self.h.write():
            record(self.h,'mail:work',{'status':'current','complete':True},
                   '2026-11-08T08:05:00+00:00',True)
        self.assertEqual(self.prompt_types(self.call('briefing')), ['mail_coverage_prompt'])
        with self.h.write():
            record(self.h,'mail:work',{'status':'current','complete':True,
                   'since':'2026-11-08T08:00:00+00:00','until':'2026-11-08T08:06:00+00:00'},
                   '2026-11-08T08:06:00+00:00',True)
        self.assertEqual(self.prompt_types(self.call('briefing')), [])
        with self.h.write():
            record(self.h,'outlook:refresh',{'status':'unavailable','error':'No SyncEnd'},at,False)
        self.assertEqual(self.prompt_types(self.call('briefing')), ['mail_access_prompt'])

    def test_calendar_failure_does_not_request_mail_delivery_test(self):
        with self.h.write():
            record(self.h,'calendar:primary',{'status':'unavailable','error':'Synthetic failure'},
                   '2026-11-08T08:00:00+00:00',False)
        self.assertNotIn('mail_access_prompt', self.prompt_types(self.call('briefing')))

    def test_due_permission_prompt_in_full_and_scoped_briefing_then_disappears_on_review(self):
        self.h.update({'id':'A','expected_revision':0,'idempotency_key':'A', 'evidence_ids':['e'],
                       'case':{'title':'Synthetic thread','purpose':'Synthetic thread',
                               'context':'Current facts','position':'Waiting','status':'open'},'actions':[]})
        from monitor.approvals import put, review
        put(self.h, {'id':'permission','kind':'standing','case_id':'A','title':'Synthetic scope',
                     'summary':'Test permission','granted_at':'2026-10-01T09:00:00+00:00',
                     'scope':{'operation':'synthetic only'},'evidence_ids':['e']})
        for args in (('briefing',),('briefing','--case','A')):
            types = self.prompt_types(self.call(*args))
            self.assertEqual(types.count('permission_review_prompt'), 1)
        review(self.h, {'id':'permission','expected_revision':1,'decision':'keep',
                        'reason':'Explicit synthetic user confirmation','evidence_ids':['e']},
               at='2026-11-08T09:00:00+00:00')
        self.assertNotIn('permission_review_prompt', self.prompt_types(self.call('briefing')))

    def test_snoozed_card_stays_quiet_then_due_and_explicit_case_work_prompts(self):
        payload = {'id':'A','expected_revision':0,'idempotency_key':'A','evidence_ids':['e'],
                   'case':{'title':'Synthetic card','purpose':'Synthetic card','context':'Current facts',
                           'position':'Pending','status':'open'},
                   'actions':[{'id':'A-a','owner':'User','status':'needs-user','next_step':'Synthetic task',
                               'completion_condition':'Confirmed','reopening_condition':'New failure',
                               'snooze_until':'2099-01-01T09:00:00+00:00'}]}
        self.h.update(payload)
        self.assertNotIn('card_upkeep_prompt',self.prompt_types(self.call('briefing')))
        self.assertIn('card_upkeep_prompt',self.prompt_types(self.call('briefing','--case','A')))
        payload.update(expected_revision=1,idempotency_key='A-due')
        payload['actions'][0].pop('snooze_until')
        payload['actions'][0]['explicit_review']='2000-01-01T00:00:00+00:00'
        self.h.update(payload)
        result = self.call('briefing')
        self.assertEqual(self.prompt_types(result).count('card_upkeep_prompt'),1)
        prompt = next(i for i in result['items'] if i.get('type') == 'card_upkeep_prompt')
        self.assertEqual(prompt['case_ids'], ['A'])

    def test_storage_warning_prompt_is_removed_when_warning_clears(self):
        with self.h.write():
            self.h.db.execute("INSERT INTO meta VALUES('maintenance_warning',?)",
                              (json.dumps({'reliability_affected':True,'failures':['Synthetic failure']}),))
        self.assertIn('maintenance_prompt',self.prompt_types(self.call('briefing')))
        with self.h.write():
            self.h.db.execute("DELETE FROM meta WHERE key='maintenance_warning'")
        self.assertNotIn('maintenance_prompt',self.prompt_types(self.call('briefing')))

    def test_pass_start_only_delivers_pass_contract_and_activation_when_requested(self):
        result = self.call('run','start')
        self.assertEqual([g['topic'] for g in result['guidance']], ['pass-receipts'])
        self.assertNotIn('schedule plan', result['guidance'][0]['instruction'])
        result = self.call('run','start','--activated')
        self.assertEqual([g['topic'] for g in result['guidance']], ['pass-receipts','activation'])

    def test_real_ingestion_includes_access_guidance_only_while_freshness_is_unverified(self):
        from monitor.sources import MAIL_STORES
        at = now()
        rid = self.h.start_run()['id']
        source = next(iter(MAIL_STORES))
        store = MAIL_STORES[source]
        page = dict(Store=store, StoreId='synthetic-store', SnapshotId='synthetic',
                    StartedAt=at, Since=at, Until=at, FirstId='synthetic-mail', Offset=0, NextOffset=None,
                    Complete=True, Errors=[], Count=1, Newest=at,
                    Items=[dict(EntryId='synthetic-mail', StoreId='synthetic-store', Received=at,
                                MessageClass='IPM.Note', Subject='Synthetic newsletter',
                                SenderEmail='sender@example.com')])
        payload = dict(source=source, pages=[page], started_at=at)
        with patch('monitor.commands.read_payload', return_value=payload):
            result = self.call('run','ingest',rid,'--input','-')
        self.assertEqual(result['guidance']['topic'], 'mail-access')
        payload['freshness'] = dict(Store=store, LatestStoreId='synthetic-store',
                                    Current=True, Status='current', CheckedAt=at, LatestReceived=at)
        with patch('monitor.commands.read_payload', return_value=payload):
            result = self.call('run','ingest',rid,'--input','-')
        self.assertEqual(result['status'], 'current')
        self.assertNotIn('guidance', result)

    def test_prompt_is_deduplicated_and_preserved_across_briefing_pages(self):
        for source in ('mail:work','mail:personal','outlook:refresh'):
            with self.h.write():
                record(self.h,source,{'status':'unavailable'},'2026-11-08T08:00:00+00:00',False)
        result = self.call('--limit','1','briefing')
        all_items = list(result['items'])
        while result['next_cursor']:
            result = self.call('--limit','1','--cursor',result['next_cursor'],'briefing')
            all_items.extend(result['items'])
        self.assertEqual(sum(i.get('type') == 'mail_access_prompt' for i in all_items), 1)
        self.assertEqual(sum(i.get('type') == 'source_health' for i in all_items), 3)
