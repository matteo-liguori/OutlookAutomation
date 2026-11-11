"""Mutation tests use synthetic objects and never instantiate Outlook."""
import json
import platform
import subprocess
import unittest
from unittest.mock import Mock

from monitor.adapters.macos.native import SCRIPT
from monitor.adapters.macos.test_actions import ACTIONS, execute_action, audit_result
from test_mac_native import FIXTURE


class AuthorizationTests(unittest.TestCase):
    def test_explicit_authorization_and_unique_token_required(self):
        runner = Mock()
        for request in ({}, {'token':'0123456789ab'}, {'token':'not-a-token','authorized':True}):
            with self.assertRaises(ValueError):
                execute_action(request, runner)
        runner.assert_not_called()

    def test_generic_audit_receipts_omit_mail_bodies(self):
        result={'status':'ok','items':[{'id':'1','body':'Synthetic test body'}]}
        receipt=audit_result(result)
        self.assertNotIn('body',receipt['items'][0])
        self.assertEqual(receipt['items'][0]['body_length'],19)
        self.assertEqual(result['items'][0]['body'],'Synthetic test body')


@unittest.skipUnless(platform.system() == 'Darwin', 'macOS JavaScript for Automation required')
class SyntheticMutationTests(unittest.TestCase):
    def js(self, scenario):
        source = SCRIPT.read_text()+'\n'+ACTIONS.read_text()+'\nfunction run(){\n'+FIXTURE+r'''
request.token='0123456789ab';request.authorized=true;request.folder_id='131';request.calendar_id='3';request.object_id='2';
request.draft_folder_id='127';request.body='Synthetic self test';
var deleted=0,sent=0;
app.delete=function(){deleted++;};app.send=function(){sent++;};
inbox.container=function(){return {id:function(){return 101;}};};
var draftFolder={container:inbox.container,outgoingMessages:{whose:function(){return function(){return [message];};}}};
app.mailFolders={byId:function(){return draftFolder;}};
message.bccRecipients=function(){return [];};
'''+'\n'+scenario+'\n}'
        p=subprocess.run(['/usr/bin/osascript','-l','JavaScript','-'],input=source,capture_output=True,text=True,timeout=20)
        self.assertEqual(p.returncode,0,p.stderr)
        return json.loads(p.stdout)

    def test_unmarked_calendar_event_cannot_be_deleted(self):
        r=self.js("request.action='calendar-delete';return JSON.stringify({result:nativeTestAction(request,app),deleted:deleted});")
        self.assertEqual(r['result']['code'],'not-the-created-test-event')
        self.assertEqual(r['deleted'],0)

    def test_only_marked_event_in_exact_calendar_is_removed(self):
        r=self.js("request.action='calendar-delete';event.subject=function(){return testSubject(request,'calendar');};calendar.calendarEvents.whose=function(){return function(){return deleted?[]:[event];};};return JSON.stringify({result:nativeTestAction(request,app),deleted:deleted});")
        self.assertTrue(r['result']['removed'])
        self.assertEqual(r['deleted'],1)

    def test_wrong_sender_or_body_blocks_send(self):
        for change in ("message.sender=function(){return {address:'other@example.invalid'};};", "message.plainTextContent=function(){return 'changed wording';};"):
            r=self.js("request.action='mail-send';message.subject=function(){return testSubject(request,'self-mail');};"+change+"return JSON.stringify({result:nativeTestAction(request,app),sent:sent});")
            self.assertEqual(r['result']['code'],'self-mail-envelope-or-body-mismatch')
            self.assertEqual(r['sent'],0)

    def test_only_exact_self_addressed_message_can_be_submitted(self):
        r=self.js("request.action='mail-send';message.subject=function(){return testSubject(request,'self-mail');};message.sender=function(){return {address:target};};message.plainTextContent=function(){return request.body;};return JSON.stringify({result:nativeTestAction(request,app),sent:sent});")
        self.assertTrue(r['result']['submitted'])
        self.assertFalse(r['result']['delivery_verified'])
        self.assertEqual(r['sent'],1)

    def test_foreign_folder_prevents_draft_access(self):
        r=self.js("request.action='mail-send';draftFolder.container=function(){return {id:function(){return 999;}};};return JSON.stringify({result:nativeTestAction(request,app),sent:sent});")
        self.assertEqual(r['result']['code'],'test-folder-not-in-gmail-root')
        self.assertEqual(r['sent'],0)
