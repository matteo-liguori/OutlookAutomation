"""Approval, duplicate prevention and private attachment boundaries."""
from pathlib import Path
import hashlib
import json
import platform
import subprocess
import tempfile
import unittest
from unittest.mock import Mock

from monitor.core import Harness
from monitor.profile import PROFILE
from monitor.approvals import put
from monitor.model import now
from outlook_platforms.macos.operations import send_approved,prepared_message,move_approved
from outlook_platforms.macos.attachments import download_attachment
from outlook_platforms.macos.operations import SCRIPT,OPERATIONS
from test_mac_native import FIXTURE


class NativeOperationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve();self.h=Harness(self.root/'monitor.db',create=True)
        self.email=next(iter(PROFILE['mail'].values()))
        self.eid=self.h.put_evidence({'source':'user','body':'Synthetic authorization'})['id']
        self.message=dict(sender=self.email,to=[self.email],cc=[],bcc=[],subject='Synthetic test',body='Synthetic body',attachments=[])

    def tearDown(self):self.h.close();self.tmp.cleanup()

    def grant(self,aid='a',scope=None):
        return put(self.h,dict(id=aid,kind='one-off',status='approved',granted_at=now(),evidence_ids=[self.eid],message=self.message,scope=scope or {}))

    def test_submission_is_durable_and_never_repeated(self):
        self.grant();run=Mock(side_effect=[dict(status='ok',id='33',Status='draft-saved'),dict(status='ok',modification='2026-11-09T00:00:00Z'),dict(status='ok',Status='submitted')])
        first=send_approved(self.h,'a','131','127',runner=run,attachment_root=self.root)
        second=send_approved(self.h,'a','131','127',runner=run,attachment_root=self.root)
        self.assertEqual(run.call_count,3);self.assertTrue(second['duplicate_prevented'])
        self.assertEqual(first['EntryId'],second['EntryId'])

    def test_uncertain_submission_does_not_retry(self):
        self.grant();run=Mock(side_effect=[dict(status='ok',id='33'),dict(status='ok',modification='x'),dict(status='blocked',code='timeout',mutation_may_have_occurred=True)])
        send_approved(self.h,'a','131','127',runner=run,attachment_root=self.root)
        with self.assertRaisesRegex(ValueError,'reconciliation'):send_approved(self.h,'a','131','127',runner=run,attachment_root=self.root)
        self.assertEqual(run.call_count,3)

    def test_save_only_scope_cannot_be_used_to_send(self):
        self.grant(scope={'actions':['save-draft']});run=Mock(return_value=dict(status='ok',id='33'))
        send_approved(self.h,'a','131','127',save_only=True,runner=run,attachment_root=self.root)
        with self.assertRaisesRegex(ValueError,'does not cover'):send_approved(self.h,'a','131','127',runner=run,attachment_root=self.root)
        self.assertEqual(run.call_count,1)

    def test_changed_approved_attachment_is_blocked(self):
        path=self.root/'fixture.txt';path.write_text('original')
        self.message['attachments']=[dict(path=str(path),sha256=hashlib.sha256(path.read_bytes()).hexdigest())]
        self.grant();path.write_text('changed');run=Mock()
        with self.assertRaisesRegex(ValueError,'content changed'):send_approved(self.h,'a','131','127',runner=run,attachment_root=self.root)
        run.assert_not_called()

    def test_move_requires_exact_scope_and_is_not_repeated(self):
        scope=dict(operation='move-message',mailbox=self.email,source_folder_id='131',entry_id='33',destination_folder_id='111',internet_message_id='<fixture@example.invalid>')
        put(self.h,dict(id='m',kind='one-off',status='approved',granted_at=now(),evidence_ids=[self.eid],scope=scope))
        run=Mock(return_value=dict(status='ok',id='34',read_state_preserved=True))
        move_approved(self.h,'m','131',runner=run);value=move_approved(self.h,'m','131',runner=run)
        self.assertTrue(value['duplicate_prevented']);self.assertEqual(run.call_count,1)

    def test_attachment_export_sanitizes_name_and_verifies_bytes(self):
        def run(request):Path(request['destination']).write_bytes(b'fixture');return dict(status='ok',saved=True,read_state_preserved=True)
        value=download_attachment(self.email,'131','131','33',dict(index=1,name='../../fixture.txt',size=12),private_root=self.root,runner=run)
        self.assertTrue(Path(value['path']).is_relative_to(self.root));self.assertEqual(Path(value['path']).name,'fixture.txt')
        self.assertEqual(value['sha256'],hashlib.sha256(b'fixture').hexdigest())
        with self.assertRaisesRegex(ValueError,'Large attachment'):download_attachment(self.email,'131','131','33',dict(index=1,name='big',size=11*1024*1024),private_root=self.root,runner=run)

    def test_encoded_attachment_size_does_not_replace_byte_hash(self):
        from outlook_platforms.macos.operations import verify_attachment_bytes
        path=self.root/'fixture.txt';path.write_bytes(b'fixture')
        message=prepared_message(dict(self.message,attachments=[dict(path=str(path),sha256=hashlib.sha256(b'fixture').hexdigest())]),self.root)
        def run(request):Path(request['destination']).write_bytes(b'changed');return dict(status='ok',read_state_preserved=True)
        with self.assertRaisesRegex(ValueError,'bytes changed'):verify_attachment_bytes(message,'131','127','33',self.root,run)
        self.assertEqual(list((self.root/'downloads').glob('*/*')),[])


@unittest.skipUnless(platform.system()=='Darwin','macOS JXA required')
class NativeOperationJavaScriptTests(unittest.TestCase):
    def js(self,scenario):
        source=SCRIPT.read_text()+'\n'+OPERATIONS.read_text()+'\nfunction run(){\n'+FIXTURE+r'''
request={action:'draft-send',authorized:true,mailbox:target,folder_id:'131',draft_folder_id:'127',object_id:'1',message:{sender:target,to:[target],cc:[],bcc:[],subject:'Known fixture',body:'approved body',attachments:[]}};
var root={id:function(){return 101;}};
var drafts={id:function(){return 127;},name:function(){return 'Drafts';},container:function(){return root;},outgoingMessages:{whose:function(){return function(){return [message];};}}};
app.mailFolders={byId:function(){return drafts;}};
message.folder=function(){return drafts;};message.sender=function(){return {address:target};};
message.plainTextContent=function(){return 'approved body';};message.bccRecipients=function(){return [];};message.attachments=function(){return [];};
var sent=0;app.send=function(){sent++;};
'''+'\n'+scenario+'\n}'
        p=subprocess.run(['/usr/bin/osascript','-l','JavaScript','-'],input=source,capture_output=True,text=True,timeout=20)
        self.assertEqual(p.returncode,0,p.stderr);return json.loads(p.stdout)

    def test_only_exact_envelope_is_submitted(self):
        result=self.js('return JSON.stringify({result:nativeOperation(request,app),sent:sent});')
        self.assertEqual(result['sent'],1);self.assertEqual(result['result']['Status'],'submitted')
        for change in ("message.plainTextContent=function(){return 'edited';};", "message.sender=function(){return {address:'other@example.invalid'};};", "request.message.to=['different@example.invalid'];"):
            result=self.js(change+'return JSON.stringify({result:nativeOperation(request,app),sent:sent});')
            self.assertEqual(result['sent'],0);self.assertEqual(result['result']['status'],'blocked')

    def test_draft_parent_mismatch_and_missing_authority_block(self):
        for change in ("message.folder=function(){return {id:function(){return 999;}};};",'request.authorized=false;'):
            result=self.js(change+'return JSON.stringify({result:nativeOperation(request,app),sent:sent});')
            self.assertEqual(result['sent'],0);self.assertEqual(result['result']['status'],'blocked')
