"""Exercise the native adapter with synthetic objects; never connect to Outlook."""
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import Mock

from mac_outlook_native import SCRIPT, execute, request_for


FIXTURE = r'''
var target = 'pilot@example.invalid';
var request = {command:'mail', mailbox:target, start:'2026-11-08T00:00:00Z', end:'2026-11-09T00:00:00Z', limit:25, include_body:true};
var reads = {body:0, inbox:0, subject:0};
var account = {emailAddress:function(){return target;}};
function owned(){return account;}
var flag = false;
var message = {
 account:owned, id:function(){return 1;}, isRead:function(){return flag;},
 subject:function(){reads.subject++;return 'Known fixture';},
 timeReceived:function(){return new Date('2026-11-08T12:00:00Z');},
 plainTextContent:function(){reads.body++;return 'Synthetic body "quoted"\nsecond line';}
};
message.sender=function(){return {name:'Fixture sender',address:'sender@example.invalid'};};
message.toRecipients=function(){return [{emailAddress:function(){return {address:target};}}];};
message.ccRecipients=function(){return [];};message.attachments=function(){return [{name:function(){return 'fixture.txt';}}];};
var matches = [message], lastFilter = null;
var inbox = {account:owned, messages:{whose:function(filter){lastFilter=filter;return function(){return matches;};}}};
inbox.id=function(){return 131;};inbox.name=function(){return 'Inbox';};message.folder=function(){return inbox;};
account.inbox=function(){reads.inbox++;return inbox;};
var app = {imapAccounts:function(){return [account];},exchangeAccounts:function(){return [];},popAccounts:function(){return [];}};
app.mainWindows=[{name:function(){return 'Inbox • '+target;}}];app.selectedFolder=function(){return inbox;};
var event = {account:owned,id:function(){return 2;},subject:function(){return 'Fixture appointment';},
 startTime:function(){return new Date('2026-11-08T12:00:00Z');},endTime:function(){return new Date('2026-11-08T13:00:00Z');},
 allDayFlag:function(){return false;},isRecurring:function(){return false;},plainTextContent:function(){return 'Fixture event';}};
event.location=function(){return 'Fixture room';};
var calendar = {account:owned,id:function(){return 3;},calendarEvents:{whose:function(filter){lastFilter=filter;return function(){return [event];};}}};
calendar.name=function(){return target;};event.calendar=function(){return calendar;};
app.calendars=function(){return [calendar];};app.calendars.byId=function(){return calendar;};
account.calendars=function(){return [calendar];};
'''


class NativeRequestTests(unittest.TestCase):
    def test_explicit_identity_and_bounded_timezone_required(self):
        for mailbox in ('', 'x', 'someone @example.invalid'):
            with self.assertRaises(ValueError):
                request_for('check', mailbox)
        for start, end in ((None, None), ('2026-11-08', '2026-11-09'),
                           ('2026-11-09T00:00:00Z', '2026-11-08T00:00:00Z'),
                           ('2026-11-01T00:00:00Z', '2026-11-09T00:00:00Z')):
            with self.assertRaises(ValueError):
                request_for('mail', 'pilot@example.invalid', start, end)
        for limit in (0, 101, True, 2.5):
            with self.assertRaises(ValueError):
                request_for('mail', 'pilot@example.invalid', '2026-11-08T00:00:00Z', '2026-11-09T00:00:00Z', limit)

    def test_request_data_is_argv_not_executable_code(self):
        subject = 'Fixture "quoted" `literal` $(literal)'
        request = request_for('mail', 'pilot@example.invalid', '2026-11-08T00:00:00Z', '2026-11-09T00:00:00Z', subject=subject)
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, '{"status":"ok"}', ''))
        self.assertEqual(execute(request, runner=runner, system='Darwin')['status'], 'ok')
        arguments = runner.call_args.args[0]
        self.assertEqual(json.loads(arguments[-1])['subject'], subject)
        self.assertNotIn('shell', runner.call_args.kwargs)

    def test_platform_transport_and_bad_responses_remain_explicit(self):
        runner = Mock()
        self.assertEqual(execute({}, runner=runner, system='Windows')['code'], 'requires-macos')
        runner.assert_not_called()
        for output in ('not JSON', '[]', '{}'):
            runner = Mock(return_value=subprocess.CompletedProcess([], 0, output, 'private error text'))
            self.assertEqual(execute({}, runner=runner, system='Darwin')['code'], 'invalid-native-response')
        runner = Mock(return_value=subprocess.CompletedProcess([], 1, '', 'private error text'))
        self.assertNotIn('private error text', json.dumps(execute({}, runner=runner, system='Darwin')))
        runner = Mock(side_effect=subprocess.TimeoutExpired([], 60))
        self.assertEqual(execute({}, runner=runner, system='Darwin')['code'], 'native-command-timeout')


class NativeJavaScriptTests(unittest.TestCase):
    def js(self, scenario):
        source = SCRIPT.read_text() + '\nfunction run() {\n' + FIXTURE + '\n' + scenario + '\n}'
        # The overridden run uses synthetic JS objects and never calls Application.
        p = subprocess.run(['/usr/bin/osascript', '-l', 'JavaScript', '-'], input=source,
                           capture_output=True, text=True, timeout=20)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    def test_check_reads_identity_only(self):
        r = self.js("request.command='check';return JSON.stringify({result:nativeCollect(request,app),reads:reads});")
        self.assertEqual(r['result']['status'], 'ok')
        self.assertFalse(r['result']['content_read'])
        self.assertEqual(r['reads'], dict(body=0, inbox=0, subject=0))

    def test_missing_duplicate_and_other_accounts_block_before_content(self):
        setups = [
            ("app.imapAccounts=function(){return [];};", 'target-account-not-scriptable'),
            ("app.popAccounts=function(){return [account];};", 'ambiguous-target-account'),
            ("app.popAccounts=function(){return [{emailAddress:function(){return 'other@example.invalid';}}];};", 'other-account-present'),
        ]
        for setup, code in setups:
            r = self.js(setup + 'return JSON.stringify({result:nativeCollect(request,app),reads:reads});')
            self.assertEqual(r['result']['code'], code)
            self.assertEqual(r['reads'], dict(body=0, inbox=0, subject=0))
            self.assertNotIn('other@example.invalid', json.dumps(r))

    def test_read_only_known_message_and_native_date_filter(self):
        r = self.js("request.subject='Known fixture';return JSON.stringify({result:nativeCollect(request,app),filter:lastFilter});")
        self.assertEqual(r['result']['status'], 'ok')
        self.assertFalse(r['result']['items'][0]['is_read'])
        self.assertTrue(r['result']['read_state_preserved'])
        self.assertIn('Synthetic body', r['result']['items'][0]['plain_text'])
        self.assertEqual(r['filter']['_and'][1], {'subject':'Known fixture'})
        self.assertEqual(r['filter']['_and'][0]['_and'][0]['timeReceived'], {'_greaterThanEquals':'2026-11-08T00:00:00.000Z'})
        self.assertFalse(r['result']['writes_performed'])

    def test_empty_results_and_repeated_reads(self):
        r = self.js('var first=nativeCollect(request,app);var second=nativeCollect(request,app);matches=[];return JSON.stringify([first,second,nativeCollect(request,app)]);')
        self.assertEqual(r[0], r[1])
        self.assertEqual(r[2]['items'], [])
        self.assertEqual(r[2]['matched_count'], 0)
        self.assertTrue(r[2]['read_state_preserved'])

    def test_foreign_owner_and_out_of_window_stop_before_body(self):
        for setup, code in [
            ("message.account=function(){return {emailAddress:function(){return 'other@example.invalid';}};};", 'owner-not-target'),
            ("message.timeReceived=function(){return new Date('2026-11-07T12:00:00Z');};", 'native-filter-outside-window'),
        ]:
            r = self.js(setup+'return JSON.stringify({result:nativeCollect(request,app),reads:reads});')
            self.assertEqual(r['result']['code'], code)
            self.assertEqual(r['reads']['body'], 0)

    def test_read_state_change_is_failure_without_rewriting_state(self):
        r = self.js("message.plainTextContent=function(){flag=true;return 'fixture';};return JSON.stringify({result:nativeCollect(request,app),flag:flag});")
        self.assertEqual(r['result']['code'], 'read-state-changed')
        self.assertTrue(r['flag'])
        self.assertNotIn('items', r['result'])

    def test_limits_are_reported_as_partial_coverage(self):
        r = self.js('request.limit=1;matches=[message,message];return JSON.stringify(nativeCollect(request,app));')
        self.assertEqual(r['matched_count'], 2)
        self.assertEqual(len(r['items']), 1)
        self.assertEqual(r['coverage'], 'partial-limit')

    def test_calendar_is_account_scoped_and_recurrence_is_explicit(self):
        r = self.js("request.command='calendar';return JSON.stringify({result:nativeCollect(request,app),filter:lastFilter});")
        self.assertEqual(r['result']['status'], 'ok')
        self.assertEqual(r['result']['items'][0]['calendar_id'], '3')
        self.assertIn('not established', r['result']['recurrence_coverage'])
        self.assertIn('startTime', r['filter']['_and'][0])
        self.assertIn('endTime', r['filter']['_and'][1])
        r = self.js("request.command='calendar';account.calendars=function(){return [];};return JSON.stringify(nativeCollect(request,app));")
        self.assertEqual(r['code'], 'no-calendars-exposed-for-target-account')

    def test_native_errors_do_not_leak_exception_descriptions(self):
        r = self.js("account.inbox=function(){throw new Error('private account or credential text');};return JSON.stringify(nativeCollect(request,app));")
        self.assertEqual(r['code'], 'native-command-failed')
        self.assertNotIn('private account', json.dumps(r))

    def test_selected_google_inbox_without_account_object(self):
        r = self.js("app.imapAccounts=function(){return [];};request.binding='selected-inbox';request.folder_id='131';return JSON.stringify(nativeCollect(request,app));")
        self.assertEqual(r['status'], 'ok')
        self.assertEqual(r['account_type'], 'ui-verified-selected-inbox')
        self.assertEqual(r['binding']['folder_id'], '131')
        self.assertTrue(r['read_state_preserved'])

    def test_selected_binding_rejects_wrong_window_folder_and_foreign_account(self):
        for setup, code in [
            ("app.mainWindows[0].name=function(){return 'Inbox • other@example.invalid';};", 'window-identity-changed'),
            ("inbox.id=function(){return 999;};", 'selected-inbox-changed'),
            ("message.folder=function(){return {id:function(){return 999;}};};", 'message-folder-mismatch'),
            ("app.popAccounts=function(){return [{emailAddress:function(){return 'other@example.invalid';}}];};", 'other-account-present'),
        ]:
            r = self.js("request.binding='selected-inbox';request.folder_id='131';"+setup+'return JSON.stringify({result:nativeCollect(request,app),reads:reads});')
            self.assertEqual(r['result']['code'], code)
            self.assertEqual(r['reads']['body'], 0)

    def test_named_google_calendar_and_inventory(self):
        r = self.js("request.command='calendar';request.binding='named-calendar';request.calendar_id='3';app.imapAccounts=function(){return [];};return JSON.stringify(nativeCollect(request,app));")
        self.assertEqual(r['status'], 'ok')
        self.assertEqual(r['items'][0]['calendar_id'], '3')
        r = self.js("request.command='calendars';return JSON.stringify(nativeCollect(request,app));")
        self.assertTrue(r['calendars'][0]['matches_mailbox_name'])
        self.assertFalse(r['content_read'])

    def test_named_calendar_mismatch_and_foreign_owner_stop_reads(self):
        for setup, code in [
            ("calendar.name=function(){return 'Other';};", 'calendar-identity-mismatch'),
            ("calendar.account=function(){return {emailAddress:function(){return 'other@example.invalid';}};};", 'other-calendar-account-present'),
            ("event.calendar=function(){return {id:function(){return 999;}};};", 'event-calendar-mismatch'),
        ]:
            r = self.js("request.command='calendar';request.binding='named-calendar';request.calendar_id='3';"+setup+'return JSON.stringify(nativeCollect(request,app));')
            self.assertEqual(r['code'], code)
            self.assertNotIn('items', r)
