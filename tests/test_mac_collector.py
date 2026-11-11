"""Native snapshots replay immutably and ingest through the shared harness."""
from datetime import datetime,timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from monitor.core import Harness,MAIL
from monitor.sources import MAIL_STORES
from monitor.adapters.macos.collector import collect_page,ingest_snapshot


class NativeCollectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.h=Harness(Path(self.tmp.name)/'monitor.db',create=True)
        self.mailbox=MAIL_STORES[MAIL[0]]
        self.rows=[dict(id=str(i),subject='Fixture '+str(i),received_at=f'2026-11-08T1{i}:00:00+00:00',sender_email='sender@example.invalid',internet_message_id=f'<fixture-{i}@example.invalid>') for i in (3,2,1)]
        self.runner=Mock(return_value=dict(status='ok',items=self.rows,matched_count=3,capture_complete=True,errors=[]))
        self.args=(self.h,self.mailbox,'2026-11-08T00:00:00+00:00','2026-11-09T00:00:00+00:00','131')

    def tearDown(self):
        self.h.close();self.tmp.cleanup()

    def test_paging_reuses_one_immutable_capture(self):
        first=collect_page(*self.args,page_size=2,runner=self.runner)
        self.runner.return_value['items']=[]
        last=collect_page(*self.args,page_size=2,offset=2,snapshot_id=first['SnapshotId'],runner=self.runner)
        self.assertEqual([r['EntryId'] for r in first['Items']+last['Items']],['3','2','1'])
        self.assertEqual(self.runner.call_count,1)
        self.assertFalse(first['Complete']);self.assertTrue(last['Complete'])
        self.assertIsNone(last['NextOffset'])

    def test_ingestion_uses_existing_candidate_identity_and_freshness_policy(self):
        run=self.h.start_run()
        first=collect_page(*self.args,page_size=1,runner=self.runner)
        result=ingest_snapshot(self.h,run['id'],MAIL[0],first,page_size=1)
        self.assertEqual(result['count'],3);self.assertTrue(result['complete'])
        self.assertEqual(result['status'],'unavailable')
        self.assertEqual(self.h.db.execute('select count(*) from candidates where run_id=?',(run['id'],)).fetchone()[0],3)
        self.assertTrue(self.h.doctor()['ok'])

    def test_mismatched_window_or_binding_cannot_reuse_snapshot(self):
        first=collect_page(*self.args,runner=self.runner)
        with self.assertRaises(ValueError):collect_page(*self.args[:-1],'999',snapshot_id=first['SnapshotId'])
        with self.assertRaises(ValueError):collect_page(self.h,self.mailbox,'2026-11-07T00:00:00Z',self.args[3],'131',snapshot_id=first['SnapshotId'])
        with self.assertRaises(ValueError):collect_page(*self.args,offset=1,runner=self.runner)

    def test_scan_cap_and_empty_results_are_explicit(self):
        self.runner.return_value.update(capture_complete=False,errors=['scan cap'])
        page=collect_page(*self.args,runner=self.runner)
        self.assertFalse(page['Complete']);self.assertEqual(page['Errors'],['scan cap'])
        self.runner.return_value.update(items=[],matched_count=0,capture_complete=True,errors=[])
        page=collect_page(*self.args,runner=self.runner)
        self.assertTrue(page['Complete']);self.assertEqual(page['Items'],[])

    def test_inclusive_until_and_timezone_equivalent_replay(self):
        self.runner.return_value['items']=[dict(self.rows[0],received_at=self.args[3])]
        first=collect_page(*self.args,runner=self.runner)
        self.assertEqual(first['Count'],1)
        self.assertEqual(self.runner.call_args.args[0]['end'],'2026-11-09T00:00:00.001000+00:00')
        replay=collect_page(self.h,self.mailbox,'2026-11-08T01:00:00+01:00','2026-11-09T01:00:00+01:00','131',snapshot_id=first['SnapshotId'])
        self.assertEqual(first,replay)
