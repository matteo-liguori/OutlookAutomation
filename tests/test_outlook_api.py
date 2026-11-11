"""One profile-selected interface preserves native scope and Windows contracts."""
import copy
import hashlib
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from monitor.adapters.api import invoke,check_send
from monitor.adapters.windows.commands import execute_json
from monitor.profile import platform_settings
from monitor.core import Harness

PROFILE=dict(name='Fixture',mail={'mail:work':'fixture@example.invalid'},calendars=['calendar:work'],
             platform=dict(os='windows',outlook_backend='com',bindings={}))


class OutlookApiTests(unittest.TestCase):
    def test_windows_send_journal_prevents_duplicate_after_success_or_uncertain_failure(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as tmp,patch('shutil.which',return_value='powershell'):
            h=Harness(Path(tmp)/'monitor.db',create=True)
            try:
                runner=Mock(return_value=subprocess.CompletedProcess([],0,json.dumps(dict(Status='submitted')),''))
                first=execute_json('send',{'approval_id':'one'},'fixture@example.invalid',h,runner)
                second=execute_json('send',{'approval_id':'one'},'fixture@example.invalid',h,runner)
                self.assertEqual(first['data'],second['data']);self.assertTrue(second['duplicate_prevented'])
                self.assertEqual(runner.call_count,1)
                runner.side_effect=subprocess.TimeoutExpired('helper',180)
                with self.assertRaises(ValueError):execute_json('send',{'approval_id':'two'},'fixture@example.invalid',h,runner)
                with self.assertRaisesRegex(ValueError,'reconciliation'):execute_json('send',{'approval_id':'two'},'fixture@example.invalid',h,runner)
                self.assertEqual(runner.call_count,2)
            finally:h.close()

    def test_windows_send_runs_transport_internally(self):
        calls=[]
        def backend(operation,*args):
            calls.append(operation)
            return dict(data=dict(success=True) if operation=='refresh' else dict(Status='submitted',NextStep='old helper instruction'))
        with patch('platform.system',return_value='Windows'),patch('monitor.adapters.api.check_send'),patch('monitor.adapters.windows.commands.execute_json',side_effect=backend):
            result=invoke('send',{'approval_id':'fixture'},profile=PROFILE)
        self.assertEqual(calls,['refresh','send','refresh'])
        self.assertFalse(result['data']['Verified'])
        self.assertNotIn('old helper',result['data']['NextStep'])

    def test_normal_help_and_parser_show_only_working_commands_without_platform_details(self):
        from monitor.__main__ import parser
        from monitor.commands import execute
        from monitor.adapters.api import status
        with patch.dict('monitor.adapters.api.PROFILE',self.mac_profile(),clear=True),patch('platform.system',return_value='Darwin'):
            public=status()
            self.assertNotIn('platform',public);self.assertNotIn('bindings',public)
            self.assertNotIn('move',public['operations']);self.assertNotIn('refresh',public['operations'])
            self.assertIn('collect',public['operations']);self.assertIn('send',public['operations'])
            partial=self.mac_profile();partial['platform']['bindings']['mail:work'].pop('draft_folder_id')
            self.assertNotIn('send',status(partial)['operations'])
            self.assertIn('read',status(partial)['operations'])
            self.assertIn('native',status(details=True)['backend'])
            help_text=execute(None,parser().parse_args(['help','outlook']))
            self.assertNotIn('Windows',json.dumps(help_text));self.assertNotIn('macos',json.dumps(help_text))
            for command in ['move','refresh','contacts','session']:
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):parser().parse_args(['outlook',command])
                with self.assertRaises(ValueError):execute(None,parser().parse_args(['help','outlook',command]))

    def test_collection_runs_preparation_internally_and_returns_same_contract(self):
        from monitor.adapters.api import collect_workflow
        page=dict(Store='fixture@example.invalid',Complete=True,Count=0,Items=[],NextOffset=None)
        outputs=[]
        for selected in ['windows','macos']:
            calls=[]
            def backend(operation,payload):
                calls.append(operation)
                if operation=='refresh':return dict(data=dict(success=True))
                if operation=='freshness':
                    proof=dict(Store=page['Store'],Current=True)
                    return dict(data=[proof] if selected=='windows' else proof)
                return dict(data=copy.deepcopy(page))
            outputs.append(collect_workflow(None,{},backend,selected,'mail:work'))
            self.assertEqual(calls,['refresh','freshness','collect'] if selected=='windows' else ['freshness','collect'])
        self.assertEqual(outputs[0],outputs[1])

    def test_mac_profile_bindings_replace_legacy_setting_and_keep_grant_body_out_of_argv(self):
        from monitor.adapters.macos.commands import execute_json as native_json
        from monitor.profile import native_bindings
        p=self.mac_profile();p['platform']['bindings']['mail:work']['draft_folder_id']='127'
        self.assertEqual(native_bindings(p,PROFILE['mail']['mail:work'])['draft_folder_id'],'127')
        completed=subprocess.CompletedProcess([],0,json.dumps(dict(status='ok',Status='submitted')), '')
        with patch('subprocess.run',return_value=completed) as run:
            native_json('send',dict(approval_id='approved',body='PRIVATE BODY',to=['other@example.invalid']),PROFILE['mail']['mail:work'],None,'mail:work',profile=p)
        argv=run.call_args.args[0]
        self.assertNotIn('PRIVATE BODY',' '.join(argv))
        self.assertIn('--drafts-folder-id',argv)

    def test_profile_dispatch_and_windows_json_preserve_literal_payload(self):
        literal='literal $(not code) `not code`; café\nsecond line'
        completed=subprocess.CompletedProcess([],0,json.dumps({'Rows':[]}), '')
        with patch('platform.system',return_value='Windows'),patch('shutil.which',return_value='powershell'),patch('monitor.adapters.windows.commands.subprocess.run',return_value=completed) as run:
            # Explicit runner is patched below because backend function defaults bind at import.
            with patch('monitor.adapters.windows.commands.execute_json',return_value={'data':[]} ) as execute:
                result=invoke('search',{'source':'mail:work','query':literal},profile=PROFILE)
            self.assertEqual(result['operation'],'search')
            self.assertNotIn('platform',result)
            self.assertNotIn('backend',result)
            self.assertEqual(execute.call_args.args[1]['query'],literal)
            execute_json('search',{'query':literal},PROFILE['mail']['mail:work'],runner=run)
        args,kwargs=run.call_args
        self.assertNotIn(literal,' '.join(args[0]))
        self.assertEqual(json.loads(kwargs['input'])['parameters']['Query'],literal)
        self.assertNotIn('shell',kwargs)

    def mac_profile(self):
        p=copy.deepcopy(PROFILE)
        p['platform']=dict(os='macos',outlook_backend='native',bindings={'mail:work':{'selected_inbox_id':'131','draft_folder_id':'127'},'calendar:work':{'calendar_id':'23'}})
        return p

    def test_mac_binding_and_partial_coverage_survive_common_interface(self):
        raw=dict(status='ok',items=[],truncated=True,matched_count=0,read_state_preserved=True,window={'start':'2026-11-08T00:00:00Z','end_exclusive':'2026-11-09T00:00:00Z'})
        completed=subprocess.CompletedProcess([],0,json.dumps(dict(data=[],coverage=raw)), '')
        with patch('platform.system',return_value='Darwin'),patch('subprocess.run',return_value=completed) as native:
            result=invoke('recent',dict(start='2026-11-08T00:00:00Z',end='2026-11-09T00:00:00Z'),profile=self.mac_profile())
        argv=native.call_args.args[0]
        self.assertEqual(argv[argv.index('--selected-inbox-id')+1],'131')
        self.assertTrue(result['coverage']['truncated'])
        self.assertEqual(result['data'],[])

    def test_mac_calendar_preserves_recurrence_gap_and_profile_source(self):
        raw=dict(status='ok',items=[],truncated=False,matched_count=0,window={'start':'2026-11-08T00:00:00Z','end_exclusive':'2026-11-09T00:00:00Z'})
        data=dict(source='calendar:work',events=[],complete=False,errors=['Native recurrence expansion has not been verified.'])
        completed=subprocess.CompletedProcess([],0,json.dumps(dict(data=data,coverage=raw)), '')
        with patch('platform.system',return_value='Darwin'),patch('subprocess.run',return_value=completed) as native:
            result=invoke('calendar',dict(start='2026-11-08T00:00:00Z',end='2026-11-09T00:00:00Z'),profile=self.mac_profile())
        argv=native.call_args.args[0]
        self.assertEqual(argv[argv.index('--calendar-id')+1],'23')
        self.assertFalse(result['data']['complete'])
        self.assertIn('recurrence',result['data']['errors'][0])

    def test_unsupported_mismatched_or_unbound_requests_do_not_connect(self):
        p=self.mac_profile()
        with patch('monitor.adapters.macos.native.execute') as native:
            with patch('platform.system',return_value='Windows'):
                with self.assertRaises(ValueError):invoke('recent',{},profile=p)
            with patch('platform.system',return_value='Darwin'):
                self.assertEqual(invoke('refresh',{},profile=p)['status'],'unsupported')
                p['platform']['bindings']={}
                self.assertEqual(invoke('read',{'entry_id':'1'},profile=p)['status'],'unsupported')
            native.assert_not_called()

    def test_invalid_source_timestamp_or_backend_cannot_redirect_access(self):
        with patch('platform.system',return_value='Windows'),patch('monitor.adapters.windows.commands.execute_json') as native:
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
            root=Path(tmp);folder=root/'monitor/adapters/windows';folder.mkdir(parents=True)
            source=Path(__file__).resolve().parents[1]/'monitor/adapters/windows/invoke.ps1'
            shutil.copyfile(source,folder/'invoke.ps1')
            (root/'monitor/adapters/windows/Read-OutlookMail.ps1').write_text("param($Query,[string[]]$Values,[switch]$UnreadOnly)\n@{query=$Query;values=$Values;flag=[bool]$UnreadOnly}|ConvertTo-Json",encoding='utf-8')
            payload=dict(helper='Read-OutlookMail.ps1',parameters=dict(Query='café $(literal)',Values=['one','two'],UnreadOnly=False))
            command=[shutil.which('pwsh') or shutil.which('powershell'),'-NoProfile','-NonInteractive','-File',str(folder/'invoke.ps1')]
            out=subprocess.run(command,input=json.dumps(payload),text=True,encoding='utf-8-sig',capture_output=True,timeout=15)
            self.assertEqual(out.returncode,0,out.stderr)
            self.assertEqual(json.loads(out.stdout),dict(query='café $(literal)',values=['one','two'],flag=False))
            (root/'monitor/adapters/windows/Read-OutlookMail.ps1').write_text("param()\n'[]'",encoding='utf-8')
            out=subprocess.run(command,input=json.dumps(dict(helper='Read-OutlookMail.ps1',parameters={})),text=True,encoding='utf-8-sig',capture_output=True,timeout=15)
            self.assertEqual(json.loads(out.stdout),[])
            (root/'monitor/adapters/windows/Read-OutlookMonitorCalendar.ps1').write_text("param([datetime]$Start,[datetime]$End)\n@{start=$Start.ToUniversalTime().ToString('o');end=$End.ToUniversalTime().ToString('o')}|ConvertTo-Json",encoding='utf-8')
            out=subprocess.run(command,input=json.dumps(dict(helper='Read-OutlookMonitorCalendar.ps1',parameters=dict(Start='2026-10-23T00:00:00+01:00',End='2026-10-31T00:00:00Z'))),text=True,encoding='utf-8-sig',capture_output=True,timeout=15)
            self.assertEqual(out.returncode,0,out.stderr)
            self.assertTrue(json.loads(out.stdout)['start'].startswith('2026-10-22T23:00:00'))
