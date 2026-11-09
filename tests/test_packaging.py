"""A share build must boot independently and contain source, never live state."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

from build_share_package import ROOT, build


class PackagingTests(unittest.TestCase):
    def test_archive_manifest_and_private_state_exclusion(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "starter.zip"
            result = build(target)
            with zipfile.ZipFile(target) as archive:
                contents = {name.removeprefix("OutlookAutomation/"): archive.read(name) for name in archive.namelist()}
            manifest = json.loads(contents.pop("PACKAGE_MANIFEST.json"))
            self.assertEqual(set(manifest["files"]), set(contents))
            for name, data in contents.items():
                self.assertEqual(manifest["files"][name], hashlib.sha256(data).hexdigest())
                self.assertNotIn(".local", Path(name).parts)
                self.assertNotIn(".git", Path(name).parts)
                self.assertNotIn(Path(name).suffix, {".db", ".msg", ".eml", ".pst", ".ost"})
            self.assertEqual(len(contents) + 1, result["files"])
            self.assertIn("-WorkStore", contents["README.md"].decode())
            with self.assertRaises(FileExistsError):
                build(target)

    def test_allowlist_rejects_path_escape_and_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "packaging").mkdir()
            (root / "packaging/files.json").write_text(json.dumps({"../escaped.txt": "some.txt"}))
            with self.assertRaisesRegex(ValueError, "Invalid archive path"):
                build(root / "bad.zip", root)
            (root / "packaging/files.json").write_text(json.dumps({"source.txt": "../outside.txt"}))
            with self.assertRaisesRegex(ValueError, "within the project"):
                build(root / "bad.zip", root)

    @unittest.skipUnless(shutil.which("pwsh") or shutil.which("powershell"), "PowerShell required")
    def test_fresh_extracted_package_initialises_single_user_and_preserves_existing_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            build(tmp / "starter.zip")
            with zipfile.ZipFile(tmp / "starter.zip") as archive:
                archive.extractall(tmp / "installed")
            folder = tmp / "installed/OutlookAutomation"
            runtime = folder / ".local/monitor/runtime.json"
            runtime.parent.mkdir(parents=True)
            runtime.write_text(json.dumps({"python": sys.executable}))
            env = dict(os.environ)
            env.pop("OUTLOOK_AUTOMATION_PROFILE", None)
            ps = shutil.which("pwsh") or shutil.which("powershell")
            command = [ps, "-NoProfile", "-NonInteractive", "-File", str(folder / "Initialize-OutlookAutomation.ps1"), "-Name", "New user", "-WorkStore", "new.user@example.com"]
            result = subprocess.run(command, cwd=folder, env=env, capture_output=True, text=True, timeout=45)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            help_result = subprocess.run([sys.executable, '-m', 'monitor', '--format', 'json',
                                          'help', 'case', 'update'], cwd=folder, env=env,
                                         capture_output=True, text=True, timeout=15)
            self.assertEqual(help_result.returncode, 0, help_result.stderr)
            self.assertEqual(json.loads(help_result.stdout)['topic'], 'card-writes')
            check = subprocess.run([sys.executable, "-c", "from monitor.core import Harness,MAIL,CAL; from monitor.sources import MAIL_STORES; import json; h=Harness(); print(json.dumps(dict(mail=MAIL,cal=CAL,stores=MAIL_STORES,cases=len(h.all_cases()),approvals=h.db.execute('select count(*) from approvals').fetchone()[0],standing=h.db.execute('select count(*) from standing_approvals').fetchone()[0],doctor=h.doctor()))); h.close()"], cwd=folder, env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(check.returncode, 0, check.stderr)
            data = json.loads(check.stdout)
            self.assertEqual(data["mail"], ["mail:work"])
            self.assertEqual(data["cal"], ["calendar:work"])
            self.assertEqual(data["stores"], {"mail:work": "new.user@example.com"})
            self.assertEqual((data["cases"], data["approvals"], data["standing"]), (0, 0, 0))
            profile = (folder / ".local/monitor/profile.json").read_bytes()
            retry = subprocess.run(command, cwd=folder, env=env, capture_output=True, text=True, timeout=30)
            self.assertNotEqual(retry.returncode, 0)
            self.assertEqual(profile, (folder / ".local/monitor/profile.json").read_bytes())

    def test_profile_rejects_wrong_store_topology(self):
        from monitor.profile import load
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "profile.json"
            sample = json.loads((ROOT / "profile.example.json").read_text())
            sample["mail"]["mail:personal"] = sample["mail"]["mail:work"]
            path.write_text(json.dumps(sample))
            with self.assertRaisesRegex(ValueError, "distinct"):
                load(path)
            sample["mail"] = {"mail:personal": "me@example.com"}
            path.write_text(json.dumps(sample))
            with self.assertRaisesRegex(ValueError, "work calendar"):
                load(path)

    def test_mac_bundle_routes_to_native_pilot_without_claiming_windows_test_is_macos(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "mac.zip"
            build(target, target_platform="macos")
            with zipfile.ZipFile(target) as archive:
                readme = archive.read("OutlookAutomation/README.md").decode()
                agents = archive.read("OutlookAutomation/AGENTS.md").decode()
                self.assertIn("macOS Apple-event adapters", readme)
                self.assertIn("python3 setup_outlook.py --smoke-test", readme)
                self.assertIn("mac_outlook.py probe", readme)
                self.assertIn("Mac pilot", agents)
                self.assertIn("OutlookAutomation/requirements-mac.lock", archive.namelist())
                for name in ('mac_outlook_native.py', 'outlook.py', 'outlook_platforms/macos/native_outlook.js',
                             'outlook_platforms/macos/test_actions.js', 'outlook_platforms/windows/commands.py',
                             'outlook_platforms/macos/operations.js', 'outlook_platforms/macos/collector.py',
                             'outlook_platforms/macos/calendar.py', 'outlook_platforms/macos/attachments.py',
                             'outlook_platforms/macos/freshness.py', 'tests/test_mac_operations.py',
                             'tests/test_mac_collector.py', 'tests/test_mac_calendar.py', 'tests/test_mac_freshness.py',
                             'tests/test_mac_native.py', 'tests/test_mac_probe_fallback.py', 'tests/test_mac_test_actions.py', 'tests/test_platform_commands.py'):
                    self.assertIn('OutlookAutomation/' + name, archive.namelist())

    def test_optional_calendar_sources_define_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "profile.json"
            path.write_text(json.dumps(dict(schema_version=1, name="Example", timezone="Europe/London", mail={"mail:work": "example@example.com"}, calendars=["calendar:work", "calendar:primary"])))
            env = dict(os.environ, OUTLOOK_AUTOMATION_PROFILE=str(path))
            script = "from monitor.core import MAIL,CAL; from monitor.google_calendar import prepare; import json; print(json.dumps(dict(mail=MAIL,cal=CAL,google=prepare({'calendar:primary':'primary'},'2026-11-02')['sources'])))"
            result = subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=env, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(result.stdout)
            self.assertEqual(data["cal"], ["calendar:work", "calendar:primary"])
            self.assertEqual(data["google"], [{"source": "calendar:primary", "calendar_id": "primary"}])
