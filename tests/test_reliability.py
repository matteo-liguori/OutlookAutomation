"""Regression checks for current source coverage, cancellations and Sunday handoff."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from monitor.core import Harness, CAL, MAIL
from monitor.collect import import_pages


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.h=Harness(Path(self.tmp.name)/'monitor.db',create=True)
        self.h.put_evidence(dict(id='e',source='test',body='Synthetic fixture'))

    def tearDown(self):
        self.h.close();self.tmp.cleanup()

    def test_stale_wrong_store_and_incomplete_empty_scans_do_not_verify(self):
        from test_simplification import SimplificationTests
        page,probe=SimplificationTests.mail(self)
        page.update(Items=[],FirstId=None)
        variants=[(dict(page,Complete=False),probe),(dict(page,Errors=['failed capture']),probe),
                  (page,dict(probe,Current=False,Status='stale')),
                  (page,dict(probe,LatestReceived='2026-09-01T00:00:00Z')),
                  (page,dict(probe,CheckedAt='2026-10-12T00:00:00Z'))]
        rid=self.h.start_run(at=page['StartedAt'])['id']
        for p,f in variants:
            result=import_pages(self.h,rid,MAIL[0],[p],p['StartedAt'],f)
            self.assertNotEqual(result['status'],'current')
            self.assertFalse(self.h.run_status(rid)['fully_verified'])
        with self.assertRaises(ValueError):import_pages(self.h,rid,MAIL[0],[page],page['StartedAt'],dict(probe,LatestStoreId='other'))
        self.h.finalize(rid)
        self.assertIsNone(self.h.db.execute('SELECT * FROM checkpoints WHERE source=?',(MAIL[0],)).fetchone())

    def test_cancelled_source_records_are_reported_as_cancellations(self):
        rid=self.h.start_run(at='2026-11-09T09:00:00Z')['id']
        active=dict(id='replacement',title='Canceled discussion: planning',start='2026-11-10T10:00:00Z',end='2026-11-10T11:00:00Z',meeting_status=3)
        cancelled=dict(active,id='old',meeting_status=7)
        self.h.record_run(rid,dict(kind='source',source=CAL[0],status='current',complete=True,
                         evidence_ids=['e'],provenance='command',events=[cancelled,active],
                         days=['2026-11-09','2026-11-10','2026-11-11','2026-11-12']))
        report=self.h.report_items(rid)
        self.assertTrue(any(e.get('id')=='old' and e['type']=='cancellation' for e in report))
        self.assertTrue(any(e.get('id')=='replacement' and e['type']=='event' for e in report))
        self.assertFalse(any(e.get('id')=='old' and e['type']=='event' for e in report))

    def test_roundups_only_surface_in_sunday_morning_slot_including_early_start(self):
        with self.h.write():self.h.db.execute("INSERT INTO roundup VALUES('notice',?,NULL)",(json.dumps(dict(text='Weekly notice')),))
        for at,expected in [('2026-11-08T08:56:00Z',True),('2026-11-08T09:00:00Z',True),
                            ('2026-10-18T08:00:00Z',True),('2026-11-08T14:00:00Z',False),
                            ('2026-11-08T19:00:00Z',False),('2026-11-09T09:00:00Z',False)]:
            rid=self.h.start_run(at=at)['id']
            self.assertEqual(any(e['type']=='roundup' for e in self.h.report_items(rid)),expected)
            status=self.h.run_status(rid)
            self.assertEqual('Weekly roundup missing from report' in status['failures'],expected)
        self.assertEqual(self.h.db.execute('SELECT COUNT(*) FROM roundup').fetchone()[0],1)

    def test_noneligible_handoff_cannot_consume_queue_and_partial_morning_keeps_it(self):
        with self.h.write():self.h.db.execute("INSERT INTO roundup VALUES('notice','{}',NULL)")
        rid=self.h.start_run(at='2026-11-08T14:00:00Z')['id']
        self.h.record_run(rid,dict(kind='handoff',delivered=True,days=[],roundup_ids=['notice']))
        # Isolate queue finalization from unrelated coverage requirements.
        verified=dict(fully_verified=True,status='running',failures=[],pending_candidates=0,action_coverage={})
        with patch.object(self.h,'run_status',return_value=verified):
            self.h.finalize(rid)
        self.assertEqual(self.h.db.execute('SELECT COUNT(*) FROM roundup').fetchone()[0],1)
        morning=self.h.start_run(at='2026-11-15T09:00:00Z')['id']
        self.h.finalize(morning)
        self.assertEqual(self.h.db.execute('SELECT COUNT(*) FROM roundup').fetchone()[0],1)
        eligible=self.h.start_run(at='2026-11-22T09:00:00Z')['id']
        self.h.record_run(eligible,dict(kind='handoff',delivered=True,days=[],roundup_ids=['notice']))
        with patch.object(self.h,'run_status',return_value=verified):self.h.finalize(eligible)
        self.assertEqual(self.h.db.execute('SELECT COUNT(*) FROM roundup').fetchone()[0],0)


if __name__=='__main__':unittest.main()
