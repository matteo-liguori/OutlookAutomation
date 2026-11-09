"""One profile-selected interface preserves native scope and Windows contracts."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from outlook_platforms.api import invoke,check_send
from outlook_platforms.windows.commands import execute_json
from monitor.profile import platform_settings
from monitor.core import Harness

PROFILE=dict(name='Fixture',mail={'mail:work':'fixture@example.invalid'},calendars=['calendar:work'],
             platform=dict(os='windows',outlook_backend='com',bindings={}))


class OutlookApiTests(unittest.TestCase):
    def test_profile_dispatch_and_windows_json_preserve_literal_payload(self):
        literal='literal $(not code) `not code`; café\nsecond line'
        completed=subprocess.CompletedProcess([],0,json.dumps({'Rows':[]}), '')
        with patch('platform.system',return_value='Windows'),patch('shutil.which',return_value='powershell'),patch('outlook_platforms.windows.commands.subprocess.run',return_value=completed) as run:
            # Explicit runner is patched below because backend function defaults bind at import.
            with patch('outlook_platforms.windows.commands.execute_json',return_value={'data':[]} ) as execute:
                result=invoke('search',{'source':'mail:work','query':literal},profile=PROFILE)
            self.assertEqual(result['platform'],'windows')
            self.assertEqual(execute.call_args.args[1]['query'],literal)
            execute_json('search',{'query':literal},PROFILE['mail']['mail:work'],runner=run)
        args,kwargs=run.call_args
        self.assertNotIn(literal,' '.join(args[0]))
        self.assertEqual(json.loads(kwargs['input'])['parameters']['Query'],literal)
        self.assertNotIn('shell',kwargs)

    def mac_profile(self):
        p=copy.deepcopy(PROFILE)
        p['platform']=dict(os='macos',outlook_backend='native',bindings={'mail:work':{'selected_inbox_id':'131'},'calendar:work':{'calendar_id':'23'}})
        return p

    def test_mac_binding_and_partial_coverage_survive_common_interface(self):
        raw=dict(status='ok',items=[],truncated=True,matched_count=0,read_state_preserved=True,window={'start':'2026-11-08T00:00:00Z','end_exclusive':'2026-11-09T00:00:00Z'})
        with patch('platform.system',return_value='Darwin'),patch('outlook_platforms.macos.native.execute',return_value=raw) as native:
            result=invoke('recent',dict(start='2026-11-08T00:00:00Z',end='2026-11-09T00:00:00Z'),profile=self.mac_profile())
        self.assertEqual(native.call_args.args[0]['folder_id'],'131')
        self.assertTrue(result['coverage']['truncated'])
        self.assertEqual(result['data'],[])

    def test_mac_calendar_preserves_recurrence_gap_and_profile_source(self):
        raw=dict(status='ok',items=[],truncated=False,matched_count=0,window={'start':'2026-11-08T00:00:00Z','end_exclusive':'2026-11-09T00:00:00Z'})
        with patch('platform.system',return_value='Darwin'),patch('outlook_platforms.macos.native.execute',return_value=raw) as native:
            result=invoke('calendar',dict(start='2026-11-08T00:00:00Z',end='2026-11-09T00:00:00Z'),profile=self.mac_profile())
        self.assertEqual(native.call_args.args[0]['calendar_id'],'23')
        self.assertFalse(result['data']['complete'])
        self.assertIn('recurrence',result['data']['errors'][0])

    def test_unsupported_mismatched_or_unbound_requests_do_not_connect(self):
        p=self.mac_profile()
        with patch('outlook_platforms.macos.native.execute') as native:
            with patch('platform.system',return_value='Windows'):
                with self.assertRaises(ValueError):invoke('recent',{},profile=p)
            with patch('platform.system',return_value='Darwin'):
                self.assertEqual(invoke('send',{},profile=p)['status'],'unsupported')
                p['platform']['bindings']={}
                with self.assertRaises(ValueError):invoke('read',{'entry_id':'1'},profile=p)
            native.assert_not_called()

    def test_invalid_source_timestamp_or_backend_cannot_redirect_access(self):
        with patch('platform.system',return_value='Windows'),patch('outlook_platforms.windows.commands.execute_json') as native:
            for payload in [{'source':'stranger@example.invalid','query':'x'},{'start':'2026-11-08T09:00:00'},{'unread_only':'false'},{'store':'stranger@example.invalid','query':'x'}]:
                with self.assertRaises(ValueError):invoke('search',payload,profile=PROFILE)
            native.assert_not_called()
        p=copy.deepcopy(PROFILE);p['platform']['outlook_backend']='native'
        with self.assertRaises(ValueError):platform_settings(p)

    def test_send_requires_exact_scope_and_unchanged_attachments(self):
        from monitor.approvals import put
        with tempfile.TemporaryDirectory() as tmp:
            h=Harness(Path(tmp)/'monitor.db',create=True)
            try:
                h.put_evidence(dict(id='a',source='user',body='Approved exact synthetic message'))
                attachment=Path(tmp)/'fixture.zip';attachment.write_bytes(b'approved fixture')
                message=dict(sender='fixture@example.invalid',to=['to@example.invalid'],cc=[],bcc=[],subject='Test',body='Exact',attachments=[dict(path=str(attachment),sha256=hashlib.sha256(attachment.read_bytes()).hexdigest())])
                put(h,dict(id='send',kind='one-off',message=message,evidence_ids=['a']))
                payload={k:v for k,v in message.items() if k!='sender'};payload['approval_id']='send'
                payload['attachments']=[str(attachment)]
                check_send(h,payload,message['sender'])
                attachment.write_bytes(b'changed fixture')
                with self.assertRaises(ValueError):check_send(h,payload,message['sender'])
                attachment.write_bytes(b'approved fixture')
                payload['body']='Different'
                with self.assertRaises(ValueError):check_send(h,payload,message['sender'])
                with self.assertRaises(ValueError):check_send(h,{},message['sender'])
            finally:h.close()

    @unittest.skipUnless(shutil.which('pwsh') or shutil.which('powershell'),'PowerShell required')
    def test_real_json_bridge_preserves_unicode_arrays_and_empty_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);folder=root/'outlook_platforms/windows';folder.mkdir(parents=True)
            source=Path(__file__).resolve().parents[1]/'outlook_platforms/windows/invoke.ps1'
            shutil.copyfile(source,folder/'invoke.ps1')
            (root/'Read-OutlookMail.ps1').write_text("param($Query,[string[]]$Values,[switch]$UnreadOnly)\n@{query=$Query;values=$Values;flag=[bool]$UnreadOnly}|ConvertTo-Json",encoding='utf-8')
            payload=dict(helper='Read-OutlookMail.ps1',parameters=dict(Query='café $(literal)',Values=['one','two'],UnreadOnly=False))
            command=[shutil.which('pwsh') or shutil.which('powershell'),'-NoProfile','-NonInteractive','-File',str(folder/'invoke.ps1')]
            out=subprocess.run(command,input=json.dumps(payload),text=True,encoding='utf-8-sig',capture_output=True,timeout=15)
            self.assertEqual(out.returncode,0,out.stderr)
            self.assertEqual(json.loads(out.stdout),dict(query='café $(literal)',values=['one','two'],flag=False))
            (root/'Read-OutlookMail.ps1').write_text("param()\n'[]'",encoding='utf-8')
            out=subprocess.run(command,input=json.dumps(dict(helper='Read-OutlookMail.ps1',parameters={})),text=True,encoding='utf-8-sig',capture_output=True,timeout=15)
            self.assertEqual(json.loads(out.stdout),[])
