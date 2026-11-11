from pathlib import Path
import json
from contextlib import closing
import sqlite3
import tempfile
import unittest
import zipfile
from unittest.mock import patch
from monitor.core import Harness
from monitor.configuration import resolve,read_yaml
from monitor.migration import run
from monitor.extensions import metadata


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.dbpath=self.root/'monitor/monitor.db';h=Harness(self.dbpath,create=True)
        h.put_evidence(dict(id='user',source='user',body='Private original context'))
        h.close()
        self.profile=self.root/'monitor/profile.json'
        self.profile.write_text(json.dumps(dict(schema_version=1,name='Example',mail={'mail:work':'fixture@example.invalid'},calendars=[],timezone='America/New_York',instructions={'reporting':'Personal report style'})),encoding='utf-8')

    def test_preview_is_read_only_and_apply_is_idempotent(self):
        original=self.profile.read_bytes();db=self.dbpath.read_bytes()
        self.assertEqual(run(self.root)['status'],'preview')
        self.assertEqual(self.profile.read_bytes(),original);self.assertEqual(self.dbpath.read_bytes(),db)
        self.assertFalse((self.root/'customisations.yaml').exists())
        self.assertEqual(run(self.root,True)['status'],'migrated')
        self.assertEqual(run(self.root,True)['status'],'already-migrated')
        self.assertNotIn('instructions',json.loads(self.profile.read_text()))
        effective=resolve(self.root,profile=json.loads(self.profile.read_text()))
        self.assertEqual(effective.get('timezone'),'America/New_York')
        self.assertEqual(effective.instruction('reporting'),'Personal report style')
        with zipfile.ZipFile(self.root/'.state/migrations/four-layer.zip') as archive:
            self.assertEqual(archive.read('before/monitor/profile.json'),original)
            self.assertIn('after/customisations.yaml',archive.namelist())

    def test_missing_diagnostic_bundle_rolls_back_and_preserves_observations(self):
        original=self.profile.read_bytes()
        with closing(sqlite3.connect(self.dbpath)) as db, db:metadata(db,'startup-observation',dict(expires_at='2099-01-01T00:00:00Z',entries={'one':{'outcome':'pending'}}))
        with self.assertRaisesRegex(ValueError,'bundle'):run(self.root,True)
        self.assertEqual(self.profile.read_bytes(),original)
        self.assertFalse((self.root/'customisations.yaml').exists())
        with closing(sqlite3.connect(self.dbpath)) as db:self.assertEqual(len(metadata(db,'startup-observation')['entries']),1)

    def test_daily_recovery_snapshot_includes_customisations(self):
        run(self.root,True)
        h=Harness(self.dbpath)
        try:h.backup('configuration')
        finally:h.close()
        with closing(sqlite3.connect(self.root/'monitor/backups/configuration.db')) as db:
            raw=db.execute("SELECT content FROM backup_configuration WHERE path='customisations.yaml'").fetchone()[0]
            self.assertEqual(bytes(raw),(self.root/'customisations.yaml').read_bytes())
        from monitor.backups import embedded_configuration
        self.assertEqual(embedded_configuration(self.root/'monitor/backups/configuration.db')['customisations.yaml'],(self.root/'customisations.yaml').read_bytes())

    def test_receipt_interruption_can_be_finalised_without_reapplying(self):
        from monitor.migration import atomic
        def interrupt(path,text):
            if Path(path).name=='four-layer-migration.json':raise OSError('Interrupted receipt write')
            return atomic(path,text)
        with patch('monitor.migration.atomic',side_effect=interrupt):
            with self.assertRaisesRegex(OSError,'Interrupted'):run(self.root,True)
        self.assertNotIn('instructions',json.loads(self.profile.read_text()))
        self.assertEqual(run(self.root,True)['status'],'already-migrated')
        self.assertTrue((self.root/'.state/four-layer-migration.json').exists())

    def test_existing_native_host_launcher_is_rebound(self):
        host=self.root/'thunderbird/native-host';host.parent.mkdir()
        host.write_text('#!/bin/sh\nexec python -m outlook_platforms.thunderbird.native_host\n',encoding='utf-8');host.chmod(0o700)
        run(self.root,True)
        self.assertIn('monitor.adapters.thunderbird.native_host',host.read_text())
        with zipfile.ZipFile(self.root/'.state/migrations/four-layer.zip') as archive:
            self.assertIn(b'outlook_platforms.thunderbird.native_host',archive.read('before/thunderbird/native-host'))


if __name__=='__main__':unittest.main()
