"""Extension removal, code approval and custom-action execution boundaries."""
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from monitor.core import Harness
from monitor.configuration import resolve,snapshot,dump
from monitor.extensions import bundle,approve,available,effective,run_action,event,metadata
from monitor.approvals import put
from monitor.model import now


class ExtensionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.h=Harness(self.root/'monitor/monitor.db',create=True);self.addCleanup(self.h.close)
        self.h.put_evidence(dict(id='user',source='user',body='Approve isolated synthetic extension'))
        self.config=resolve(self.root,profile={})

    def install(self,kind='experiment'):
        folder=self.root/'extensions/sample';folder.mkdir(parents=True)
        manifest=dict(id='sample',kind=kind,enabled=True,timeout_seconds=3,settings={'reviews':{'card_days':2}},
                      actions={'calculate':{'entrypoint':'extension.py:run','parameters':{'value':{'type':'integer','required':True}}}},hooks={'activation':'extension.py:observe'})
        (folder/'manifest.yaml').write_text(dump(manifest),encoding='utf-8')
        (folder/'extension.py').write_text("def run(c):\n return {'answer':c['parameters']['value']*2}\ndef observe(c):\n return {'observations':{'seen':c['state']['date']}}\n",encoding='utf-8')
        return folder

    def grant_code(self):
        current=bundle(self.root,'sample')
        with snapshot(self.config):approve(self.h,dict(id='sample',hash=current['hash'],evidence_ids=['user']))

    def test_version_approval_removal_and_expiry_restore_defaults(self):
        folder=self.install();self.assertFalse(available(self.config,self.h)[0]['active']);self.grant_code()
        self.assertEqual(effective(self.config,self.h).get('reviews.card_days'),2)
        source=folder/'extension.py';source.write_text(source.read_text()+'\n# A new version\n',encoding='utf-8')
        self.assertFalse(available(self.config,self.h)[0]['active']);self.grant_code()
        manifest=(folder/'manifest.yaml');value=__import__('monitor.configuration',fromlist=['read_yaml']).read_yaml(manifest)
        value['expires_at']='2020-01-01T00:00:00Z';manifest.write_text(dump(value),encoding='utf-8');self.grant_code()
        self.assertEqual(effective(self.config,self.h).get('reviews.card_days'),7)
        shutil.rmtree(folder)
        self.assertEqual(effective(self.config,self.h).get('reviews.card_days'),7)

    def test_observations_are_private_and_survive_bundle_removal(self):
        folder=self.install();self.grant_code()
        with snapshot(self.config):event(self.h,'activation',{'state':{'date':'2026-11-11'}})
        self.assertEqual(metadata(self.h.db,'extension-observations:sample')['seen'],'2026-11-11')
        shutil.rmtree(folder)
        with snapshot(self.config):event(self.h,'activation',{'state':{'date':'2026-11-12'}})
        self.assertEqual(metadata(self.h.db,'extension-observations:sample')['seen'],'2026-11-11')

    def test_activation_hook_observations_share_outer_transaction(self):
        import os
        from monitor.activation import arm
        self.install(); self.grant_code()
        with snapshot(self.config), patch.dict(os.environ, CODEX_THREAD_ID='synthetic-mail-chat'):
            arm(self.h)
            observed = metadata(self.h.db, 'extension-observations:sample')
            self.assertIn('seen', observed)
            with self.assertRaisesRegex(ValueError, 'rollback'):
                with self.h.write():
                    event(self.h, 'activation', {'state': {'date': '2099-01-01'}})
                    raise ValueError('rollback')
            self.assertEqual(metadata(self.h.db, 'extension-observations:sample'), observed)

    def test_custom_action_requires_scope_and_deduplicates(self):
        self.install('action');self.grant_code()
        self.h.update(dict(id='card',expected_revision=0,idempotency_key='create',evidence_ids=['user'],
             case=dict(title='Synthetic',position='Calculate',context='Synthetic calculation',status='open'),
             actions=[dict(id='task',owner='assistant',status='needs-user',next_step='Calculate',completion_condition='Result checked',reopening_condition='Input changes',custom_action=dict(bundle='sample',action='calculate',parameters={'value':3},approval_id='execute'))]))
        with snapshot(self.config):
            with self.assertRaises(ValueError):run_action(self.h,'task',{'idempotency_key':'one'})
            put(self.h,dict(id='execute',kind='one-off',case_id='card',granted_at=now(),evidence_ids=['user'],scope=dict(operation='custom-action',bundle='sample',action='calculate')))
            result=run_action(self.h,'task',{'idempotency_key':'one'})
            self.assertEqual(result['answer'],6)
            self.assertTrue(run_action(self.h,'task',{'idempotency_key':'one'})['duplicate_prevented'])
            with patch('monitor.extensions.invoke',side_effect=ValueError('interrupted')):
                with self.assertRaisesRegex(ValueError,'interrupted'):run_action(self.h,'task',{'idempotency_key':'two'})
            with self.assertRaisesRegex(ValueError,'uncertain'):run_action(self.h,'task',{'idempotency_key':'two'})
            with self.assertRaisesRegex(ValueError,'uncertain'):run_action(self.h,'task',{'idempotency_key':'different'})
            from monitor.extensions import reconcile_action
            reconcile_action(self.h,'task',dict(idempotency_key='two',outcome='not-attempted',evidence_ids=['user']))
            self.assertEqual(run_action(self.h,'task',{'idempotency_key':'two'})['answer'],6)

    def test_card_customisation_survives_replacement_and_controls_new_defaults(self):
        payload=dict(id='private',expected_revision=0,idempotency_key='create-private',evidence_ids=['user'],
           case=dict(title='Private rule',position='Wait',context='Synthetic',customisations={'reviews':{'followup_days':4,'card_days':3}}),
           actions=[dict(id='wait',owner='other',status='waiting',next_step='Await reply',completion_condition='Reply',reopening_condition='New request')])
        with snapshot(self.config):
            self.h.update(payload)
            original=self.h.case('private')
            self.assertEqual(original['actions'][0]['follow_up']['interval_days'],4)
            self.assertEqual(original['review_interval_days'],3)
            del payload['case']['customisations'];payload.update(expected_revision=1,idempotency_key='replace-private',actions=original['actions'])
            self.h.update(payload)
            self.assertEqual(self.h.case('private')['customisations'],original['customisations'])


if __name__=='__main__':unittest.main()
