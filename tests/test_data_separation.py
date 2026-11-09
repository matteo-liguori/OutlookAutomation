"""Independent installations reuse source without inheriting another user's state."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from automation_paths import data_root, configure_data, ROOT
from manage_data import relocate
from manage_data import replace_paths
from setup_outlook import initialise


class DataSeparationTests(unittest.TestCase):
    def test_owned_path_alias_is_rebased_by_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp).resolve();old=base/'old';new=base/'new';alias=base/'alias'
            resolve=Path.resolve
            def canonical(path,*args,**kwargs):
                if path==alias/'owned.txt':return old/'owned.txt'
                return resolve(path,*args,**kwargs)
            with patch.object(Path,'resolve',canonical):
                self.assertEqual(replace_paths(str(alias/'owned.txt'),old,new),str(new/'owned.txt'))
    def test_permission_defaults_overlay_does_not_export_individual_grants(self):
        from monitor.policy import DEFAULTS,effective
        current=effective({})
        self.assertTrue(current['local_records'])
        self.assertTrue(current['routine_filing'])
        self.assertFalse(current['filing_deletes_mail'])
        changed=effective({'permission_defaults':{'routine_filing':False,'sending':'ask-every-time'}})
        self.assertFalse(changed['routine_filing'])
        self.assertEqual(changed['sending'],'ask-every-time')
        self.assertTrue(DEFAULTS['routine_filing'])
        self.assertNotIn('approvals',changed)
        with self.assertRaises(ValueError):effective({'permission_defaults':{'sending':True}})
    def test_configured_data_root_and_env_override(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ):
            os.environ.pop('OUTLOOK_AUTOMATION_DATA',None)
            root=Path(tmp).resolve()/'code';root.mkdir()
            first=Path(tmp).resolve()/'first';second=Path(tmp).resolve()/'second'
            self.assertEqual(configure_data(first,root),first)
            self.assertEqual(data_root(root),first)
            with self.assertRaises(ValueError):configure_data(second,root)
            os.environ['OUTLOOK_AUTOMATION_DATA']=str(second)
            self.assertEqual(data_root(root),second)
            os.environ['OUTLOOK_AUTOMATION_DATA']='relative/data'
            with self.assertRaises(ValueError):data_root(root)

    def test_fresh_profile_can_live_outside_code_and_existing_state_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ):
            os.environ.pop('OUTLOOK_AUTOMATION_PROFILE',None)
            os.environ.pop('OUTLOOK_AUTOMATION_DATA',None)
            root=Path(tmp).resolve()/'code';root.mkdir();private=Path(tmp).resolve()/'private'
            initialise('Example','example@example.invalid',root,data_dir=private)
            self.assertTrue((private/'monitor/profile.json').is_file())
            self.assertFalse((root/'.local/monitor/profile.json').exists())
            with self.assertRaises(ValueError):initialise('Other','other@example.invalid',root,data_dir=private)
            with self.assertRaises(ValueError):initialise('Other','other@example.invalid',root,data_dir=Path(tmp)/'elsewhere')
            self.assertEqual(data_root(root),private)

    def test_relocation_rebases_owned_paths_but_preserves_full_message_body(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ):
            os.environ.pop('OUTLOOK_AUTOMATION_DATA',None)
            root=Path(tmp).resolve()/'code';old=root/'.local';old.mkdir(parents=True)
            new=Path(tmp).resolve()/'private';(old/'monitor').mkdir()
            attachment=old/'monitor/owned.txt';attachment.write_text('Attachment')
            db=sqlite3.connect(old/'monitor/monitor.db')
            db.executescript('CREATE TABLE thread_attachments(path TEXT);CREATE TABLE thread_messages(body TEXT);')
            body='Email text mentioning '+str(attachment)
            db.execute('INSERT INTO thread_attachments VALUES(?)',(str(attachment),))
            db.execute('INSERT INTO thread_messages VALUES(?)',(body,));db.commit();db.close()
            result=relocate(new,root)
            self.assertEqual(result['path_fields_updated'],1)
            self.assertEqual(data_root(root),new)
            self.assertTrue((new/'monitor/owned.txt').exists())
            self.assertFalse((old/'monitor').exists())
            db=sqlite3.connect(new/'monitor/monitor.db')
            self.assertEqual(db.execute('SELECT body FROM thread_messages').fetchone()[0],body)
            self.assertEqual(db.execute('SELECT path FROM thread_attachments').fetchone()[0],str(new/'monitor/owned.txt'))
            db.close()

    def test_two_users_have_independent_identity_database_and_private_guidance(self):
        with tempfile.TemporaryDirectory() as tmp:
            for label in ['first','second']:
                private=Path(tmp).resolve()/label;(private/'monitor').mkdir(parents=True)
                profile=dict(schema_version=1,name=label,timezone='Europe/London',mail={'mail:work':label+'@example.invalid'},calendars=['calendar:work'],instructions={'mail-access':label+' private recovery preference'})
                (private/'monitor/profile.json').write_text(json.dumps(profile))
                env=dict(os.environ,OUTLOOK_AUTOMATION_DATA=str(private))
                env.pop('OUTLOOK_AUTOMATION_PROFILE',None)
                script="from monitor.core import Harness,MAIL;from monitor.guidance import guide;from monitor.profile import PROFILE;import json;h=Harness(create=True);print(json.dumps([PROFILE['name'],MAIL,len(h.all_cases()),guide('mail-access')['instruction'],str(h.path)]));h.close()"
                result=subprocess.run([sys.executable,'-c',script],cwd=ROOT,env=env,capture_output=True,text=True,timeout=20)
                self.assertEqual(result.returncode,0,result.stderr)
                name,mail,cases,instruction,db=json.loads(result.stdout)
                self.assertEqual((name,cases,instruction),(label,0,label+' private recovery preference'))
                self.assertEqual(Path(db),private/'monitor/monitor.db')

    def test_explicit_legacy_source_roles_come_from_profile_not_code(self):
        from monitor.profile import load
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'profile.json'
            p.write_text(json.dumps(dict(schema_version=1,name='Example',timezone='Europe/London',mail={'mail:legacy':'example@example.invalid'},calendars=['calendar:legacy'],roles={'work_mail':'mail:legacy','work_calendar':'calendar:legacy'},calendar_providers={'calendar:legacy':'outlook'})))
            self.assertEqual(load(p)['roles']['work_mail'],'mail:legacy')
