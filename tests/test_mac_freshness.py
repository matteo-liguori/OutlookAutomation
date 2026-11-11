"""Stable native freshness never substitutes version metadata for recent mail."""
from datetime import datetime,timedelta,timezone
import unittest
from monitor.adapters.macos.freshness import probe

class FreshnessTests(unittest.TestCase):
    def test_stable_recent_mail_and_preserved_state(self):
        at=[datetime(2026,11,9,tzinfo=timezone.utc)]
        def sleep(seconds):at[0]+=timedelta(seconds=seconds)
        row=dict(id='33',subject='Synthetic',received_at=at[0].isoformat())
        value=probe('fixture@example.invalid','131',runner=lambda _:dict(status='ok',items=[row],read_state_preserved=True),sleeper=sleep,clock=lambda:at[0])
        self.assertTrue(value['Current']);self.assertEqual(value['Samples'],2);self.assertEqual(value['WaitedSeconds'],15)
    def test_empty_error_changed_watermark_and_unverified_state(self):
        for result in [dict(status='ok',items=[],read_state_preserved=True),dict(status='blocked',code='owner-not-target'),dict(status='ok',items=[dict(id='1',subject='x',received_at='2026-11-09T00:00:00Z')],read_state_preserved=False)]:
            value=probe('fixture@example.invalid','131',max_wait=0,runner=lambda _:result,clock=lambda:datetime(2026,11,9,tzinfo=timezone.utc))
            self.assertFalse(value['Current'])
        at=[datetime(2026,11,9,tzinfo=timezone.utc)];calls=[0]
        def run(_):calls[0]+=1;return dict(status='ok',items=[dict(id=str(calls[0]),subject='x',received_at=at[0].isoformat())],read_state_preserved=True)
        value=probe('fixture@example.invalid','131',interval=1,max_wait=2,runner=run,sleeper=lambda s:at.__setitem__(0,at[0]+timedelta(seconds=s)),clock=lambda:at[0])
        self.assertFalse(value['Current']);self.assertEqual(value['Status'],'not-stable')
