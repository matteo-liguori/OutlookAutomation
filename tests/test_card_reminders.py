"""Reminders default to existing card work; app planning requires explicit intent."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from monitor.__main__ import parser
from monitor.commands import execute
from monitor.core import Harness
from monitor.configuration import resolve, snapshot


class CardReminderTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        env=patch.dict(os.environ,CODEX_THREAD_ID='card-reminder-test',CODEX_HOME=self.tmp.name,
                       OUTLOOK_AUTOMATION_DATA=self.tmp.name)
        env.start();self.addCleanup(env.stop)

    def call(self,*args):return execute(None,parser().parse_args(list(args)))

    def test_exact_time_alone_returns_card_due_fields_without_app_rules(self):
        result=self.call('schedule','plan','--purpose','reminder','--date','2026-11-14',
                         '--time','10:00','--at','2026-11-13T09:00:00Z')
        self.assertEqual(result['delivery'],'card')
        self.assertFalse(result['automation_requested'])
        self.assertEqual(result['due'][0]['utc'],'2026-11-14T10:00:00+00:00')
        self.assertEqual(result['follow_up_mode'],'handoff')
        self.assertIsNone(result['rrule']);self.assertEqual(result['one_shot_rules'],[])

    def test_no_exact_time_still_defaults_to_card_not_new_wake(self):
        result=self.call('schedule','plan','--purpose','reminder','--at','2026-11-13T09:00:00Z')
        self.assertEqual(result['delivery'],'card');self.assertEqual(result['due'],[])
        self.assertFalse(result['one_shot_rules'])

    def test_explicit_separate_automation_can_prepare_bounded_rules(self):
        result=self.call('schedule','plan','--purpose','reminder','--automation-requested',
                         '--date','2026-11-14','--time','10:00','--at','2026-11-13T09:00:00Z')
        self.assertIn('COUNT=1',result['rrule'])
        self.assertTrue(result['automation_requested']);self.assertEqual(result['delivery'],'automation')
        self.assertEqual(len(result['one_shot_rules']),1)

    def test_regular_mail_continuations_are_unchanged(self):
        result=self.call('schedule','plan','--at','2026-11-13T09:00:00Z')
        self.assertEqual(result['purpose'],'continuation')
        self.assertEqual([p['time'] for p in result['eligible']],['14:00','19:00'])
        self.assertIn('COUNT=2',result['rrule'])

    def test_card_due_fields_use_configured_timezone_and_preserve_overdue_time(self):
        with tempfile.TemporaryDirectory() as folder:
            config=resolve(Path(folder),profile={}).overlay({'timezone':'America/New_York'},'user')
            with snapshot(config):
                result=self.call('schedule','plan','--purpose','reminder','--date','2026-11-13',
                                 '--time','10:00','--at','2026-11-13T16:00:00Z')
        self.assertEqual(result['due'][0]['utc'],'2026-11-13T15:00:00+00:00')
        self.assertTrue(result['due'][0]['overdue']);self.assertIsNone(result['rrule'])

    def test_help_routes_explain_default_and_reject_implied_automation_permission(self):
        for words in [('reminders',),('reminder',),('remind',),('action-scheduling',)]:
            result=self.call('help',*words)
            self.assertEqual(result['topic'],'reminders')
            self.assertIn('unless the user explicitly asks',result['instruction'])
            self.assertIn('weekly cadence alone is not',result['instruction'])
        self.assertIn('help reminders',self.call('help','scheduling')['instruction'])

    def test_saved_card_reminder_surfaces_when_due_and_reading_does_not_complete_it(self):
        from monitor.reviews import active_due
        with tempfile.TemporaryDirectory() as folder:
            h=Harness(Path(folder)/'monitor.db',create=True)
            try:
                h.put_evidence(dict(id='request',source='user',body='Remind me to book the appointment in normal mail checks.'))
                due=self.call('schedule','plan','--purpose','reminder','--date','2026-11-14',
                              '--time','10:00','--at','2026-11-13T09:00:00Z')['due'][0]['utc']
                h.update(dict(id='reminder-card',expected_revision=0,idempotency_key='save-reminder',evidence_ids=['request'],
                              case=dict(title='Book appointment',position='Booking remains pending.',status='open'),
                              actions=[dict(id='book',owner='User',status='needs-user',next_step='Book the appointment.',
                                            completion_condition='User confirms booking or dismisses.',reopening_condition='New request.',
                                            explicit_review=due,snooze_until=due,
                                            follow_up=dict(mode='handoff',basis='calendar',interval_days=7,
                                                           reason='Remind through existing mail checks.',evidence_ids=['request']))]))
                action=h.case('reminder-card')['actions'][0]
                self.assertFalse(active_due(action,'2026-11-14T09:59:59+00:00'))
                self.assertTrue(active_due(action,due))
                with patch('monitor.commands.now',return_value=due):
                    for _ in range(2):
                        result=execute(h,parser().parse_args(['briefing']))
                        self.assertTrue(any(i.get('action')=='book' and i.get('type')=='due_action' for i in result['items']))
                self.assertEqual(h.case('reminder-card')['actions'][0]['status'],'needs-user')
            finally:h.close()

    def test_shared_default_is_available_despite_private_scheduling_wording(self):
        with tempfile.TemporaryDirectory() as folder:
            config=resolve(Path(folder),profile={}).overlay({'instructions':{'scheduling':'My private schedule wording.'}},'user')
            with snapshot(config):
                result=self.call('schedule','plan','--purpose','reminder','--at','2026-11-13T09:00:00Z')
            self.assertEqual(result['delivery'],'card')
            self.assertIn('Reminder requests go on the relevant card/action',result['instruction'])
            self.assertEqual(config.instruction('scheduling'),'My private schedule wording.')

    def test_existing_installation_gains_shared_reminder_default_without_losing_overrides(self):
        from monitor.configuration import defaults,sync,read_yaml,paths,atomic,dump
        root=Path(self.tmp.name);public=root/'public.yaml';private=root/'existing'
        old=defaults();del old['instructions']['reminders'];atomic(public,dump(old))
        sync(private,profile={},default_file=public)
        path,_=paths(private);user=read_yaml(path)
        user['instructions']['scheduling']='My existing scheduling preference.'
        user['reviews']['card_days']=3;atomic(path,dump(user))
        atomic(public,dump(defaults()));sync(private,profile={},default_file=public)
        upgraded=resolve(private,profile={},default_file=public)
        self.assertIn('relevant card/action',upgraded.instruction('reminders'))
        self.assertEqual(upgraded.origins['instructions.reminders'],'default')
        self.assertEqual(upgraded.instruction('scheduling'),'My existing scheduling preference.')
        self.assertEqual(upgraded.get('reviews.card_days'),3)


if __name__=='__main__':unittest.main()
