"""Use isolated app definitions; no real scheduler, mailbox or calendar calls."""
import json
import os
from pathlib import Path
import tempfile
import shutil
import subprocess
import unittest
from unittest.mock import patch

from monitor.configuration import sync, paths, read_yaml, atomic, dump, resolve, snapshot, notices
from monitor.automation_sync import reconcile, preflight


class AutomationSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.user=self.root/'private';self.home=self.root/'codex'
        sync(self.user,profile={})
        self.at='2026-11-13T08:00:00+00:00'
        self.bindings=[dict(id='morning',role='morning')]
        self.app=dict(id='morning',kind='cron',name='Mail',prompt='Existing user instructions',status='ACTIVE',
                      rrule='FREQ=DAILY;BYHOUR=9;BYMINUTE=0',model='fixture',reasoning_effort='medium',
                      execution_environment='local',target={'type':'project','project_id':'project'})
        self.write_app(self.app)
        self.configure()
        p=patch.dict(os.environ,CODEX_HOME=str(self.home),OUTLOOK_AUTOMATION_CUSTOMISATIONS=str(self.user/'customisations.yaml'))
        p.start();self.addCleanup(p.stop)

    def write_app(self,a):
        path=self.home/'automations'/a['id']/'automation.toml';path.parent.mkdir(parents=True,exist_ok=True)
        # JSON scalars are TOML-compatible; target is an inline table.
        text='\n'.join(k+' = '+(('{ type = "project", project_id = '+json.dumps(v['project_id'])+' }')
                       if k=='target' else json.dumps(v)) for k,v in a.items())
        path.write_text(text,encoding='utf-8')

    def configure(self, **changes):
        path,_=paths(self.user);user=read_yaml(path)
        user['schedule']['automations']=self.bindings
        for key,value in changes.items():
            if key=='timezone':user[key]=value
            else:user['schedule'][key]=value
        atomic(path,dump(user))

    def result(self,at=None):return reconcile(self.user,self.home,at or self.at)

    def apply(self,operation):
        args=operation['arguments']
        # Simulate independent app readback, not a self-reported success receipt.
        a=dict(self.app,rrule=args['rrule'],prompt=args['prompt'],name=args['name'])
        self.write_app(a);self.app=a

    def test_yaml_drives_morning_timezone_and_preserves_user_fields(self):
        self.configure(morning='10:25',timezone='America/New_York')
        r=self.result();self.assertEqual(r['status'],'pending')
        args=r['operations'][0]['arguments']
        self.assertIn('TZID=America/New_York',args['rrule']);self.assertIn('T102500',args['rrule'])
        self.assertEqual(args['projectId'],'project');self.assertEqual(args['model'],'fixture')
        self.assertIn('Existing user instructions',args['prompt'])
        self.apply(r['operations'][0]);self.assertEqual(self.result()['status'],'synced')
        # Named-zone recurrence remains stable across a DST/date change.
        self.assertEqual(self.result('2026-11-14T08:00:00+00:00')['status'],'synced')

    def test_pending_is_durable_until_actual_definition_matches(self):
        sync(self.user,profile={})
        self.configure(morning='11:10');sync(self.user,profile={})
        self.assertEqual(self.result()['status'],'pending');self.assertEqual(self.result()['status'],'pending')
        self.assertTrue(notices(self.user));self.assertTrue(notices(self.user))
        self.apply(self.result()['operations'][0])
        self.assertEqual(self.result()['status'],'synced');self.assertEqual(notices(self.user),[])
        self.app['rrule']='FREQ=DAILY;BYHOUR=7;BYMINUTE=0';self.write_app(self.app)
        self.assertEqual(self.result()['status'],'pending')

    def test_paused_and_missing_work_are_not_reenabled_or_recreated(self):
        self.app['status']='PAUSED';self.write_app(self.app)
        self.assertEqual(self.result()['operations'],[])
        (self.home/'automations/morning/automation.toml').unlink()
        r=self.result();self.assertTrue(r['issues']);self.assertEqual(r['operations'],[])

    def test_unrelated_automation_is_not_adopted(self):
        self.bindings=[];self.configure()
        self.assertEqual(self.result()['status'],'unmanaged')

    def setup_followups(self):
        self.app=dict(id='followup',kind='heartbeat',name='Mail',prompt='Old mail instructions',status='ACTIVE',
                      target_thread_id='chat',rrule='FREQ=DAILY;COUNT=2;BYHOUR=14,19;BYMINUTE=0')
        self.write_app(self.app)
        self.bindings=[dict(id='followup',role='continuations',date='2026-11-13',thread_id='chat')]
        self.configure(continuations=['14:25','19:45'])

    def test_nonuniform_minutes_have_exact_bounded_dates(self):
        from monitor.timing import recurrence_instants
        self.setup_followups()
        r=self.result();rule=r['operations'][0]['arguments']['rrule']
        self.assertIn('RDATE:',rule)
        self.assertEqual([p.strftime('%H:%M') for p in recurrence_instants(rule,self.at)],['14:25','19:45'])
        self.apply(r['operations'][0]);self.assertEqual(self.result()['status'],'synced')
        self.assertEqual(self.result('2026-11-14T08:00:00+00:00')['operations'][0]['arguments']['mode'],'delete')

    def test_disabled_followups_delete_only_bound_active_heartbeat(self):
        self.setup_followups()
        self.configure(continuations=[])
        r=self.result();self.assertEqual(r['operations'][0]['arguments'],{'mode':'delete','id':'followup'})

    def test_wrong_chat_binding_is_not_mutated(self):
        self.setup_followups()
        self.bindings[0]['thread_id']='different-chat';self.configure()
        self.assertTrue(self.result()['issues']);self.assertFalse(self.result()['operations'])

    def test_preflight_rejects_concurrent_yaml_or_app_edits(self):
        with patch('monitor.automation_sync.now',return_value=self.at):
            request=self.result();self.assertTrue(preflight(request)['verified'])
            self.configure(morning='11:15')
            with self.assertRaises(ValueError):preflight(request)

    def test_config_set_returns_sync_for_new_value_not_old_snapshot(self):
        from monitor.commands import execute
        from monitor.__main__ import parser
        with patch('monitor.commands.read_payload',return_value={'key':'schedule.morning','value':'11:20'}):
            result=execute(None,parser().parse_args(['config','set','--input','-']))
        self.assertIn('T112000',result['automation_sync']['operations'][0]['arguments']['rrule'])

    def test_templates_and_roundup_guidance_follow_configuration(self):
        self.configure(morning='10:25',continuations=['15:10'],roundup_weekday=2,roundup_start='10:00',roundup_end='11:00',timezone='America/New_York')
        config=resolve(self.user,profile={})
        text=config.instruction('activation');report=config.render(config.get('templates.report'))
        self.assertIn('10:25 America/New_York',text);self.assertIn('15:10',text)
        self.assertIn('Wednesday',report);self.assertNotIn('Sunday 09:00',report)
        self.assertNotIn('{{',report)

    def test_user_literal_instructions_are_not_rewritten_as_schedule_values(self):
        config=resolve(self.user,profile={}).overlay({'instructions':{'reporting':'Appointment at 09:00 London; schedule {{schedule.morning}}'},'schedule':{'morning':'10:25'}},'user')
        self.assertEqual(config.instruction('reporting'),'Appointment at 09:00 London; schedule 10:25')

    def test_review_retention_and_policy_consumers_use_current_values(self):
        from monitor.backups import limits
        from monitor.maintenance import policy
        from monitor.policy import effective
        from monitor.runs import roundup_eligible
        config=resolve(self.user,profile={}).overlay({'maintenance':{'daily_backups':3,'weekly_backups':4,'special_backups':2,'cache_days':6},'permission_defaults':{'routine_filing':False},'schedule':{'roundup_weekday':4,'roundup_start':'12:00','roundup_end':'13:00'}},'user')
        with snapshot(config):
            self.assertEqual(limits(),{'daily':3,'weekly':4,'special':2})
            self.assertEqual(policy()['cache_days'],6);self.assertFalse(effective({})['routine_filing'])
            self.assertTrue(roundup_eligible('2026-11-13T12:30:00+00:00'))
            self.assertFalse(roundup_eligible('2026-11-15T09:00:00+00:00'))

    @unittest.skipUnless(shutil.which('node'), 'Node executor fixture')
    def test_shared_executor_uses_supported_tool_and_requires_preflight(self):
        result=subprocess.run(['node','tests/Test-AutomationSync.js'],capture_output=True,text=True,
                              cwd=Path(__file__).resolve().parents[1],timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)


if __name__=='__main__':unittest.main()
