"""Core mail parity: exact draft operations, byte transfers and sync evidence."""
import base64
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

from monitor.core import Harness
from monitor.approvals import put
from monitor.model import now
from monitor.adapters.thunderbird.commands import execute_json
from monitor.adapters.thunderbird.mail import uploaded_attachments,download_attachment,freshness,attachment_specs,CHUNK

MAILBOX='fixture@example.invalid'
PROFILE=dict(mail={'mail:work':MAILBOX},calendars=[],platform=dict(os='macos',outlook_backend='thunderbird',bindings={
 'mail:work':dict(account_id='account1',selected_inbox_id='account1://INBOX',draft_folder_id='account1://Drafts',sent_folder_id='account1://Sent',trash_folder_id='account1://Trash',address_book_ids=['book1'])}))
BASE=dict(account_id='account1',mailbox=MAILBOX)


class ThunderbirdMailTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name).resolve()
        self.h=Harness(self.root/'monitor.db',create=True);self.addCleanup(self.h.close)
        self.eid=self.h.put_evidence(dict(source='user',body='Exact synthetic mail helper tests'))['id']
        self.p=copy.deepcopy(PROFILE)

    def approve(self,aid,message,scope=None):
        put(self.h,dict(id=aid,kind='one-off',granted_at=now(),message=message,scope=scope or {},evidence_ids=[self.eid]))

    def message(self,**kw):
        return dict(sender=MAILBOX,to=[MAILBOX],cc=[],bcc=[],subject='Synthetic fixture',body='Exact body',attachments=[],**kw)

    def test_binary_transfer_crosses_multiple_frames_and_hashes_real_bytes(self):
        content=bytes(range(256))*(CHUNK//256+5);source=self.root/'café.bin';source.write_bytes(content)
        digest=hashlib.sha256(content).hexdigest();message=self.message();message['attachments']=[dict(path=str(source),sha256=digest)]
        uploaded=bytearray();offsets=[]
        def upload(request):
            offsets.append(request['offset']);uploaded.extend(base64.b64decode(request['data']));return dict(result=dict(status='ok',received=len(uploaded)))
        from monitor.adapters.thunderbird.commands import call
        with patch('monitor.adapters.thunderbird.mail.data_root',return_value=self.root):
            specs=uploaded_attachments(message,BASE,call,upload)
            self.assertEqual(bytes(uploaded),content);self.assertEqual(offsets,[0,CHUNK]);self.assertEqual(specs[0]['sha256'],digest)
            def retrieve(request):
                if request['action']=='attachment-open':return dict(result=dict(status='ok',size=len(content),sha256=digest,download_id='one'))
                offset=request['offset'];part=content[offset:offset+CHUNK]
                return dict(result=dict(status='ok',offset=offset,data=base64.b64encode(part).decode(),eof=offset+len(part)==len(content)))
            asset=download_attachment(BASE,dict(message_id=12),dict(name='../../café.bin',part_name='1.2'),call,retrieve)
            self.assertTrue(Path(asset['path']).is_relative_to(self.root));self.assertEqual(Path(asset['path']).read_bytes(),content)
            self.assertEqual(asset['sha256'],digest);self.assertFalse(list(self.root.rglob('*.partial')))

    def test_corrupt_download_leaves_no_partial_or_final_asset(self):
        from monitor.adapters.thunderbird.commands import call
        replies=[dict(result=dict(status='ok',size=3,sha256='0'*64,download_id='one')),dict(result=dict(status='ok',offset=0,data=base64.b64encode(b'bad').decode(),eof=True))]
        target=self.root/'file.bin'
        with patch('monitor.adapters.thunderbird.mail.data_root',return_value=self.root):
            with self.assertRaisesRegex(ValueError,'hash/size'):
                download_attachment(BASE,dict(message_id=12),dict(name='file.bin',part_name='1.2'),call,Mock(side_effect=replies),target=target)
        self.assertFalse(target.exists());self.assertFalse(list(self.root.glob('*.partial')))

    def test_attachment_changed_bytes_are_rejected(self):
        source=self.root/'file.bin';source.write_bytes(b'approved');message=self.message();message['attachments']=[dict(path=str(source),sha256=hashlib.sha256(b'approved').hexdigest())]
        with patch('monitor.adapters.thunderbird.mail.data_root',return_value=self.root):
            attachment_specs(message);source.write_bytes(b'changed')
            with self.assertRaises(ValueError):attachment_specs(message)

    def test_attachment_paths_outside_private_data_are_rejected(self):
        with tempfile.TemporaryDirectory() as outside:
            source=Path(outside)/'external';source.write_bytes(b'approved')
            message=self.message();message['attachments']=[dict(path=str(source),sha256=hashlib.sha256(b'approved').hexdigest())]
            with patch('monitor.adapters.thunderbird.mail.data_root',return_value=self.root):
                with self.assertRaisesRegex(ValueError,'escaped'):attachment_specs(message)

    def test_attachment_symlink_escape_is_rejected(self):
        source=self.root/'file.bin';message=self.message();message['attachments']=[dict(path=str(source),sha256=hashlib.sha256(b'approved').hexdigest())]
        with patch('monitor.adapters.thunderbird.mail.data_root',return_value=self.root):
            with tempfile.TemporaryDirectory() as outside:
                path=Path(outside)/'external';path.write_bytes(b'approved')
                try:source.symlink_to(path)
                except OSError as error:
                    if getattr(error,'winerror',None)==1314:self.skipTest('Windows symlink privilege unavailable')
                    raise
                with self.assertRaisesRegex(ValueError,'escaped'):attachment_specs(message)

    def test_draft_save_is_cached_and_exposes_stable_locator_and_raw_hash(self):
        message=self.message();self.approve('draft',message,dict(actions=['save-draft']))
        header=dict(id=12,internet_message_id='draft@fixture.invalid',subject=message['subject'],date='2026-11-09T12:00:00Z',author=MAILBOX,read=True,folder=dict(id='account1://Drafts',accountId='account1',path='/Drafts'))
        runner=Mock(return_value=dict(result=dict(status='ok',draft=dict(message=header,folder_id=header['folder']['id'],internet_message_id=header['internet_message_id'],raw_sha256='a'*64))))
        payload=dict(approval_id='draft',**{k:message[k] for k in ('to','cc','bcc','subject','body','attachments')})
        first=execute_json('draft-save',payload,MAILBOX,self.h,'mail:work',profile=self.p,runner=runner)
        second=execute_json('draft-save',payload,MAILBOX,self.h,'mail:work',profile=self.p,runner=runner)
        self.assertTrue(first['data']['EntryId'].startswith('tb:'));self.assertEqual(first['data']['ContentHash'],'a'*64)
        self.assertTrue(second['data']['duplicate_prevented']);self.assertEqual(runner.call_count,1)
        self.assertEqual(runner.call_args.args[0]['action'],'draft-message')

    def test_draft_update_requires_old_raw_hash_and_exact_bound_folder(self):
        message=self.message();scope=dict(actions=['update-draft'],draft_id='12',draft_folder_id='account1://Drafts')
        self.approve('missing-version',message,scope)
        runner=Mock();payload=dict(approval_id='missing-version',**{k:message[k] for k in ('to','cc','bcc','subject','body','attachments')})
        with self.assertRaisesRegex(ValueError,'expected_raw_sha256'):execute_json('draft-update',payload,MAILBOX,self.h,'mail:work',profile=self.p,runner=runner)
        runner.assert_not_called()
        scope['expected_raw_sha256']='a'*64;self.approve('versioned',message,scope)
        runner.return_value=dict(result=dict(status='ok'))
        execute_json('draft-update',dict(payload,approval_id='versioned'),MAILBOX,self.h,'mail:work',profile=self.p,runner=runner)
        self.assertEqual(runner.call_args.args[0]['expected_raw_sha256'],'a'*64);self.assertEqual(runner.call_args.args[0]['message_id'],12)

    def test_freshness_requires_callback_and_two_recent_stable_samples(self):
        from monitor.adapters.thunderbird.commands import call
        import datetime
        message=dict(id=1,internet_message_id='one',date=datetime.datetime.now(datetime.timezone.utc).isoformat())
        def runner(r):
            if r['action']=='client-refresh':return dict(result=dict(status='ok',success=True,provider_callback_verified=True))
            return dict(result=dict(status='ok',complete=True,messages=[message]))
        result=freshness(BASE,self.p['platform']['bindings']['mail:work'],dict(interval_seconds=0),call,runner)
        self.assertTrue(result['Current']);self.assertEqual(result['StableSamples'],2)
        false=Mock(return_value=dict(result=dict(status='ok',success=False,provider_callback_verified=False)))
        self.assertFalse(freshness(BASE,self.p['platform']['bindings']['mail:work'],{},call,false)['Current']);self.assertEqual(false.call_count,1)

    def test_send_reconciliation_never_resubmits_or_needs_original_attachment_file(self):
        message=self.message();path=self.root/'asset.bin';path.write_bytes(b'bytes');message['attachments']=[dict(path=str(path),sha256=hashlib.sha256(b'bytes').hexdigest())];self.approve('send',message)
        payload=dict(approval_id='send',**{k:message[k] for k in ('to','cc','bcc','subject','body')},attachments=[str(path)])
        def runner(r):
            if r['action']=='attachment-upload':return dict(result=dict(status='ok',received=len(b'bytes')))
            return dict(result=dict(status='blocked',code='client-operation-timeout',mutation_may_have_occurred=True))
        with patch('monitor.adapters.thunderbird.mail.data_root',return_value=self.root):
            self.assertEqual(execute_json('send',payload,MAILBOX,self.h,'mail:work',profile=self.p,runner=runner)['status'],'blocked')
        path.unlink();reader=Mock(return_value=dict(result=dict(status='ok',sent_verified=True,delivery_verified=False)))
        result=execute_json('send-reconcile',dict(approval_id='send'),MAILBOX,self.h,'mail:work',profile=self.p,runner=reader)
        self.assertTrue(result['data']['reconciled']);self.assertEqual(reader.call_args.args[0]['action'],'send-verify');self.assertTrue(reader.call_args.args[0]['reconcile'])

    def test_uncertain_move_refreshes_both_folders_and_never_moves_again(self):
        scope=dict(operation='trash-message',mailbox=MAILBOX,source_folder_id='account1://INBOX',destination_folder_id='account1://Trash',entry_id='12',internet_message_id='fixture@example.invalid')
        self.approve('cleanup',None,scope)
        def runner(request):
            action=request['action']
            if action=='trash-message':return dict(result=dict(status='blocked',code='move-not-verified',mutation_may_have_occurred=True))
            if action=='client-refresh':
                self.assertEqual(request['folder_ids'],['account1://INBOX','account1://Trash'])
                return dict(result=dict(status='ok',success=True))
            self.assertEqual(action,'reconcile-trash')
            return dict(result=dict(status='ok',reconciled=True,move_command_reissued=False))
        spy=Mock(side_effect=runner)
        result=execute_json('trash',dict(approval_id='cleanup'),MAILBOX,self.h,'mail:work',profile=self.p,runner=spy)
        self.assertTrue(result['data']['reconciled']);self.assertEqual([c.args[0]['action'] for c in spy.call_args_list],['trash-message','client-refresh','reconcile-trash'])
        repeated=execute_json('trash',dict(approval_id='cleanup'),MAILBOX,self.h,'mail:work',profile=self.p,runner=spy)
        self.assertTrue(repeated['data']['duplicate_prevented']);self.assertEqual(spy.call_count,3)

    def test_previews_keep_distinct_same_rfc_physical_copies(self):
        header=dict(id=12,internet_message_id='same@fixture.invalid',subject='Synthetic',date='2026-11-09T12:00:00Z',author=MAILBOX,read=False,folder=dict(id='account1://Sent',accountId='account1',path='/Sent'))
        def runner(request):
            if request['action']=='query':return dict(result=dict(status='ok',complete=True,messages=[header,dict(header,id=13)]))
            self.assertEqual(request['action'],'read-extended');self.assertIn(request['message_id'],[12,13]);self.assertEqual(request['internet_message_id'],'same@fixture.invalid')
            return dict(result=dict(status='ok',message=header,full=dict(parts=[dict(contentType='text/plain',body='Synthetic')]),read_state_preserved=True))
        spy=Mock(side_effect=runner)
        result=execute_json('recent',dict(folder='sent',include_preview=True),MAILBOX,self.h,'mail:work',profile=self.p,runner=spy)
        self.assertEqual(result['status'],'ok');self.assertEqual({i['TransientId'] for i in result['data']},{12,13});self.assertTrue(all(i['BodyPreview']=='Synthetic' for i in result['data']));self.assertEqual(spy.call_count,3)
