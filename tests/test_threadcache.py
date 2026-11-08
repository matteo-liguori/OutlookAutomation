import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from monitor.core import Harness
from monitor.model import now, dumps
from monitor.threadcache import put_message, find_message, folder, register_attachment, sweep, snapshot
from monitor.__main__ import parser, execute


class ThreadCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.h = Harness(Path(self.tmp.name)/'monitor.db', create=True)
        self.h.put_evidence({'id':'user','source':'user','body':'Please track this task'})

    def tearDown(self):
        self.h.close(); self.tmp.cleanup()

    def case(self, cid='task'):
        self.h.update(dict(id=cid,expected_revision=0,idempotency_key='new-'+cid,evidence_ids=['user'],case=dict(title='Task',position='Outstanding',status='open',origin='user-requested'),actions=[dict(id=cid+'-action',owner='Example user',status='needs-user',next_step='Do the requested task',completion_condition='User confirms done',reopening_condition='User asks again')]))

    def close_case(self,cid='task'):
        c=self.h.case(cid)
        a=c['actions'][0];a.update(status='done',completion_reason='User confirms done',completion_evidence=['user'])
        self.h.update(dict(id=cid,expected_revision=c['revision'],idempotency_key='close-'+cid,evidence_ids=['user'],case=dict(title='Task',position='Complete',context='User confirmed completion.',status='closed'),actions=c['actions']))

    def message(self,cid='task'):
        return put_message(self.h,dict(case_id=cid,message=dict(EntryId='mail',StoreId='store',InternetMessageId='<stable@example>',Subject='Relevant',Body='Full useful email')))

    def asset(self,cid='task'):
        f=folder(self.h,cid)/'attachments'/'invoice.pdf';f.parent.mkdir(parents=True,exist_ok=True);f.write_bytes(b'PDF fixture')
        return register_attachment(self.h,dict(case_id=cid,path=str(f),source='mail'))

    def test_independent_copies_deduplicate_only_inside_thread_and_close(self):
        for cid in ['A','B']:
            self.case(cid);self.message(cid);self.asset(cid)
        self.message('A')
        put_message(self.h,dict(case_id='A',message=dict(EntryId='copy',StoreId='other-store',InternetMessageId='<stable@example>',Subject='Relevant',Body='Full useful email')))
        self.assertEqual(self.h.db.execute('SELECT count(*) FROM thread_messages').fetchone()[0],2)
        self.assertTrue(find_message(self.h,dict(case_id='A',entry_id='mail',store_id='store'))['found'])
        self.close_case('A')
        self.assertFalse(folder(self.h,'A').exists())
        self.assertTrue(folder(self.h,'B').exists())
        self.assertFalse(self.h.db.execute("SELECT 1 FROM thread_messages WHERE case_id='A'").fetchone())
        self.assertTrue(find_message(self.h,dict(case_id='B',entry_id='mail',store_id='store'))['found'])
        self.assertIn('closed_at',self.h.case('A'))

    def test_expiry_is_fixed_and_startup_resumes_it(self):
        self.case();self.close_case();c=self.h.case('task');closed=c['closed_at']
        sweep(self.h,at=(datetime.fromisoformat(closed)+timedelta(days=364)).isoformat())
        self.assertEqual(self.h.case('task')['closed_at'],closed)
        sweep(self.h,at=(datetime.fromisoformat(closed)+timedelta(days=365)).isoformat())
        self.assertFalse(self.h.db.execute("SELECT 1 FROM cases WHERE id='task'").fetchone())
        self.assertFalse(self.h.db.execute("SELECT 1 FROM sqlite_master WHERE name='history'").fetchone())
        self.assertTrue(self.h.doctor()['ok'])

    def test_long_open_threads_keep_bodies_and_files(self):
        self.case();self.message();self.asset()
        result=sweep(self.h,at=(datetime.fromisoformat(now())+timedelta(days=91)).isoformat())
        self.assertNotIn('history_rows_compacted',result)
        self.assertFalse(self.h.db.execute("SELECT 1 FROM sqlite_master WHERE name='history'").fetchone())
        self.assertTrue(find_message(self.h,dict(case_id='task',entry_id='mail',store_id='store'))['found'])
        self.assertTrue(folder(self.h,'task').exists())

    def test_failed_deletion_is_visible_and_retried(self):
        self.case();self.asset()
        with patch('monitor.threadcache.shutil.rmtree',side_effect=OSError('busy')):
            self.close_case()
        self.assertTrue(self.h.db.execute('SELECT 1 FROM thread_cleanup').fetchone())
        self.assertTrue(any(i.get('thread_cache_warning') for i in self.h.current_items(True)))
        sweep(self.h)
        self.assertFalse(folder(self.h,'task').exists())
        self.assertFalse(self.h.db.execute('SELECT 1 FROM thread_cleanup').fetchone())

    def test_startup_finishes_closure_interrupted_after_commit(self):
        self.case();self.message();self.asset()
        with patch('monitor.threadcache.sweep',return_value={}):self.close_case()
        self.assertTrue(folder(self.h,'task').exists())
        self.h.close();self.h=Harness(Path(self.tmp.name)/'monitor.db')
        self.assertFalse(folder(self.h,'task').exists())
        self.assertFalse(self.h.db.execute('SELECT 1 FROM thread_messages').fetchone())
        self.assertFalse(self.h.db.execute('SELECT 1 FROM thread_cleanup').fetchone())

    def test_pending_send_survives_cleanup(self):
        self.case();self.message()
        self.h.put_record('approval',dict(id='send',case_id='task',status='approved',evidence_ids=['user'],message=dict(sender='me@example.invalid',to=['you@example.invalid'],cc=[],bcc=[],subject='Approved',body='Exact pending wording',attachments=[])))
        self.close_case()
        self.assertTrue(self.h.db.execute('SELECT 1 FROM thread_messages').fetchone())
        self.assertIn('Exact pending wording',self.h.db.execute("SELECT details FROM approvals WHERE id='send'").fetchone()[0])

    def test_no_email_task_appears_in_briefing(self):
        self.case()
        self.assertTrue(any(r.get('action')=='task-action' for r in self.h.current_items(True)))

    def test_stdin_and_snapshot_roundtrip_and_expiry(self):
        args=parser().parse_args(['evidence','put','--input','-'])
        with patch('sys.stdin',io.StringIO(json.dumps(dict(id='stdin',source='user',body='A decision')))):
            self.assertEqual(execute(self.h,args)['id'],'stdin')
        data={'SnapshotId':'snapshot','Items':[{'EntryId':'mail'}]}
        snapshot(self.h,'put',data)
        self.assertEqual(snapshot(self.h,'get',sid='snapshot'),data)
        sweep(self.h,at=(datetime.fromisoformat(now())+timedelta(days=2)).isoformat())
        with self.assertRaises(ValueError):snapshot(self.h,'get',sid='snapshot')
        self.assertFalse(list(Path(self.tmp.name).rglob('*.json')))

    def test_backup_excludes_disposable_cache_and_expired_completion(self):
        self.case();self.message();self.asset()
        path=Path(self.h.backup('cache-test')[0])
        restored=Harness(path)
        try:
            self.assertEqual(restored.db.execute('SELECT count(*) FROM thread_messages').fetchone()[0],0)
            self.assertEqual(restored.case('task')['status'],'open')
        finally:restored.close()


    def test_rejects_orphan_body_and_attachment_escape(self):
        self.case()
        with self.assertRaises(ValueError):self.h.put_evidence(dict(kind='message-body',body='orphan'))
        path=Path(self.tmp.name)/'elsewhere.pdf';path.write_bytes(b'x')
        with self.assertRaises(ValueError):register_attachment(self.h,dict(case_id='task',path=str(path)))
        self.assertTrue(path.exists())


    def test_attachment_hash_dedup_and_missing_attachment_refresh(self):
        self.case();first=self.asset()
        duplicate=folder(self.h,'task')/'attachments'/'duplicate.pdf';duplicate.write_bytes(b'PDF fixture')
        second=register_attachment(self.h,dict(case_id='task',path=str(duplicate)))
        self.assertEqual(first['path'],second['path']);self.assertFalse(duplicate.exists())
        receipt=put_message(self.h,dict(case_id='task',message=dict(EntryId='mail',StoreId='store',Body='full',CachedAttachments=[dict(downloaded=True,path=first['path'])])))
        found=find_message(self.h,dict(case_id='task',entry_id='mail',store_id='store'))
        self.assertEqual(found['message']['CacheEvidenceId'],receipt['body_evidence_id'])
        Path(first['path']).unlink()
        self.assertFalse(find_message(self.h,dict(case_id='task',entry_id='mail',store_id='store'))['found'])

    def test_case_ids_differing_by_case_have_independent_windows_folders(self):
        for cid in ['A','a']:
            self.case(cid);self.asset(cid)
        self.assertNotEqual(str(folder(self.h,'A')).lower(),str(folder(self.h,'a')).lower())
        self.close_case('A')
        self.assertTrue(folder(self.h,'a').exists())

    def test_reopening_starts_empty_and_manual_task_snoozes(self):
        self.case();self.message();self.asset();self.close_case()
        c=self.h.case('task');self.assertEqual(c['actions'],[])
        a=dict(id='task-new-work',owner='Example user',status='needs-user',next_step='Do the new requested task',completion_condition='User confirms done',reopening_condition='User asks again',snooze_until='2099-01-01T00:00:00+00:00')
        self.h.update(dict(id='task',expected_revision=c['revision'],idempotency_key='reopen-task',reopen_reason='User requested more work',evidence_ids=['user'],case=dict(title='Task',position='New work',status='open'),actions=[a]))
        self.assertNotIn('closed_at',self.h.case('task'))
        self.assertFalse(find_message(self.h,dict(case_id='task',entry_id='mail',store_id='store'))['found'])
        self.assertFalse(any(r.get('action')=='task-new-work' for r in self.h.current_items(True)))
        self.assertTrue(any(r.get('action')=='task-new-work' for r in self.h.current_items(True,at='2099-01-02T00:00:00Z')))

    def test_startup_and_backup_cannot_revive_expired_completion(self):
        self.case();self.close_case()
        old=(datetime.fromisoformat(now())-timedelta(days=366)).isoformat()
        c=self.h.case('task');c['closed_at']=old;c['expires_at']=(datetime.fromisoformat(old)+timedelta(days=365)).isoformat()
        with self.h.db:self.h.db.execute("UPDATE cases SET details=? WHERE id='task'",(dumps(c),))
        path=Path(self.h.backup('expired-test')[0]);restored=Harness(path)
        try:self.assertFalse(restored.db.execute("SELECT 1 FROM cases WHERE id='task'").fetchone())
        finally:restored.close()
        self.h.close();self.h=Harness(Path(self.tmp.name)/'monitor.db')
        self.assertFalse(self.h.db.execute("SELECT 1 FROM cases WHERE id='task'").fetchone())
        self.assertTrue(self.h.doctor()['ok'])

    def test_expired_pending_approval_is_preserved_in_recovery_until_reconciled(self):
        self.case()
        self.h.put_record('approval',dict(id='send',case_id='task',status='approved',evidence_ids=['user'],message=dict(sender='me@example.invalid',to=['you@example.invalid'],cc=[],bcc=[],subject='Approved',body='Exact pending wording',attachments=[])))
        self.close_case();c=self.h.case('task')
        old=(datetime.fromisoformat(now())-timedelta(days=366)).isoformat()
        c.update(closed_at=old,expires_at=(datetime.fromisoformat(old)+timedelta(days=365)).isoformat())
        with self.h.db:self.h.db.execute("UPDATE cases SET details=? WHERE id='task'",(dumps(c),))
        restored=Harness(self.h.backup('pending-expired')[0])
        try:
            self.assertEqual(restored.case('task')['status'],'closed')
            self.assertIn('Exact pending wording',restored.db.execute("SELECT details FROM approvals WHERE id='send'").fetchone()[0])
        finally:restored.close()


if __name__=='__main__':unittest.main()
