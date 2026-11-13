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

from monitor.scheduled_guard import check, StaleAutomation


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
        self.env = patch.dict(os.environ, CODEX_THREAD_ID='test-chat', CODEX_HOME=str(self.home))
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

    def test_old_scheduled_chat_stops_with_exact_cleanup_instruction(self):
        result = check()
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['automation_id'], 'test-wake')
        self.assertEqual(result['instruction'], 'Wrong day: stale automation. Stop and cancel this automation.')
        self.assertEqual(result['chat_date'], '2026-11-11')

    def test_latest_user_turn_is_exempt_despite_old_heartbeat_and_old_title(self):
        for trigger in ('composer', 'queue'):
            self.turn(trigger, append=True)
            self.assertEqual(check()['status'], 'allowed')
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

    def test_known_scheduled_turn_without_parseable_title_stops(self):
        self.title('Unknown date')
        self.assertEqual(check()['status'], 'blocked')
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

    def test_cli_blocks_before_database_constructor(self):
        from monitor.__main__ import main
        with patch('sys.argv', ['monitor', 'briefing']), patch('monitor.__main__.Harness') as harness:
            with contextlib.redirect_stderr(io.StringIO()) as output, self.assertRaises(SystemExit) as stopped:
                main()
            self.assertEqual(stopped.exception.code, 3)
            self.assertIn('Wrong day: stale automation', output.getvalue())
            harness.assert_not_called()

    def test_programmatic_dispatch_is_also_guarded(self):
        from monitor.commands import execute
        from monitor.__main__ import parser
        with patch('monitor.commands.read_payload') as payload:
            with self.assertRaises(StaleAutomation):
                execute(None, parser().parse_args(['briefing']))
            payload.assert_not_called()

    def test_core_guard_command_returns_decision_without_database(self):
        from monitor.commands import execute, HANDLERS
        from monitor.__main__ import parser
        self.assertIn('guard', HANDLERS)
        self.assertEqual(execute(None, parser().parse_args(['guard']))['status'], 'blocked')
        self.turn('composer')
        self.assertEqual(execute(None, parser().parse_args(['guard']))['status'], 'allowed')

    def test_core_help_documents_stop_and_cancellation(self):
        from monitor.guidance import help_for
        result = help_for(['guard'])
        self.assertIn('Stop and cancel this automation', json.dumps(result))

    @unittest.skipUnless(os.name == 'nt', 'Windows helper preflight')
    def test_windows_namespace_stops_before_com(self):
        self.title('Mail check 01/01/1900')
        root = Path(__file__).resolve().parents[1]
        helper = str(root/'monitor/adapters/windows/OutlookAutomation.Common.ps1').replace("'", "''")
        # Override COM entry so a regression cannot touch the real user's Outlook.
        script = f". '{helper}'; function Get-OutlookSessionState {{ throw 'REACHED_COM' }}; "
        script += "try { Get-OutlookAutomationNamespace; exit 9 } catch { Write-Output $_; exit 3 }"
        result = subprocess.run([shutil.which('pwsh') or 'powershell', '-NoProfile', '-Command', script],
                                capture_output=True, text=True, timeout=30, cwd=root)
        self.assertEqual(result.returncode, 3, result.stdout+result.stderr)
        self.assertIn('Wrong day: stale automation', result.stdout)
        self.assertNotIn('REACHED_COM', result.stdout+result.stderr)


if __name__ == '__main__':
    unittest.main()
