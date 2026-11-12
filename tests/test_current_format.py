"""Current-only storage, canonical actions and working-data lifecycle contracts."""

import copy
import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from monitor.core import Harness
from monitor.storage import CURRENT_SCHEMA
from monitor.model import MAIL,dumps,now
from monitor.maintenance import clean,verified_backup
from monitor.threadcache import put_message
from monitor.__main__ import parser


class CurrentFormatTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/'monitor.db'
        self.h=Harness(self.path,create=True)
        self.h.put_evidence(dict(id='proof',source='user',body='Track approval and payment independently'))

    def tearDown(self):
        self.h.close();self.tmp.cleanup()

    def create_case(self):
        action=dict(owner='Other party',status='waiting',next_step='Obtain decision',completion_condition='Written decision',reopening_condition='Decision withdrawn',deadline='2026-11-10T12:00:00Z',follow_up=dict(mode='reminder',anchor_at='2026-10-26T09:00:00Z',interval_days=2,basis='calendar',reason='Expected two-day response',evidence_ids=['proof']))
        self.h.update(dict(id='A',expected_revision=0,idempotency_key='create',evidence_ids=['proof'],case=dict(title='Approval and payment',purpose='Obtain approval and payment.\nWatch for either independent outcome.',position='Both outstanding',context='Request submitted; approval and payment are separate.'),actions=[dict(action,id='approval'),dict(action,id='payment',next_step='Verify payment',completion_condition='Bank receipt verified')]))

    def test_old_and_future_formats_are_rejected_without_modifying_the_file(self):
        self.h.close()
        for version in (1,4,6,7,8,CURRENT_SCHEMA+1):
            db=sqlite3.connect(self.path);db.execute('PRAGMA user_version='+str(version));db.commit();db.close()
            before=hashlib.sha256(self.path.read_bytes()).hexdigest()
            with self.assertRaisesRegex(ValueError,'only current format'):Harness(self.path)
            self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(),before)
            self.assertFalse(verified_backup(self.path))
        db=sqlite3.connect(self.path);db.execute('PRAGMA user_version='+str(CURRENT_SCHEMA));db.commit();db.close()
        self.h=Harness(self.path)

    def test_cards_join_the_single_action_record_and_timers_are_derived(self):
        self.create_case()
        c=self.h.case('A')
        raw=json.loads(self.h.db.execute("SELECT details FROM cases WHERE id='A'").fetchone()[0])
        self.assertNotIn('actions',raw);self.assertNotIn('obligations',raw)
        obsolete=self.h.db.execute("SELECT name FROM sqlite_master WHERE name IN ('history','snapshots','expired_evidence')").fetchall()
        self.assertEqual(obsolete,[])
        canonical=[json.loads(r[0]) for r in self.h.db.execute("SELECT details FROM actions WHERE case_id='A' ORDER BY id")]
        self.assertEqual(c['actions'],canonical)
        for a in c['actions']:self.assertEqual(a['next_review'],'2026-10-28T09:00:00+00:00')
        before=copy.deepcopy(c['actions'][1])
        c['actions'][0].update(status='done',completion_reason='Written approval',completion_evidence=['proof'])
        self.h.update(dict(id='A',expected_revision=1,idempotency_key='approve',evidence_ids=['proof'],case={k:v for k,v in c.items() if k not in {'actions','decisions'}},actions=c['actions']))
        self.assertEqual(self.h.case('A')['actions'],[before])

    def test_snooze_changes_only_the_identified_action(self):
        self.create_case();c=self.h.case('A');before=copy.deepcopy(c['actions'][1])
        c['actions'][0]['snooze_until']='2026-11-01T09:00:00Z'
        self.h.update(dict(id='A',expected_revision=1,idempotency_key='snooze',evidence_ids=['proof'],case={k:v for k,v in c.items() if k not in {'actions','decisions'}},actions=c['actions']))
        current=self.h.case('A')['actions']
        self.assertEqual(current[0]['next_review'],'2026-11-01T09:00:00+00:00')
        self.assertEqual(current[1],before)

    def test_finished_pass_expiry_preserves_pending_work_health_and_task_state(self):
        self.create_case()
        rid=self.h.start_run()['id']
        self.h.record_run(rid,dict(kind='candidate',id='unread',source=MAIL[0],entry_id='mail',store_id='store',received=now(),evidence_ids=['proof']))
        result=self.h.finalize(rid)
        self.assertEqual(result['status'],'partial')
        with self.h.db:
            self.h.db.execute("UPDATE retention_activity SET at='2000-01-01T00:00:00+00:00' WHERE kind='run'")
            self.h.db.execute("INSERT INTO checkpoints VALUES(?,?,?,?)",(MAIL[0],now(),None,None))
        protected={t:[tuple(r) for r in self.h.db.execute('SELECT * FROM '+t)] for t in ('cases','actions','approvals','context','checkpoints','source_health')}
        clean(self.h)
        for t,rows in protected.items():self.assertEqual([tuple(r) for r in self.h.db.execute('SELECT * FROM '+t)],rows,t)
        self.assertTrue(self.h.run(rid)['details']['working_expired'])
        self.assertTrue(self.h.db.execute("SELECT 1 FROM pending_work WHERE id='unread'").fetchone())
        self.assertEqual(self.h.run_status(rid)['failures'],result['failures'])
        self.assertFalse(self.h.run_status(rid)['fully_verified'])
        with self.assertRaises(ValueError):self.h.finalize(rid)

    def test_running_work_never_expires_by_age(self):
        rid=self.h.start_run(at='2000-01-01T00:00:00Z')['id']
        self.h.record_run(rid,dict(kind='candidate',id='pending',source=MAIL[0],entry_id='mail',evidence_ids=['proof']))
        with self.h.db:self.h.db.execute("UPDATE retention_activity SET at='2000-01-01T00:00:00+00:00' WHERE kind='run'")
        before=[tuple(r) for r in self.h.db.execute('SELECT * FROM candidates')]
        clean(self.h)
        self.assertEqual(self.h.run(rid)['status'],'running')
        self.assertEqual([tuple(r) for r in self.h.db.execute('SELECT * FROM candidates')],before)

    def test_detached_current_capture_gets_24hours_then_expires(self):
        self.h.put_evidence(dict(id='capture',kind='freshness-probe',body='Temporary raw access diagnostic'))
        clean(self.h)
        self.assertEqual(self.h.db.execute("SELECT body FROM evidence WHERE id='capture'").fetchone()[0],'Temporary raw access diagnostic')
        with self.h.db:self.h.db.execute("UPDATE retention_activity SET at='2000-01-01T00:00:00+00:00' WHERE id='capture'")
        clean(self.h)
        row=self.h.db.execute("SELECT metadata,body FROM evidence WHERE id='capture'").fetchone()
        self.assertIsNone(row)
        self.assertEqual(self.h.db.execute("SELECT body FROM evidence WHERE id='proof'").fetchone()[0],'Track approval and payment independently')

    def test_cleanup_validation_failure_rolls_back_unresolved_work_transfer(self):
        rid=self.h.start_run()['id']
        self.h.record_run(rid,dict(kind='candidate',id='pending',source=MAIL[0],entry_id='mail',evidence_ids=['proof']))
        self.h.finalize(rid)
        with self.h.db:self.h.db.execute("UPDATE retention_activity SET at='2000-01-01T00:00:00+00:00' WHERE kind='run'")
        before=[tuple(r) for r in self.h.db.execute('SELECT * FROM candidates')]
        original=self.h.doctor;calls=[]
        def doctor():
            result=original();calls.append(True)
            if len(calls)>1:result.update(ok=False,problems=['Synthetic validation failure'])
            return result
        with patch.object(self.h,'doctor',doctor):
            with self.assertRaisesRegex(ValueError,'rolled back'):clean(self.h)
        self.assertEqual([tuple(r) for r in self.h.db.execute('SELECT * FROM candidates')],before)
        self.assertFalse(self.h.db.execute('SELECT 1 FROM pending_work').fetchone())

    def test_current_backup_preserves_obligations_but_excludes_cached_mail(self):
        self.create_case()
        put_message(self.h,dict(case_id='A',action_ids=['approval'],message=dict(EntryId='mail',StoreId='store',Body='Relevant cached email')))
        before=copy.deepcopy(self.h.case('A')['actions'])
        backup=Path(self.h.backup('current-format')[0]);self.assertTrue(verified_backup(backup))
        restored=Harness(backup)
        try:
            self.assertEqual(restored.case('A')['actions'],before)
            self.assertEqual(restored.db.execute('SELECT count(*) FROM thread_messages').fetchone()[0],0)
        finally:restored.close()

    def test_legacy_commands_and_plain_body_exports_are_rejected(self):
        for args in (['maintenance','threads','--migrate'],['maintenance','cards'],['case','history','A'],['snapshots','prune']):
            with patch('sys.stderr',io.StringIO()):
                with self.assertRaises(SystemExit):parser().parse_args(args)
        with self.assertRaises(ValueError):self.h.put_evidence(dict(source='mail:personal',entry_id='mail',store_id='store',body='Full email'))

    def test_old_backup_slot_is_replaced_by_verified_current_state_without_import(self):
        self.create_case();backup=Path(self.h.backup()[0])
        original=copy.deepcopy(self.h.case('A')['actions'])
        db=sqlite3.connect(backup);db.execute('PRAGMA user_version=6');db.commit();db.close()
        self.assertFalse(verified_backup(backup))
        self.h.backup()
        self.assertTrue(verified_backup(backup))
        restored=Harness(backup)
        try:self.assertEqual(restored.case('A')['actions'],original)
        finally:restored.close()

    def test_failed_backup_replacement_keeps_original_slot_and_cleans_temp(self):
        backup=Path(self.h.backup()[0])
        db=sqlite3.connect(backup);db.execute('PRAGMA user_version=6');db.commit();db.close()
        before=backup.read_bytes()
        with patch('monitor.threadcache.sanitize_backup',side_effect=OSError('Synthetic backup failure')):
            with self.assertRaises(OSError):self.h.backup()
        self.assertEqual(backup.read_bytes(),before)
        self.assertFalse(list(backup.parent.glob('*.tmp')))


if __name__=='__main__':unittest.main()
