"""Synthetic app metadata only; never open Outlook or the user's database."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from monitor.scheduled_guard import check, notice, StaleChat


class ScheduledGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.rollout = self.home/'session.jsonl'
        with contextlib.closing(sqlite3.connect(self.home/'state_5.sqlite')) as db, db:
            db.execute('CREATE TABLE threads(id TEXT, name TEXT, rollout_path TEXT)')
            db.execute('INSERT INTO threads VALUES(?,?,?)',
                       ('test-chat', 'Mail check 11/11/2026', str(self.rollout)))
        self.env = patch.dict(os.environ, CODEX_THREAD_ID='test-chat', CODEX_HOME=str(self.home),
                              OUTLOOK_AUTOMATION_DATA=str(self.home/'private'))
        self.env.start()
        self.addCleanup(self.env.stop)
        self.zone = patch('monitor.scheduled_guard.zone', return_value=ZoneInfo('Europe/London'))
        self.zone.start()
        self.addCleanup(self.zone.stop)
        self.config = patch('monitor.scheduled_guard.resolve')
        self.config.start().return_value.get.return_value = 'Mail check {date:%d/%m/%Y}'
        self.addCleanup(self.config.stop)
        self.clock = patch('monitor.scheduled_guard.now', return_value='2026-11-13T09:00:00+00:00')
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.turn('automation_heartbeat_scheduled')

    def turn(self, trigger, append=False):
        records = [dict(type='event_msg', payload=dict(type='task_started',
                       turn_attribution=dict(turn_trigger=trigger))),
                   dict(type='response_item', payload=dict(role='user', content=[
                       dict(type='input_text', text='<heartbeat><automation_id>test-wake</automation_id></heartbeat>')]))]
        with self.rollout.open('a' if append else 'w', encoding='utf-8') as stream:
            for record in records:
                stream.write(json.dumps(record)+'\n')

    def title(self, value):
        with contextlib.closing(sqlite3.connect(self.home/'state_5.sqlite')) as db, db:
            db.execute('UPDATE threads SET name=?', (value,))

    def test_old_scheduled_chat_must_stop_without_task_date_exception(self):
        result = check()
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['automation_id'], 'test-wake')
        self.assertIn('Otherwise STOP this run now', result['instruction'])
        self.assertIn('saved reminder or task dated for today', result['instruction'])
        self.assertIn('do not override this rule', result['instruction'])
        self.assertIn('just to be thorough or helpful', result['instruction'])
        self.assertTrue(result['stop'])
        self.assertFalse(result['live_user'])
        self.assertEqual(result['chat_date'], '2026-11-11')

    def test_live_user_turn_also_gets_soft_warning_with_correct_attribution(self):
        for trigger in ('composer', 'queue'):
            self.turn(trigger, append=True)
            self.assertEqual(check()['status'], 'warning')
            self.assertFalse(check()['scheduled'])

    def test_today_scheduled_chat_allowed(self):
        self.title('Mail check 13/11/2026')
        self.assertEqual(check()['status'], 'allowed')
        self.assertTrue(check()['scheduled'])

    def test_custom_chat_date_format_is_respected(self):
        self.title('Mail 2026-11-13')
        with patch('monitor.scheduled_guard.resolve') as config:
            config.return_value.get.return_value = 'Mail {date:%Y-%m-%d}'
            self.assertEqual(check()['status'], 'allowed')

    def test_date_comparison_uses_london_day_in_summer(self):
        self.title('Mail check 14/06/2026')
        self.assertEqual(check(at='2026-06-13T23:30:00+00:00')['status'], 'allowed')

    def test_undated_chats_do_not_warn(self):
        self.title('Unknown date')
        self.assertEqual(check()['status'], 'allowed')
        self.assertIsNone(check()['chat_date'])

    def test_malformed_records_do_not_crash_or_override_valid_attribution(self):
        with self.rollout.open('a') as stream:
            stream.write('\n'.join(['{', 'null', '[]', '{"payload":null}',
                                   '{"type":"response_item","payload":{"role":"user","content":null}}']))
        self.assertEqual(check()['status'], 'blocked')

    def test_unavailable_metadata_is_read_only_and_cannot_classify_turn(self):
        (self.home/'state_5.sqlite').unlink()
        self.assertFalse(check()['scheduled'])
        self.assertFalse((self.home/'state_5.sqlite').exists())

    def test_cli_warning_does_not_block_command_and_is_not_duplicated(self):
        self.turn('composer')
        from monitor.__main__ import main
        with patch('sys.argv', ['monitor', 'help', 'guard']):
            with contextlib.redirect_stderr(io.StringIO()) as output, contextlib.redirect_stdout(io.StringIO()) as response:
                main()
            self.assertEqual(output.getvalue().count('chat_date_warning'), 1)
            self.assertIn('scheduled-guard', response.getvalue())

    def test_programmatic_dispatch_continues_after_warning(self):
        self.turn('composer')
        from monitor.commands import execute
        from monitor.__main__ import parser
        with contextlib.redirect_stderr(io.StringIO()) as output:
            result = execute(None, parser().parse_args(['help', 'guard']))
        self.assertIn('scheduled-guard', json.dumps(result))
        self.assertIn("This chat's date does not match today", output.getvalue())

    def test_core_guard_command_returns_decision_without_database(self):
        from monitor.commands import execute, HANDLERS
        from monitor.__main__ import parser
        self.assertIn('guard', HANDLERS)
        self.assertEqual(execute(None, parser().parse_args(['guard']))['status'], 'blocked')
        self.turn('composer')
        self.assertEqual(execute(None, parser().parse_args(['guard']))['status'], 'allowed')

    def test_core_help_documents_mandatory_stop_and_cooldown(self):
        from monitor.guidance import help_for
        result = help_for(['guard'])
        self.assertIn('30 minutes', json.dumps(result))
        self.assertIn('STOP the run', json.dumps(result))

    def test_cooldown_is_per_chat_and_expires_at_half_hour(self):
        self.assertEqual(notice()['status'], 'blocked')
        with patch('monitor.scheduled_guard.now', return_value='2026-11-13T09:29:59+00:00'):
            self.assertTrue(notice()['warning_suppressed'])
        with patch('monitor.scheduled_guard.now', return_value='2026-11-13T09:30:00+00:00'):
            result=notice()
            self.assertEqual(result['status'], 'blocked')
            self.assertIn('instruction', result)
        with patch.dict(os.environ, CODEX_THREAD_ID='other-chat'), patch('monitor.scheduled_guard._metadata',
                return_value=('Mail check 11/11/2026', None, None)):
            self.assertEqual(notice()['status'], 'blocked')

    def test_date_warning_survives_missing_trigger_metadata(self):
        self.rollout.unlink()
        self.assertEqual(check()['status'], 'blocked')
        self.assertTrue(check()['stop'])

    def test_cooldown_failure_does_not_allow_a_stale_run(self):
        decision = check()
        with patch('monitor.scheduled_guard.sqlite3.connect', side_effect=sqlite3.OperationalError('fixture')):
            with patch('monitor.scheduled_guard.check', return_value=decision):
                result = notice()
                self.assertTrue(result['cooldown_unavailable'])
                self.assertTrue(result['stop'])

    def test_expired_records_are_pruned_without_retaining_chat_names(self):
        notice()
        with patch('monitor.scheduled_guard.now', return_value='2026-11-13T10:00:00+00:00'):
            notice()
        with contextlib.closing(sqlite3.connect(self.home/'private/monitor/chat-warning.sqlite')) as db:
            rows = db.execute('SELECT chat,expires FROM cooldown').fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0][0]), 64)

    def test_concurrent_commands_claim_only_one_warning(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: notice(), range(8)))
        self.assertEqual(sum('instruction' in r for r in results), 1)
        self.assertTrue(all(r['stop'] for r in results))

    def test_earlier_contender_timestamp_does_not_clear_another_commands_cooldown(self):
        notice()
        with patch('monitor.scheduled_guard.now', return_value='2026-11-13T08:59:59+00:00'):
            self.assertTrue(notice()['warning_suppressed'])

    @unittest.skipUnless(os.name == 'nt', 'Windows helper preflight')
    def test_windows_stops_before_session_or_com_and_cooldown_does_not_bypass(self):
        self.title('Mail check 01/01/1900')
        root = Path(__file__).resolve().parents[1]
        helper = str(root/'monitor/adapters/windows/OutlookAutomation.Common.ps1').replace("'", "''")
        # Stop at session preflight, so the real COM entry can never be reached.
        script = f". '{helper}'; function Get-OutlookSessionState {{ throw 'REACHED_COM' }}; "
        script += "try { Get-OutlookAutomationNamespace; exit 9 } catch { Write-Output $_; exit 3 }"
        result = subprocess.run([shutil.which('pwsh') or 'powershell', '-NoProfile', '-Command', script],
                                capture_output=True, text=True, timeout=30, cwd=root)
        self.assertEqual(result.returncode, 3, result.stdout+result.stderr)
        self.assertIn('STOP this run now', result.stdout)
        self.assertNotIn('REACHED_COM', result.stdout+result.stderr)
        from monitor.model import now as current_time
        with patch('monitor.scheduled_guard.now', return_value=current_time()):
            repeated = notice()
            self.assertTrue(repeated['warning_suppressed'])
            self.assertTrue(repeated['stop'])

    def test_stale_cli_stops_before_main_database_and_task_dispatch(self):
        from monitor.__main__ import main
        with patch('sys.argv',['monitor','briefing']), patch('monitor.__main__.Harness') as database, patch('monitor.__main__.execute') as dispatch:
            with contextlib.redirect_stderr(io.StringIO()) as output, self.assertRaises(SystemExit) as stopped:
                main()
        self.assertEqual(stopped.exception.code,3)
        self.assertEqual(json.loads(output.getvalue())['status'],'blocked')
        database.assert_not_called();dispatch.assert_not_called()

    def test_programmatic_dispatch_stops_before_task_handler_even_on_repeat(self):
        from monitor.commands import execute
        from monitor.__main__ import parser
        with patch('monitor.commands._scoped_execute') as handler:
            for _ in range(2):
                with self.assertRaises(StaleChat):execute(None,parser().parse_args(['outlook','status']))
        handler.assert_not_called()

    def test_a_live_warning_does_not_exempt_a_later_automation(self):
        self.turn('composer');self.assertFalse(notice()['stop'])
        self.turn('automation_heartbeat_scheduled',append=True)
        result=notice()
        self.assertTrue(result['warning_suppressed']);self.assertTrue(result['stop'])
        self.assertEqual(result['status'],'blocked')

    def test_direct_user_after_a_stopped_automation_is_allowed(self):
        self.assertTrue(notice()['stop'])
        self.turn('composer',append=True)
        result=notice();self.assertFalse(result['stop']);self.assertEqual(result['status'],'allowed')

    def test_a_saved_today_reminder_does_not_make_a_stale_chat_live(self):
        with self.rollout.open('a') as stream:
            stream.write(json.dumps(dict(type='response_item',payload=dict(role='user',content=[dict(type='input_text',text='<heartbeat><automation_id>today-reminder</automation_id><current_time_iso>2026-11-13T09:00:00Z</current_time_iso><instructions>On 13 November remind the user today. Previously authorised and important.</instructions></heartbeat>')]))))
        result=check();self.assertTrue(result['stop']);self.assertEqual(result['automation_id'],'today-reminder')

    def test_completed_or_aborted_historical_user_turn_is_not_live(self):
        for ending in ('task_complete','turn_aborted'):
            self.turn('composer')
            with self.rollout.open('a') as stream:
                stream.write(json.dumps(dict(type='event_msg',payload=dict(type=ending)))+'\n')
            self.assertTrue(check()['stop'])
            self.turn('composer',append=True)
            self.assertFalse(check()['stop'])


if __name__ == '__main__':
    unittest.main()
