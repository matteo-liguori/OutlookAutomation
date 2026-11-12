"""Spent records retire atomically; active work, evidence and authority survive."""

import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from monitor.core import Harness
from monitor.model import dumps
from monitor.cards import render
from monitor.threadcache import put_message, find_message


class CardRetirementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.h = Harness(Path(self.tmp.name) / 'monitor.db', create=True)
        for eid in ('request', 'completion', 'approval-proof'):
            self.h.put_evidence(dict(id=eid, source='user', body=eid))
        self.create('A')

    def tearDown(self):
        self.h.close()
        self.tmp.cleanup()

    def create(self, cid):
        actions = [dict(id=cid+'-'+name, status='waiting', owner='Other party',
                        next_step=name, completion_condition='Written confirmation',
                        reopening_condition='New request') for name in ('approval', 'payment')]
        self.h.update(dict(id=cid, expected_revision=0, idempotency_key='create-'+cid,
                           evidence_ids=['request'], case=dict(title='Approval and payment',
                           position='Both outstanding', context='Independent approval and payment.', status='open'),
                           actions=actions))

    def replacement(self, cid='A', key='replace'):
        c = self.h.case(cid)
        return dict(id=cid, expected_revision=c['revision'], idempotency_key=key,
                    evidence_ids=['request'], case={k:v for k,v in c.items() if k not in
                    {'id', 'revision', 'updated_at', 'actions', 'decisions', 'evidence_ids'}},
                    actions=c['actions'])

    def complete(self, p):
        p['actions'][0].update(status='done', completion_reason='Written confirmation received',
                               completion_evidence=['completion'])
        p['case']['context']='Approval confirmed; payment still outstanding.'

    def approval(self, aid, status=None):
        item=dict(id=aid, case_id='A', evidence_ids=['approval-proof'], message=dict(
            sender='me@example.invalid', to=['you@example.invalid'], cc=[], bcc=[],
            subject='Approved exact request', body='Exact approved wording', attachments=[]))
        if status is not None:
            item['status']=status
        self.h.put_record('approval', item)

    def test_completion_deletes_canonical_row_keeps_sibling_cache_and_proof(self):
        put_message(self.h, dict(case_id='A', action_ids=['A-approval','A-payment'], message=dict(EntryId='mail', StoreId='store', Body='Useful email')))
        p=self.replacement();sibling=copy.deepcopy(p['actions'][1]);self.complete(p)
        result=self.h.update(p)
        self.assertEqual(self.h.update(p),result)
        c=self.h.case('A')
        self.assertEqual(c['actions'],[sibling])
        self.assertFalse(self.h.db.execute("SELECT 1 FROM actions WHERE id='A-approval'").fetchone())
        self.assertIn('completion',[e['id'] for e in c['evidence_index']])
        self.assertEqual(c['context'],'Approval confirmed; payment still outstanding.')
        self.assertTrue(find_message(self.h,dict(case_id='A',entry_id='mail',store_id='store'))['found'])
        raw=json.loads(self.h.db.execute("SELECT details FROM cases WHERE id='A'").fetchone()[0])
        self.assertNotIn('actions',raw)
        self.assertNotIn('resolved_actions',raw)
        with self.assertRaisesRegex(ValueError,'Stale revision'):
            self.h.update(dict(p,idempotency_key='stale'))

    def test_no_completion_without_proof_or_reviewed_context(self):
        original=self.h.case('A');p=self.replacement();self.complete(p)
        p['actions'][0]['completion_evidence']=[]
        with self.assertRaisesRegex(ValueError,'completion evidence'):self.h.update(p)
        self.assertEqual(self.h.case('A'),original)
        p=self.replacement();self.complete(p);p['case'].pop('context')
        with self.assertRaisesRegex(ValueError,'context'):self.h.update(p)
        self.assertEqual(self.h.case('A'),original)
        p=self.replacement();p['actions'].pop()
        with self.assertRaisesRegex(ValueError,'Unresolved actions cannot disappear'):self.h.update(p)
        self.assertEqual(self.h.case('A'),original)

    def test_spent_approvals_delete_but_pending_unknown_wording_survives(self):
        for state in ('approved', None, 'unknown', 'sent', 'superseded', 'revoked'):
            self.approval(str(state),state)
        p=self.replacement();self.h.update(p)
        retained={r['id']:json.loads(r['details']) for r in self.h.db.execute('SELECT * FROM approvals')}
        self.assertEqual(set(retained),{'approved','None','unknown'})
        self.assertTrue(all(a['message']['body']=='Exact approved wording' for a in retained.values()))
        self.assertIn('approval-proof',self.h.case('A')['evidence_ids'])

    def test_scoped_notes_expire_but_live_and_unlinked_constraints_remain(self):
        p=self.replacement(key='notes')
        p['decisions']=[dict(id='spent', text='Approval-only constraint', evidence_ids=['request'], action_ids=['A-approval']),
                        dict(id='both', text='Applies while either obligation remains', evidence_ids=['request'], action_ids=['A-approval','A-payment']),
                        dict(id='standing', text='Keep this current boundary', evidence_ids=['request'])]
        self.h.update(p)
        p=self.replacement(key='complete');self.complete(p);self.h.update(p)
        notes={d['id']:d for d in self.h.case('A')['decisions']}
        self.assertEqual(set(notes),{'both','standing'})
        self.assertEqual(notes['both']['action_ids'],['A-payment'])
        p=self.replacement(key='fold-notes');p['decisions']=[]
        p['case']['context']='Approval confirmed; payment remains subject to the current agreed terms.'
        self.h.update(p)
        self.assertEqual(self.h.case('A')['decisions'],[])

    def block_payment(self,cid='A'):
        p=self.replacement(cid,key='block-'+cid)
        p['actions'][1]['follow_up'].update(mode='blocked',depends_on=['A-approval'])
        self.h.update(p)

    def test_dependency_must_be_reconciled_without_implicitly_starting_timer(self):
        self.block_payment();before=self.h.case('A')
        p=self.replacement();self.complete(p)
        with self.assertRaisesRegex(ValueError,'Reconcile dependency'):self.h.update(p)
        self.assertEqual(self.h.case('A'),before)
        dependent=p['actions'][1];anchor=dependent['follow_up']['anchor_at']
        dependent['follow_up'].pop('depends_on')
        dependent['follow_up'].update(mode='review',reason='Prerequisite complete; prepare the actual request before any reply chase')
        self.h.update(p)
        actual=self.h.case('A')['actions'][0]['follow_up']
        self.assertEqual(actual['mode'],'review')
        self.assertEqual(actual['anchor_at'],anchor)
        self.assertNotIn('depends_on',actual)

    def test_external_dependency_blocks_pruning_until_explicitly_reconciled(self):
        self.create('B');self.block_payment('B')
        p=self.replacement();self.complete(p)
        with self.assertRaisesRegex(ValueError,'dependency in B-payment'):self.h.update(p)
        q=self.replacement('B',key='release-B')
        q['actions'][1]['follow_up'].pop('depends_on')
        q['actions'][1]['follow_up'].update(mode='review',reason='Review next step before requesting a reply')
        self.h.update(q);self.h.update(p)
        self.assertEqual(len(self.h.case('B')['actions']),2)
        self.assertEqual(len(self.h.case('A')['actions']),1)

    def test_missing_dependency_rejected_and_foreign_action_cannot_be_deleted(self):
        p=self.replacement();p['actions'][1]['follow_up'].update(mode='blocked',depends_on=['missing'])
        with self.assertRaisesRegex(ValueError,'Reconcile dependency'):self.h.update(p)
        self.create('B');p=self.replacement();a=copy.deepcopy(self.h.case('B')['actions'][0])
        a.update(status='done',completion_reason='Done',completion_evidence=['completion']);p['actions'].append(a)
        with self.assertRaisesRegex(ValueError,'another case'):self.h.update(p)
        self.assertEqual(len(self.h.case('B')['actions']),2)

    def seed_resolved_row(self):
        a=self.h.case('A')['actions'][0]
        a.update(status='done',completion_reason='Already confirmed',completion_evidence=['completion'],next_review=None)
        with self.h.db:
            self.h.db.execute('UPDATE actions SET status=?,next_review=NULL,details=? WHERE id=?',('done',dumps(a),a['id']))

    def test_old_resolved_row_can_be_omitted_without_faking_a_new_development(self):
        self.seed_resolved_row()
        with self.h.db:self.h.db.execute("INSERT INTO reported_cases VALUES('A',1)")
        p=self.replacement();p['actions']=[a for a in p['actions'] if a['status']!='done']
        self.h.update(p)
        self.assertEqual(self.h.db.execute("SELECT revision FROM reported_cases WHERE case_id='A'").fetchone()[0],2)
        self.assertIn('completion',self.h.case('A')['evidence_ids'])
        self.assertEqual(len(self.h.case('A')['actions']),1)

    def test_new_completion_remains_reportable_and_old_unreported_work_stays_unreported(self):
        with self.h.db:self.h.db.execute("INSERT INTO reported_cases VALUES('A',1)")
        p=self.replacement();self.complete(p);self.h.update(p)
        self.assertEqual(self.h.db.execute("SELECT revision FROM reported_cases WHERE case_id='A'").fetchone()[0],1)
        p=self.replacement(key='tidy');self.h.update(p)
        self.assertEqual(self.h.db.execute("SELECT revision FROM reported_cases WHERE case_id='A'").fetchone()[0],1)

    def test_late_failure_rolls_back_cleanup_card_and_approval_together(self):
        self.create('B');self.approval('spent','sent')
        with self.h.db:
            self.h.db.execute('INSERT INTO decisions VALUES(?,?,?)',('taken','B',dumps(dict(id='taken',text='Live',evidence_ids=['request']))))
        before=self.h.case('A');p=self.replacement();self.complete(p)
        p['decisions']=[dict(id='taken',text='Conflicting ID',evidence_ids=['request'])]
        with self.assertRaises(sqlite3.IntegrityError):self.h.update(p)
        self.assertEqual(self.h.case('A'),before)
        self.assertTrue(self.h.db.execute("SELECT 1 FROM approvals WHERE id='spent'").fetchone())
        self.assertFalse(self.h.db.execute("SELECT 1 FROM mutations WHERE key='replace'").fetchone())

    def test_render_has_no_resolved_list_or_empty_boundary_section(self):
        p=self.replacement();self.complete(p);self.h.update(p)
        content=Path(render(self.h,'A')['path']).read_text(encoding='utf-8')
        self.assertNotIn('Resolved actions',content)
        self.assertNotIn('Active decisions and pending approvals',content)
        self.assertIn('Harness actions (1)',content)
        self.assertIn('Approval confirmed; payment still outstanding.',content)


if __name__=='__main__':unittest.main()
