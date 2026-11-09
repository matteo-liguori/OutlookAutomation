"""User edits are narrow, atomic and do not count as task/card reviews."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from monitor.core import Harness
from monitor.card_input import apply, pending, Conflict
from monitor.cards import show
from monitor.briefing import compact
from monitor.viewer import detail


class CardInputTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/'records.db'
        self.h = Harness(self.path,create=True)
        self.h.put_evidence(dict(id='source',source='user',body='Synthetic instruction'))
        self.h.update(dict(id='A',expected_revision=0,idempotency_key='create',evidence_ids=['source'],
                           case=dict(title='Old title',purpose='Old purpose',context='Keep this context',position='Current',status='open'),
                           actions=[dict(id='action',status='waiting',owner='Other person',next_step='Await reply',
                                         completion_condition='Reply arrives',reopening_condition='Further question')]))

    def tearDown(self):
        self.h.close();self.tmp.cleanup()

    def edit(self, **fields):
        return dict(id='A',expected_revision=self.h.case('A')['revision'],idempotency_key='edit',**fields)

    def replacement(self, c, key='replace'):
        return dict(id='A',expected_revision=c['revision'],idempotency_key=key,evidence_ids=c['evidence_ids'],
                    case={k:v for k,v in c.items() if k not in {'id','revision','updated_at','actions','decisions','evidence_ids'}},
                    actions=c['actions'],decisions=c['decisions'])

    def test_labels_preserve_context_actions_permissions_and_review_clocks(self):
        before=self.h.case('A')
        result=apply(self.h,self.edit(title='New name',purpose='New recognition and goal'))
        after=self.h.case('A')
        self.assertEqual(after['title'],'New name');self.assertEqual(after['purpose'],'New recognition and goal')
        for key in ('actions','context','position','card_reviewed_at','review_interval_days','decisions'):
            self.assertEqual(before[key],after[key],key)
        self.assertEqual(result['revision'],before['revision']+1)
        self.assertEqual(self.h.db.execute('SELECT COUNT(*) FROM approvals').fetchone()[0],0)

    def test_note_is_seen_in_briefing_and_card_and_survives_agent_replacement(self):
        apply(self.h,self.edit(note='Please check the revised deadline before chasing.'))
        card=show(self.h,'A');note=card['user_notes'][0]
        self.assertIn('case note-ack',card['user_note_instruction'])
        self.assertTrue(any(i.get('type')=='user_note_prompt' for i in compact(self.h,'2026-11-09T12:00:00Z')))
        self.assertEqual(detail(self.path,'A')['user_notes'][0]['text'],note['text'])
        c=self.h.case('A');payload=self.replacement(c);payload['case'].pop('user_notes')
        payload['case']['user_note_instruction']=card['user_note_instruction']
        self.h.update(payload)
        self.assertEqual(self.h.case('A')['user_notes'],[note])
        self.assertNotIn('user_note_instruction',self.h.case('A'))
        # Neither ordinary reads nor a replacement acknowledge the inbox.
        show(self.h,'A');self.assertEqual(pending(self.h)[0]['count'],1)

    def test_acknowledgement_is_explicit_idempotent_and_preserves_other_notes(self):
        apply(self.h,self.edit(note='First note'))
        apply(self.h,dict(self.edit(note='Second note'),idempotency_key='second'))
        c=self.h.case('A');first=c['user_notes'][0]['id']
        payload=self.edit(note_ids=[first],outcome='Recorded the clarification in the current card.')
        payload['idempotency_key']='ack'
        result=apply(self.h,payload,True)
        self.assertEqual(apply(self.h,payload,True),result)
        self.assertEqual([n['text'] for n in self.h.case('A')['user_notes']],['Second note'])
        self.assertEqual(pending(self.h,['other']),[])

    def test_retry_does_not_duplicate_note_and_changed_retry_is_rejected(self):
        p=self.edit(note='One note only')
        first=apply(self.h,p)
        self.assertEqual(apply(self.h,p),first)
        self.assertEqual(len(self.h.case('A')['user_notes']),1)
        with self.assertRaises(ValueError):apply(self.h,dict(p,note='Different note'))

    def test_stale_and_invalid_edits_leave_no_orphan_evidence_or_partial_changes(self):
        stale=self.edit(title='Stale')
        apply(self.h,dict(self.edit(title='Newer'),idempotency_key='newer'))
        before='\n'.join(self.h.db.iterdump())
        with self.assertRaises(Conflict):apply(self.h,stale)
        for fields in [dict(purpose='a\nb\nc\nd'),dict(note=''),dict(title='x'*181),dict(status='closed'),dict(expected_revision=True)]:
            p=self.edit();p.update(fields)
            with self.assertRaises(ValueError):apply(self.h,p)
        self.assertEqual('\n'.join(self.h.db.iterdump()),before)

    def test_failed_write_rolls_back_evidence_and_labels(self):
        with self.h.write():self.h.db.execute("CREATE TRIGGER fail_input BEFORE UPDATE ON cases BEGIN SELECT RAISE(ABORT,'fixture failure'); END")
        before='\n'.join(self.h.db.iterdump())
        import sqlite3
        with self.assertRaises(sqlite3.Error):apply(self.h,self.edit(title='Fails',note='Not saved'))
        self.assertEqual('\n'.join(self.h.db.iterdump()),before)

    def test_pending_notes_cannot_be_dropped_on_closure(self):
        apply(self.h,self.edit(note='Please deal with this first'))
        c=self.h.case('A');p=self.replacement(c)
        p['actions']=[dict(c['actions'][0],status='dismissed',completion_reason='User dismissal',completion_evidence=['source'])]
        p['case']['status']='closed'
        with self.assertRaisesRegex(ValueError,'pending user notes'):self.h.update(p)
        self.assertEqual(self.h.case('A')['status'],'open')

    def test_filtered_briefing_and_cli_ack_deliver_notes(self):
        from monitor.__main__ import parser,markdown
        from monitor.commands import execute
        apply(self.h,self.edit(note='A note for the next run'))
        result=execute(self.h,parser().parse_args(['briefing','--case','A']))
        self.assertTrue(any(i.get('type')=='user_note_prompt' for i in result['items']))
        self.assertIn('User notes pending',markdown(result))
        c=self.h.case('A');p=self.edit(note_ids=[n['id'] for n in c['user_notes']],outcome='Handled in current records')
        p['idempotency_key']='cli-ack'
        with patch('monitor.commands.read_payload',return_value=p):
            execute(self.h,parser().parse_args(['case','note-ack','--input','-']))
        self.assertEqual(pending(self.h),[])

    @unittest.skipUnless(importlib.util.find_spec('flask'),'Optional UI dependencies missing')
    def test_http_edits_need_token_same_origin_and_json_and_report_conflicts(self):
        from monitor.ui import create_app
        client=create_app(self.path,token='secret').test_client()
        route='/api/card/A/edit';p=self.edit(title='Updated via UI',note='Inspect the reply')
        self.assertEqual(client.post(route,json=p).status_code,401)
        headers={'X-Viewer-Token':'secret'}
        self.assertEqual(client.post(route,json=p,headers=headers).status_code,403)
        self.assertEqual(client.post(route,json=p,headers=dict(headers,Origin='https://malicious.invalid')).status_code,403)
        headers['Origin']='http://localhost'
        self.assertEqual(client.post(route,data='{}',headers=headers).status_code,415)
        self.assertEqual(client.post(route,json=p,headers=headers).status_code,200)
        self.assertEqual(client.post(route,json=p,headers=headers).status_code,200)
        stale=dict(p,idempotency_key='stale')
        self.assertEqual(client.post(route,json=stale,headers=headers).status_code,409)
        self.assertEqual(client.post(route,json=dict(p,id='other'),headers=headers).status_code,409)
        self.assertEqual(len(self.h.case('A')['user_notes']),1)


if __name__=='__main__':unittest.main()
