"""Client routing, exact writes, uncertainty and bounded snapshots."""
import copy
import io
import json
from pathlib import Path
import tempfile
import socket
import subprocess
import sys
import os
import time
import unittest
from unittest.mock import Mock,patch

from monitor.core import Harness
from monitor.approvals import put
from monitor.model import now
from monitor.profile import platform_settings
from outlook_platforms.api import invoke,status
from outlook_platforms.thunderbird.commands import execute_json,validate_event
from outlook_platforms.thunderbird.native_host import read_frame,write_frame
from outlook_platforms.thunderbird.setup import prepare

PROFILE=dict(name='Fixture',mail={'mail:work':'fixture@example.invalid'},calendars=['calendar:work'],
    platform=dict(os='macos',outlook_backend='thunderbird',bindings={
        'mail:work':dict(account_id='account1',selected_inbox_id='account1://INBOX',sent_folder_id='account1://Sent Items',draft_folder_id='account1://Drafts',trash_folder_id='account1://Trash'),
        'calendar:work':dict(backend='native',calendar_id='23')}))
ICS='BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:fixture\r\nDTSTART;TZID=Europe/London:20270327T120000\r\nDTEND;TZID=Europe/London:20270327T121000\r\nRRULE:FREQ=DAILY;COUNT=3\r\nSUMMARY:Fixture\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n'


class ThunderbirdTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.h=Harness(Path(self.tmp.name)/'monitor.db',create=True);self.addCleanup(self.h.close)
        self.eid=self.h.put_evidence(dict(source='user',body='Explicit synthetic test authority'))['id']
        self.p=copy.deepcopy(PROFILE)

    def approve(self,aid,**kw):
        put(self.h,dict(id=aid,kind='one-off',granted_at=now(),evidence_ids=[self.eid],**kw))

    def test_mail_and_calendar_choose_separate_clients_without_fallback(self):
        with patch('platform.system',return_value='Darwin'):
            info=status(self.p,details=True)
            self.assertEqual(info['source_backends'],{'mail:work':'thunderbird','calendar:work':'native'})
            self.assertIn('move',info['operations']);self.assertNotIn('calendar-create',info['operations'])
            self.assertNotIn('download_attachments',info['payload_fields']['read'])
            self.assertNotIn('include_preview',info['payload_fields']['recent'])
            with patch('outlook_platforms.thunderbird.commands.execute_json',return_value={'data':[]}) as tb,patch('outlook_platforms.macos.commands.execute_json',return_value={'data':[]}) as native:
                invoke('read',dict(entry_id='1'),self.h,self.p);tb.assert_called_once();native.assert_not_called()
                invoke('calendar',dict(source='calendar:work'),self.h,self.p);native.assert_called_once()
        self.p['platform']['bindings']['calendar:work']=dict(backend='thunderbird',calendar_id='uuid')
        self.assertEqual(platform_settings(self.p)['outlook_backend'],'thunderbird')
        with patch('platform.system',return_value='Darwin'):
            self.assertIn('calendar-create',status(self.p)['operations'])
        with patch('platform.system',return_value='Windows'):
            self.assertFalse(status(self.p)['ready'])
            with self.assertRaises(ValueError):invoke('read',dict(entry_id='1'),self.h,self.p)

    def test_exact_move_is_durable_and_changed_scope_cannot_reuse_it(self):
        scope=dict(operation='move-message',mailbox='fixture@example.invalid',source_folder_id='account1://INBOX',
                   destination_folder_id='account1://Filed',entry_id='12',internet_message_id='fixture@example.invalid')
        self.approve('move',scope=scope)
        runner=Mock(return_value=dict(result=dict(status='ok',read_state_preserved=True)))
        args=('move',dict(approval_id='move',entry_id='12'),scope['mailbox'],self.h,'mail:work')
        first=execute_json(*args,profile=self.p,runner=runner)
        again=execute_json(*args,profile=self.p,runner=runner)
        self.assertEqual(first['status'],'ok');self.assertTrue(again['data']['duplicate_prevented']);self.assertEqual(runner.call_count,1)
        with self.assertRaises(ValueError):execute_json('move',dict(approval_id='move',entry_id='13'),scope['mailbox'],self.h,'mail:work',profile=self.p,runner=runner)
        self.p['platform']['bindings']['mail:work']['account_id']='account9'
        with self.assertRaisesRegex(ValueError,'changed'):execute_json(*args,profile=self.p,runner=runner)
        self.assertEqual(runner.call_count,1)

    def test_uncertain_send_is_never_reissued(self):
        message=dict(sender='fixture@example.invalid',to=['fixture@example.invalid'],cc=[],bcc=[],subject='Synthetic',body='Synthetic fixture',attachments=[])
        self.approve('send',message=message)
        payload=dict(message,approval_id='send');payload.pop('sender')
        runner=Mock(side_effect=OSError('lost response'))
        args=('send',payload,message['sender'],self.h,'mail:work')
        with self.assertRaises(OSError):execute_json(*args,profile=self.p,runner=runner)
        with self.assertRaisesRegex(ValueError,'reconciliation'):execute_json(*args,profile=self.p,runner=runner)
        self.assertEqual(runner.call_count,1)
        altered=dict(payload,body='Changed')
        with self.assertRaises(ValueError):execute_json('send',altered,message['sender'],self.h,'mail:work',profile=self.p,runner=runner)

    def test_uncertain_move_reconciles_without_a_second_mutation(self):
        self.approve('move',scope=dict(operation='move-message',mailbox='fixture@example.invalid',source_folder_id='account1://INBOX',
            destination_folder_id='account1://Filed',entry_id='12',internet_message_id='fixture@example.invalid'))
        runner=Mock(return_value=dict(result=dict(status='blocked',code='client-operation-timeout')))
        first=execute_json('move',dict(approval_id='move'),'fixture@example.invalid',self.h,'mail:work',profile=self.p,runner=runner)
        self.assertEqual(first['status'],'blocked')
        runner.return_value=dict(result=dict(status='ok',reconciled=True,move_command_reissued=False))
        result=execute_json('move-reconcile',dict(approval_id='move'),'fixture@example.invalid',self.h,'mail:work',profile=self.p,runner=runner)
        self.assertTrue(result['data']['reconciled']);self.assertEqual(runner.call_args.args[0]['action'],'reconcile-move')
        again=execute_json('move',dict(approval_id='move'),'fixture@example.invalid',self.h,'mail:work',profile=self.p,runner=runner)
        self.assertTrue(again['data']['duplicate_prevented']);self.assertEqual(runner.call_count,2)

    def test_trash_uses_only_approved_configured_recoverable_destination(self):
        scope=dict(operation='trash-message',mailbox='fixture@example.invalid',source_folder_id='account1://INBOX',
                   destination_folder_id='account1://Trash',entry_id='12',internet_message_id='fixture@example.invalid')
        self.approve('trash',scope=scope)
        runner=Mock(return_value=dict(result=dict(status='ok',recoverable=True)))
        args=('trash',dict(approval_id='trash'),'fixture@example.invalid',self.h,'mail:work')
        r=execute_json(*args,profile=self.p,runner=runner)
        self.assertTrue(r['data']['recoverable']);self.assertEqual(runner.call_args.args[0]['action'],'trash-message')
        self.assertNotIn('deletePermanently',runner.call_args.args[0])
        self.p['platform']['bindings']['mail:work']['trash_folder_id']='account1://Another'
        with self.assertRaisesRegex(ValueError,'Trash approval'):execute_json(*args,profile=self.p,runner=runner)
        self.assertEqual(runner.call_count,1)
        with patch('platform.system',return_value='Darwin'):
            with self.assertRaises(ValueError):invoke('trash',dict(approval_id='trash',deletePermanently=True),self.h,self.p)

    def test_client_busy_before_dispatch_can_be_retried_with_same_grant(self):
        self.approve('move',scope=dict(operation='move-message',mailbox='fixture@example.invalid',source_folder_id='account1://INBOX',
            destination_folder_id='account1://Filed',entry_id='12',internet_message_id='fixture@example.invalid'))
        runner=Mock(side_effect=[dict(result=dict(status='blocked',code='client-write-in-progress',mutation_may_have_occurred=False)),dict(result=dict(status='ok'))])
        args=('move',dict(approval_id='move'),'fixture@example.invalid',self.h,'mail:work')
        self.assertEqual(execute_json(*args,profile=self.p,runner=runner)['status'],'blocked')
        self.assertEqual(execute_json(*args,profile=self.p,runner=runner)['status'],'ok')
        self.assertEqual(runner.call_count,2)

    def test_calendar_write_exact_id_and_old_version(self):
        self.p['platform']['bindings']['calendar:work']=dict(backend='thunderbird',calendar_id='calendar-uuid')
        scope=dict(operation='calendar-update',calendar_id='calendar-uuid',event_id='fixture',icalendar=ICS,expected_icalendar='EXACT OLD VERSION')
        self.approve('calendar',scope=scope)
        runner=Mock(return_value=dict(result=dict(status='ok',client_verified=True,server_verified=False)))
        result=execute_json('calendar-update',dict(approval_id='calendar'),'fixture@example.invalid',self.h,'calendar:work',profile=self.p,runner=runner)
        self.assertFalse(result['data']['server_verified'])
        self.assertEqual(runner.call_args.args[0]['expected_icalendar'],'EXACT OLD VERSION')
        self.p['platform']['bindings']['calendar:work']['calendar_id']='another-calendar'
        with self.assertRaises(ValueError):execute_json('calendar-update',dict(approval_id='calendar'),'fixture@example.invalid',self.h,'calendar:work',profile=self.p,runner=runner)
        validate_event(ICS,'fixture')
        for bad in [ICS.replace('UID:fixture','UID:other'),ICS.replace('SUMMARY:Fixture','ATTENDEE:mailto:other@example.invalid'),ICS.replace(';TZID=Europe/London','')]:
            with self.assertRaises(ValueError):validate_event(bad,'fixture')

    def test_calendar_cancellation_remains_authoritative_in_shared_ingestion(self):
        self.p['platform']['bindings']['calendar:work']=dict(backend='thunderbird',calendar_id='calendar-uuid')
        runner=Mock(return_value=dict(result=dict(status='ok',complete=True,events=[dict(id='fixture',title='Fixture',icalendar=ICS.replace('SUMMARY:Fixture','SUMMARY:Fixture\r\nSTATUS:CANCELLED'))])))
        result=execute_json('calendar',dict(start='2027-03-27T00:00:00Z',end='2027-03-30T00:00:00Z'),'fixture@example.invalid',self.h,'calendar:work',profile=self.p,runner=runner)
        from monitor.sources import calendar_cancellations
        normalized=calendar_cancellations(result['data'])
        self.assertEqual(normalized['events'],[])
        self.assertEqual(normalized['explicit_cancellations'][0]['status'],'cancelled')

    def test_snapshot_continuation_is_immutable_and_source_bound(self):
        result=dict(status='ok',complete=True,scanned=2,messages=[dict(id=i,internet_message_id=str(i),date='2026-11-09T12:00:00Z',read=False,author='Fixture <fixture@example.invalid>',subject='Synthetic',folder={'id':'account1://INBOX','accountId':'account1'}) for i in [1,2]])
        runner=Mock(return_value={'result':result})
        payload=dict(start='2026-11-09T00:00:00+00:00',end='2026-11-10T00:00:00+00:00',page_size=1)
        args=('collect',payload,'fixture@example.invalid',self.h,'mail:work')
        first=execute_json(*args,profile=self.p,runner=runner)['data']
        continuation=dict(payload,offset=1,snapshot_id=first['SnapshotId'])
        second=execute_json('collect',continuation,'fixture@example.invalid',self.h,'mail:work',profile=self.p,runner=runner)['data']
        self.assertTrue(second['Complete']);self.assertEqual(runner.call_count,1)
        self.p['platform']['bindings']['mail:work']['selected_inbox_id']='account9://INBOX'
        with self.assertRaises(ValueError):execute_json('collect',continuation,'fixture@example.invalid',self.h,'mail:work',profile=self.p,runner=runner)
        freshness=execute_json('freshness',{},'fixture@example.invalid',self.h,'mail:work',profile=self.p,runner=runner)
        self.assertFalse(freshness['data']['Current']);self.assertEqual(runner.call_count,1)
        from monitor.sources import validate_mail
        with patch('monitor.sources.MAIL_STORES',{'mail:work':'fixture@example.invalid'}):
            self.assertFalse(validate_mail('mail:work',[first,second],first['StartedAt'],freshness['data']))

    def test_persisted_message_locator_resolves_new_session_integer(self):
        from outlook_platforms.thunderbird.commands import row
        message=dict(id=12,internet_message_id='fixture@example.invalid',date='2026-11-09T12:00:00Z',read=False,author='fixture@example.invalid',subject='Fixture',folder=dict(id='account1://Filed',accountId='account1',path='/Filed'))
        locator=row(message,'fixture@example.invalid')['EntryId'];message['id']=999
        runner=Mock(side_effect=[dict(result=dict(status='ok',complete=True,messages=[message])),dict(result=dict(status='ok',message=message,full=dict(parts=[dict(contentType='text/plain',body='Synthetic')]),read_state_preserved=True))])
        result=execute_json('read',dict(entry_id=locator),'fixture@example.invalid',self.h,'mail:work',profile=self.p,runner=runner)
        self.assertEqual(result['data']['EntryId'],locator);self.assertEqual(runner.call_args.args[0]['message_id'],999)
        self.assertTrue(result['data']['UnreadUnchanged'])

    def test_registration_preserves_other_installation_and_calendar_is_optional(self):
        root=Path(self.tmp.name)
        with patch('outlook_platforms.thunderbird.setup.data_root',return_value=root/'data'):
            first=prepare(manifest_dir=root/'hosts')
            import zipfile
            with zipfile.ZipFile(first['package']) as z:
                self.assertNotIn('experiment_apis',json.loads(z.read('manifest.json')))
            with patch('outlook_platforms.thunderbird.setup.data_root',return_value=root/'other'):
                with self.assertRaises(ValueError):prepare(manifest_dir=root/'hosts')
            second=prepare(calendar=True,manifest_dir=root/'hosts')
            with zipfile.ZipFile(second['package']) as z:
                self.assertIn('harnessCalendar',json.loads(z.read('manifest.json'))['experiment_apis'])

    def test_frames_reject_truncation_and_oversize(self):
        stream=io.BytesIO();write_frame(stream,dict(literal='café $(literal)'));stream.seek(0)
        self.assertEqual(read_frame(stream),dict(literal='café $(literal)'))
        for value in [b'\x01',b'\xff\xff\xff\xff']:
            with self.assertRaises(ValueError):read_frame(io.BytesIO(value))

    @unittest.skipUnless(sys.platform=='darwin','Mac native-messaging regression')
    def test_native_host_disconnects_never_abort_python(self):
        # In particular, closing Thunderbird's output pipe while input stays
        # open used to abort Python at buffered-stdin finalization.
        for mode in ('eof','partial','oversize','broken-output','roundtrip','disconnect-during-request'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as tmp:
                env=dict(os.environ,OUTLOOK_AUTOMATION_DATA=tmp)
                p=subprocess.Popen([sys.executable,'-m','outlook_platforms.thunderbird.native_host'],
                    stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env)
                try:
                    receipt=Path(tmp)/'thunderbird/runtime.json'
                    deadline=time.monotonic()+4
                    while not receipt.exists() and p.poll() is None and time.monotonic()<deadline:time.sleep(0.01)
                    if not receipt.exists():
                        error=p.stderr.read().decode() if p.poll() is not None else ''
                        if 'Operation not permitted' in error:self.skipTest('Unix socket bind requires unsandboxed test execution')
                        self.fail('Native host did not start: '+error)
                    if mode in {'eof','partial','oversize'}:
                        p.stdin.write({'eof':b'','partial':b'\x01','oversize':b'\xff\xff\xff\xff'}[mode]);p.stdin.close()
                    else:
                        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as client:
                            client.settimeout(4);client.connect(json.loads(receipt.read_text())['socket'])
                            with client.makefile('rwb') as channel:
                                if mode=='broken-output':p.stdout.close()
                                write_frame(channel,dict(action='status'))
                                if mode=='roundtrip':
                                    outgoing=read_frame(p.stdout)
                                    write_frame(p.stdin,dict(id=outgoing['id'],result=dict(status='ok')))
                                    self.assertEqual(read_frame(channel)['result']['status'],'ok')
                                    p.stdin.close()
                                elif mode=='disconnect-during-request':
                                    read_frame(p.stdout);p.stdin.close();read_frame(channel)
                                else:read_frame(channel)
                    self.assertEqual(p.wait(timeout=4),0,p.stderr.read().decode())
                    self.assertFalse(receipt.exists())
                    self.assertNotIn(b'Fatal Python error',p.stderr.read())
                finally:
                    if p.poll() is None:p.kill();p.wait()
                    for stream in (p.stdin,p.stdout,p.stderr):
                        if stream and not stream.closed:stream.close()
