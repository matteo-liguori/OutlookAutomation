"""Shared command names retain Windows field meanings across platform adapters."""
import contextlib
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from outlook_platforms.macos.commands import main, normalize
from outlook_platforms.windows.commands import command_line


MAILBOX = 'pilot@example.invalid'
MAIL = dict(status='ok', account_type='fixture', items=[dict(id='33', subject='Fixture subject',
    received_at='2026-11-08T23:23:01Z', is_read=False, plain_text='Synthetic\nmessage text',
    sender_email='sender@example.invalid', **{'from':'Fixture sender'}, to=[MAILBOX], cc=[], attachment_names=['fixture.txt'])],
    read_state_preserved=True, truncated=False, matched_count=1,
    window=dict(start='2026-11-08T00:00:00Z', end_exclusive='2026-11-09T00:00:00Z'))


class PlatformCommandTests(unittest.TestCase):
    def test_mail_field_names_and_read_meanings_match_windows_contract(self):
        rows = normalize('Read-OutlookMail', MAIL, MAILBOX)
        self.assertEqual(set(rows[0]), {'Store','Received','Unread','From','SenderEmail','Subject','Preview','EntryId'})
        self.assertTrue(rows[0]['Unread'])
        self.assertEqual(rows[0]['Preview'], 'Synthetic message text')
        message = normalize('Get-OutlookMessage', MAIL, MAILBOX)
        self.assertEqual(message['EntryId'], rows[0]['EntryId'])
        self.assertEqual(message['Received'], rows[0]['Received'])
        self.assertEqual(message['Body'], MAIL['items'][0]['plain_text'])
        self.assertTrue(message['UnreadUnchanged'])
        self.assertEqual(message['AttachmentNames'], ['fixture.txt'])

    def test_windows_style_flags_reach_mac_native_request(self):
        args = ['-Store',MAILBOX,'-Start','2026-11-08T00:00:00Z','-End','2026-11-09T00:00:00Z',
                '-Limit','10','-UnreadOnly','--selected-inbox-id','131','-Output','Json']
        with patch('outlook_platforms.macos.commands.execute', return_value=MAIL) as run, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main('Read-OutlookMail', args), 0)
        request = run.call_args.args[0]
        self.assertEqual(request['limit'], 10)
        self.assertEqual(request['binding'], 'selected-inbox')
        self.assertTrue(request['unread_only'])
        self.assertEqual(json.loads(output.getvalue())[0]['EntryId'], '33')

    def test_exact_message_and_account_contracts(self):
        args = ['-Store',MAILBOX,'-Start','2026-11-08T00:00:00Z','-End','2026-11-09T00:00:00Z','-EntryId','33','--selected-inbox-id','131']
        with patch('outlook_platforms.macos.commands.execute', return_value=MAIL) as run, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main('Get-OutlookMessage', args), 0)
        self.assertEqual(run.call_args.args[0]['entry_id'], '33')
        self.assertTrue(run.call_args.args[0]['include_body'])
        self.assertEqual(json.loads(output.getvalue())['EntryId'], '33')
        rows = normalize('Inspect-Outlook', dict(status='ok',account_type='fixture'), MAILBOX)
        self.assertEqual(set(rows[0]), {'DisplayName','SmtpAddress','AccountType'})
        self.assertEqual(rows[0]['SmtpAddress'], MAILBOX)

    def test_calendar_schema_discloses_recurrence_gap(self):
        result = dict(status='ok',truncated=False,matched_count=1,window=MAIL['window'],
            items=[dict(id='23',subject='Fixture event',start='2026-11-08T12:00:00Z',end='2026-11-08T13:00:00Z',location='Room',all_day=False)])
        value = normalize('Read-OutlookMonitorCalendar', result, MAILBOX)
        self.assertEqual(set(value['events'][0]), {'id','title','start','end','location','all_day'})
        self.assertFalse(value['complete'])
        self.assertTrue(value['bounded_query_complete'])
        self.assertIn('recurrence', value['errors'][0])

    def test_unsupported_actions_are_explicit_and_do_not_connect(self):
        with patch('outlook_platforms.macos.commands.execute') as run, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                main('Read-OutlookMail', ['-Store',MAILBOX,'-Action','Search'])
        run.assert_not_called()

    def test_windows_delegates_existing_helper_without_shell_interpolation(self):
        literal = 'subject $(literal) `literal`'
        argv = command_line('Read-OutlookMail', ['-Store',MAILBOX,'-Query',literal], 'powershell')
        self.assertEqual(Path(argv[5]).name, 'Read-OutlookMail.ps1')
        self.assertTrue(Path(argv[5]).is_file())
        self.assertEqual(argv[-1], literal)
        with self.assertRaises(ValueError):
            command_line('Send-OutlookStoreMail', [], 'powershell')
