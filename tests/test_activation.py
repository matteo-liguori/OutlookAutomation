"""Pending title/follow-up reminders use actual SQLite state; no app writes."""
import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from monitor.core import Harness
from monitor.__main__ import parser, markdown
from monitor.commands import execute
from monitor.activation import arm, reminder, complete, defer, detach
from monitor.activation import reconcile


class ActivationTests(unittest.TestCase):
    def test_first_plain_briefing_supplies_only_current_startup_context_once(self):
        result=self.call('briefing')
        session=result['session_start']
        self.assertEqual(session['first_interaction_local'],'2026-11-08T17:00:00+00:00')
        self.assertEqual(session['title'],'Mail check 08/11/2026')
        self.assertEqual(session['remaining_followups'],['19:00'])
        self.assertIn('reminder',result)
        repeated=self.call('briefing')
        self.assertNotIn('session_start',repeated)
        self.assertNotIn('reminder',repeated)

    def test_scoped_and_detached_briefings_do_not_start_mail_setup(self):
        self.assertNotIn('session_start',self.call('briefing','--task','no-match'))
        detach(self.h,dict(reason='Maintenance only',evidence_ids=['e']))
        self.assertNotIn('session_start',self.call('briefing'))

    def test_detached_maintenance_chat_is_not_rearmed_or_renamed(self):
        arm(self.h)
        detach(self.h,dict(reason='User reserves this chat for maintenance',evidence_ids=['e']))
        arm(self.h,user_active=True);reconcile(self.h)
        self.assertIsNone(reminder(self.h))
        with patch('monitor.activation.now',return_value='2026-11-09T09:00:00+00:00'):
            arm(self.h,user_active=True)
            self.assertIsNone(reminder(self.h))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.h = Harness(Path(self.tmp.name)/'monitor.db', create=True)
        self.h.put_evidence(dict(id='e',source='app-readback',body='Synthetic exact readback'))
        self.stack = [patch.dict(os.environ, CODEX_THREAD_ID='chat-A'),
                      patch('monitor.activation.now', return_value='2026-11-08T17:00:00+00:00'),
                      patch('monitor.timing.now', return_value='2026-11-08T17:00:00+00:00'),
                      patch('monitor.commands.now', return_value='2026-11-08T17:00:00+00:00')]
        for p in self.stack: p.start()
        self.payload = dict(title_readback='Mail check 08/11/2026',evidence_ids=['e'],
                            followup=dict(status='ACTIVE',target_thread_id='chat-A',
                                          rrule='FREQ=DAILY;COUNT=1;BYHOUR=19;BYMINUTE=0'))

    def tearDown(self):
        for p in reversed(self.stack): p.stop()
        self.h.close(); self.tmp.cleanup()

    def call(self,*words):
        return execute(self.h,parser().parse_args(list(words)))

    def test_background_quiet_user_signal_persists_and_markdown_displays_reminder(self):
        self.assertNotIn('reminder',self.call('doctor'))
        self.assertNotIn('reminder',self.call('briefing','--task','no-match'))
        result=self.call('briefing','--task','no-match','--user-active')
        self.assertIn('reminder', result)
        self.assertIn(result['reminder'],markdown(result))
        self.assertNotIn('reminder',self.call('doctor'))
        self.assertNotIn('reminder',self.call('case','search','no-match'))
        arm(self.h)
        self.assertIsNotNone(reminder(self.h))

    def test_completion_clears_checklist_and_repeated_signal_does_not_rearm_it(self):
        arm(self.h)
        self.assertTrue(complete(self.h,self.payload)['completed'])
        self.assertNotIn('reminder',self.call('doctor'))
        arm(self.h)
        self.assertIsNone(reminder(self.h))

    def test_invalid_readbacks_leave_reminder_pending(self):
        arm(self.h)
        for change in ('title','evidence','target','recurrence'):
            p=copy.deepcopy(self.payload)
            if change=='title':p['title_readback']='Wrong date'
            if change=='evidence':p['evidence_ids']=['missing']
            if change=='target':p['followup']['target_thread_id']='chat-B'
            if change=='recurrence':p['followup']['rrule']='FREQ=DAILY;COUNT=2;BYHOUR=19;BYMINUTE=0'
            with self.subTest(change=change), self.assertRaises(ValueError):complete(self.h,p)
            self.assertIsNotNone(reminder(self.h))

    def test_chat_and_date_isolation_and_no_new_day_substitute(self):
        arm(self.h)
        with patch.dict(os.environ,CODEX_THREAD_ID='chat-B'):
            self.assertIsNone(reminder(self.h))
        with patch('monitor.activation.now',return_value='2026-11-09T09:00:00+00:00'):
            self.assertIsNone(reminder(self.h))
            arm(self.h)
            self.assertIsNotNone(reminder(self.h))
        self.assertEqual(self.h.db.execute("SELECT count(*) FROM meta WHERE key LIKE 'mail-activation:%'").fetchone()[0],1)

    def test_after_last_check_requires_explicit_none_needed(self):
        with patch('monitor.activation.now',return_value='2026-11-08T20:00:00+00:00'), patch('monitor.timing.now',return_value='2026-11-08T20:00:00+00:00'):
            arm(self.h)
            with self.assertRaises(ValueError):complete(self.h,self.payload)
            self.payload['followup']={'status':'none-needed'}
            self.assertTrue(complete(self.h,self.payload)['completed'])

    def test_user_can_decline_followups_with_evidence_but_not_silently(self):
        arm(self.h)
        self.payload['followup']={'status':'declined'}
        with self.assertRaises(ValueError):complete(self.h,self.payload)
        self.payload['decline_evidence_ids']=['e']
        self.assertTrue(complete(self.h,self.payload)['completed'])

    def test_automatic_reconciliation_clears_done_work_and_only_reports_missing_part(self):
        arm(self.h)
        with patch('monitor.app_state.inspect',return_value=dict(title=True,followups=False)):
            reconcile(self.h)
        self.assertIn('same-day follow-ups',reminder(self.h))
        self.assertNotIn('title',reminder(self.h))
        with patch('monitor.app_state.inspect',return_value=dict(title=True,followups=True)):
            reconcile(self.h)
        self.assertIsNone(reminder(self.h))
        with patch('monitor.app_state.inspect',return_value=dict(title=False,followups=True)):
            reconcile(self.h)
        self.assertIn('title',reminder(self.h))
        self.assertNotIn('follow-ups',reminder(self.h))

    def test_unknown_metadata_does_not_invent_missing_work_or_erase_last_completion(self):
        arm(self.h);complete(self.h,self.payload)
        with patch('monitor.app_state.inspect',return_value=dict(title=None,followups=None)):
            reconcile(self.h)
        self.assertIsNone(reminder(self.h))

    def test_startup_attempt_can_defer_until_user_reply_without_claiming_user_presence(self):
        result=self.call('briefing','--task','no-match','--setup-chat')
        self.assertIn('reminder',result)
        with self.assertRaises(ValueError):defer(self.h,{'reason':'No evidence'})
        defer(self.h,{'reason':'Actual startup tool unavailable','evidence_ids':['e']})
        self.assertIsNone(reminder(self.h))
        self.assertNotIn('reminder',self.call('briefing','--task','no-match','--setup-chat'))
        self.assertNotIn('reminder',self.call('briefing','--task','no-match','--user-active'))
        self.assertIsNotNone(reminder(self.h))
        self.assertTrue(complete(self.h,self.payload)['completed'])
        with patch('monitor.app_state.inspect',return_value=dict(title=True,followups=True)):
            self.assertNotIn('reminder',self.call('briefing','--task','no-match','--user-active'))
