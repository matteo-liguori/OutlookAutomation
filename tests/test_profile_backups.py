"""Profile recovery stays paired with the database's bounded backup slots."""
import json
from datetime import datetime
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from monitor.core import Harness
from monitor.backups import embedded_profile
from tools.manage_data import restore_profile


class ProfileBackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.profile=self.root/'profile.json'
        self.value=dict(schema_version=1,name='Example user',timezone='Europe/London',
                        mail={'mail:work':'example@example.invalid'},calendars=['calendar:work'])
        self.save_profile()
        with patch('monitor.storage.Storage.backup',return_value=[]):
            self.h=Harness(self.root/'monitor.db',create=True)

    def tearDown(self):
        self.h.close();self.tmp.cleanup()

    def save_profile(self):
        raw=('\ufeff'+json.dumps(self.value,ensure_ascii=False,indent=2)+'\n').encode('utf-8')
        self.profile.write_bytes(raw)
        return raw

    def test_exact_profile_recovery_without_live_profile_or_overwriting_existing_file(self):
        raw=self.profile.read_bytes()
        backup=Path(self.h.backup()[0])
        self.assertEqual(embedded_profile(backup),raw)
        self.assertFalse(self.h.db.execute("SELECT 1 FROM sqlite_master WHERE name='backup_profile'").fetchone())
        self.profile.unlink()
        restore_profile(backup,self.profile)
        self.assertEqual(self.profile.read_bytes(),raw)
        with self.assertRaises(FileExistsError):restore_profile(backup,self.profile)
        self.assertEqual(self.profile.read_bytes(),raw)

    def test_same_slot_stays_frozen_and_two_daily_two_weekly_snapshots_include_matching_profiles(self):
        for day in ['2026-10-01','2026-10-08','2026-10-15']:
            self.value['name']=day;self.save_profile()
            with self.h.db:
                self.h.db.execute("INSERT OR REPLACE INTO meta VALUES('profile-name',?)",(day,))
            with patch('monitor.storage.datetime') as clock:
                clock.now.return_value=datetime.fromisoformat(day+'T09:00:00+00:00')
                paths=self.h.backup()
                frozen=Path(paths[0]).read_bytes()
                self.value['name']='Later same-day edit';self.save_profile()
                self.h.backup()
                self.assertEqual(Path(paths[0]).read_bytes(),frozen)
        folder=self.root/'backups'
        self.assertEqual(len(list(folder.glob('daily-*.db'))),2)
        self.assertEqual(len(list(folder.glob('weekly-*.db'))),2)
        for path in folder.glob('*.db'):
            profile=json.loads(embedded_profile(path).decode('utf-8-sig'))
            db=sqlite3.connect(path)
            try:self.assertEqual(db.execute("SELECT value FROM meta WHERE key='profile-name'").fetchone()[0],profile['name'])
            finally:db.close()

    def test_legacy_slot_is_upgraded_and_failed_profile_capture_keeps_original(self):
        self.profile.unlink()
        backup=Path(self.h.backup()[0]);original=backup.read_bytes()
        self.assertIsNone(embedded_profile(backup))
        self.save_profile()
        with patch('monitor.backups.include_profile',side_effect=OSError('Synthetic profile capture failure')):
            with self.assertRaises(OSError):self.h.backup()
        self.assertEqual(backup.read_bytes(),original)
        self.assertFalse(list(backup.parent.glob('*.tmp')))
        self.h.backup()
        self.assertEqual(embedded_profile(backup),self.profile.read_bytes())

    def test_corrupt_profile_snapshot_is_rejected_and_replaced(self):
        backup=Path(self.h.backup()[0])
        db=sqlite3.connect(backup)
        with db:db.execute("UPDATE backup_profile SET content=?",(b'corrupt',))
        db.close()
        with self.assertRaises(ValueError):embedded_profile(backup)
        self.h.backup()
        self.assertEqual(embedded_profile(backup),self.profile.read_bytes())
