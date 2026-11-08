"""Evidence follows its own thread's expiry instead of becoming an unbounded diary."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from monitor.core import Harness
from monitor.model import dumps, now
from monitor.evidence import prune
from monitor.threadcache import sweep


class EvidenceLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.h = Harness(Path(self.tmp.name) / 'monitor.db', create=True)
        self.input = dict(id='fact',source='user',body='The useful source fact')
        self.h.put_evidence(self.input)

    def tearDown(self):
        self.h.close()
        self.tmp.cleanup()

    def case(self, cid, **changes):
        p=dict(id=cid,expected_revision=0,idempotency_key='new-'+cid,evidence_ids=['fact'],
               case=dict(title='Task',position='Current outcome',context='Current concise brief',status='closed'),actions=[])
        p['case'].update(changes)
        self.h.update(p)
        return self.h.case(cid)

    def expire(self,cid):
        c=self.h.case(cid)
        sweep(self.h,at=(datetime.fromisoformat(c['closed_at'])+timedelta(days=366)).isoformat())

    def test_index_only_source_text_expires_with_completion(self):
        c=self.case('A')
        self.assertEqual(c['evidence_ids'],['fact'])
        meta=json.loads(self.h.db.execute("SELECT metadata FROM evidence WHERE id='fact'").fetchone()[0])
        self.assertEqual(meta['owner_case_id'],'A')
        self.expire('A')
        self.assertFalse(self.h.db.execute("SELECT 1 FROM cases WHERE id='A'").fetchone())
        row=self.h.db.execute("SELECT body FROM evidence WHERE id='fact'").fetchone()
        self.assertNotIn('useful source fact',row[0] if row else '')

    def test_two_threads_have_independent_evidence_not_a_shared_ownership_list(self):
        a=self.case('A');b=self.case('B',status='open')
        self.assertNotEqual(a['evidence_ids'],b['evidence_ids'])
        b_id=b['evidence_ids'][0]
        self.expire('A')
        row=self.h.db.execute('SELECT metadata,body FROM evidence WHERE id=?',(b_id,)).fetchone()
        self.assertEqual(row[1],self.input['body'])
        meta=json.loads(row[0]);self.assertEqual(meta['owner_case_id'],'B')
        self.assertNotIn('owners',meta)
        self.assertNotIn('refcount',meta)

    def test_standing_preference_uses_an_independent_current_context_copy(self):
        self.case('A')
        self.h.put_record('context',dict(id='preference',entity='Standing preference',text='Keep the user preference',evidence_ids=['fact']))
        d=json.loads(self.h.db.execute("SELECT details FROM context WHERE id='preference'").fetchone()[0])
        self.assertNotEqual(d['evidence_ids'],['fact'])
        self.expire('A')
        row=self.h.db.execute('SELECT metadata,body FROM evidence WHERE id=?',(d['evidence_ids'][0],)).fetchone()
        self.assertEqual(json.loads(row[0])['scope'],'standing')
        self.assertEqual(row[1],self.input['body'])

    def test_canonical_action_proof_is_local_and_failed_update_rolls_back_copy(self):
        self.case('A')
        before=[tuple(r) for r in self.h.db.execute('SELECT * FROM evidence')]
        with self.assertRaises(ValueError):self.case('B',context='x'*6001)
        self.assertEqual([tuple(r) for r in self.h.db.execute('SELECT * FROM evidence')],before)
        self.assertFalse(self.h.db.execute("SELECT 1 FROM cases WHERE id='B'").fetchone())
        self.h.put_evidence(self.input)  # Internal ownership cannot break retry safety.
        changed=dict(self.input,body='Changed source fact')
        with self.assertRaisesRegex(ValueError,'immutable'):self.h.put_evidence(changed)

    def test_new_nested_card_exports_are_rejected(self):
        c=self.case('A')
        for value in ([c],{'export':{'before':c,'after':c}}):
            with self.assertRaisesRegex(ValueError,'card snapshots'):
                self.h.put_evidence(dict(source='audit',body=dumps(value)))
        with self.assertRaisesRegex(ValueError,'card snapshots'):
            self.h.put_evidence(dict(source='audit',body='Summary',metadata={'copies':[c]}))

    def test_unclaimed_inputs_expire_but_pending_work_survives(self):
        self.h.put_evidence(dict(id='pending-proof',source='mail-source-reference',body='Read this pending message'))
        self.h.put_evidence(dict(id='unused',source='temporary',body='Unused export'))
        old=(datetime.fromisoformat(now())-timedelta(days=2)).isoformat()
        with self.h.db:
            self.h.db.execute("UPDATE retention_activity SET at=? WHERE kind='evidence'",(old,))
            self.h.db.execute('INSERT INTO pending_work VALUES(?,?,?)',('pending','mail:personal',dumps(dict(evidence_ids=['pending-proof']))))
        with self.h.write():prune(self.h)
        self.assertFalse(self.h.db.execute("SELECT 1 FROM evidence WHERE id='unused'").fetchone())
        self.assertTrue(self.h.db.execute("SELECT 1 FROM evidence WHERE id='pending-proof'").fetchone())

    def test_backup_cannot_restore_expired_index_only_source(self):
        c=self.case('A')
        d=json.loads(self.h.db.execute("SELECT details FROM cases WHERE id='A'").fetchone()[0])
        past=(datetime.fromisoformat(now())-timedelta(days=366)).isoformat()
        d.update(closed_at=past,expires_at=(datetime.fromisoformat(past)+timedelta(days=365)).isoformat())
        with self.h.db:self.h.db.execute("UPDATE cases SET details=? WHERE id='A'",(dumps(d),))
        restored=Harness(self.h.backup('expiry-contract')[0])
        try:
            self.assertFalse(restored.db.execute("SELECT 1 FROM cases WHERE id='A'").fetchone())
            row=restored.db.execute("SELECT body FROM evidence WHERE id='fact'").fetchone()
            self.assertNotIn('useful source fact',row[0] if row else '')
        finally:restored.close()

    def test_reported_notice_rows_are_retired_without_affecting_pending_notices(self):
        with self.h.db:
            self.h.db.execute('INSERT INTO roundup VALUES(?,?,?)',('sent','{}','old-run'))
            self.h.db.execute('INSERT INTO roundup VALUES(?,?,NULL)',('pending','{}'))
        with self.h.write():prune(self.h)
        self.assertEqual([r[0] for r in self.h.db.execute('SELECT id FROM roundup')],['pending'])

    def test_expired_source_cannot_be_reused_as_new_authority(self):
        self.case('A');self.expire('A')
        with self.assertRaisesRegex(ValueError,'Expired evidence'):
            self.case('NEW')

    def test_live_action_proof_is_independent_and_review_cannot_restart_clock(self):
        self.case('A')
        action=dict(id='B-action',owner='Other party',status='waiting',next_step='Await decision',
                    completion_condition='Written decision',reopening_condition='New request',
                    follow_up=dict(mode='reminder',anchor_at='2026-10-26T09:00:00Z',interval_days=2,
                                   reason='Promised two-day response',evidence_ids=['fact']))
        p=dict(id='B',expected_revision=0,idempotency_key='B-new',evidence_ids=['fact'],
               case=dict(title='Pending reply',context='Request submitted',position='Await reply'),actions=[action])
        self.h.update(p);before=self.h.case('B')['actions'][0]
        self.assertNotEqual(before['follow_up']['evidence_ids'],['fact'])
        self.expire('A')
        p.update(expected_revision=1,idempotency_key='B-check')
        self.h.update(p)
        self.assertEqual(self.h.case('B')['actions'][0],before)
        self.assertTrue(self.h.doctor()['ok'])

    def test_organization_persists_the_destination_actions_own_proof(self):
        from monitor.organize import organize
        for cid in ('A','B'):
            self.h.update(dict(id=cid,expected_revision=0,idempotency_key=cid,evidence_ids=['fact'],
                case=dict(title='Task',position='Await reply',context='An independent request is outstanding'),
                actions=[dict(id=cid+'-action',owner='Other party',status='waiting',next_step='Await reply',completion_condition='Written reply',reopening_condition='New request')]))
        before=self.h.case('B')['actions'][0]['next_review']
        organize(self.h,'merge',dict(target_id='A',source_ids=['B'],expected_revisions={'A':1,'B':1},
            idempotency_key='merge',reason='Same task, independent obligations',evidence_ids=['fact']))
        moved=next(a for a in self.h.case('A')['actions'] if a['id']=='B-action')
        self.assertEqual(moved['next_review'],before)
        self.expire('B')
        for eid in moved['follow_up']['evidence_ids']:
            meta,body=self.h.db.execute('SELECT metadata,body FROM evidence WHERE id=?',(eid,)).fetchone()
            self.assertEqual(json.loads(meta)['owner_case_id'],'A')
            self.assertEqual(body,self.input['body'])
        self.assertEqual(self.h.case('A')['evidence_ids'],['fact'])


if __name__=='__main__':unittest.main()
