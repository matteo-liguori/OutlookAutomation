"""Read-only desktop metadata probes use synthetic files, never live app mutations."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from monitor.app_state import inspect


class AppStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.home=Path(self.tmp.name)
        self.clock=patch('monitor.timing.now',return_value='2026-11-08T17:00:00+00:00');self.clock.start()
        c=sqlite3.connect(self.home/'state_5.sqlite')
        c.execute('CREATE TABLE threads(id TEXT,name TEXT)')
        c.execute('INSERT INTO threads VALUES(?,?)',('A','Mail check 08/11/2026'));c.commit();c.close()
        (self.home/'automations').mkdir()

    def tearDown(self):self.clock.stop();self.tmp.cleanup()

    def automation(self,name='one',target='A',rule='FREQ=DAILY;COUNT=1;BYHOUR=19;BYMINUTE=0'):
        p=self.home/'automations'/name;p.mkdir()
        (p/'automation.toml').write_text(f'id="{name}"\nkind="heartbeat"\nstatus="ACTIVE"\ntarget_thread_id="{target}"\nrrule="{rule}"\n',encoding='utf-8')

    def test_exact_title_and_unique_active_correct_target_rule(self):
        self.automation()
        c=sqlite3.connect(self.home/'thread_history_1.sqlite')
        c.execute('CREATE TABLE thread_turns(thread_id TEXT,status TEXT,rollout_ordinal INTEGER)')
        c.execute('INSERT INTO thread_turns VALUES(?,?,?)',('A','inProgress',1));c.commit();c.close()
        r=inspect('A','Mail check 08/11/2026',self.home)
        self.assertTrue(r['title']);self.assertTrue(r['followups'])
        self.assertEqual(r['runtime_status'],'inProgress')
        self.assertIsNone(r['user_present'])
        self.assertFalse(inspect('A','Wrong title',self.home)['title'])

    def test_other_chat_wrong_rule_and_duplicates_are_not_success(self):
        self.automation('other','B')
        self.automation('wrong','A','FREQ=DAILY;COUNT=2;BYHOUR=19;BYMINUTE=0')
        self.assertFalse(inspect('A','Mail check 08/11/2026',self.home)['followups'])
        self.automation('valid');self.assertTrue(inspect('A','Mail check 08/11/2026',self.home)['followups'])
        self.automation('duplicate');r=inspect('A','Mail check 08/11/2026',self.home)
        self.assertFalse(r['followups']);self.assertTrue(r['duplicate_followups'])

    def test_unavailable_or_malformed_sources_remain_unknown(self):
        self.assertIsNone(inspect('A','Title',self.home/'missing')['title'])
        self.assertIsNone(inspect('A','Title',self.home/'missing')['followups'])
        p=self.home/'automations'/'broken';p.mkdir();(p/'automation.toml').write_text('invalid [')
        self.assertIsNone(inspect('A','Title',self.home)['followups'])
        self.automation('valid')
        self.assertIsNone(inspect('A','Title',self.home)['followups'])

    def test_no_next_day_substitute_after_final_check(self):
        with patch('monitor.timing.now',return_value='2026-11-08T20:00:00+00:00'):
            self.assertTrue(inspect('A','Mail check 08/11/2026',self.home)['followups'])
