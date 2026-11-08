import json
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from mac_outlook import dictionary_summary, probe
from setup_outlook import initialise, verify_package


class MacSupportTests(unittest.TestCase):
    def test_native_probe_does_not_claim_windows_is_mac(self):
        runner = unittest.mock.Mock()
        self.assertEqual(probe(system="Windows", runner=runner)["status"], "requires-macos")
        runner.assert_not_called()

    def test_dictionary_drives_capabilities_including_extensions(self):
        summary = dictionary_summary('<dictionary><suite><class name="account"><property name="email address"/><element type="mail folder"/></class><class-extension extends="account"><property name="name"/></class-extension><class name="message"/><command name="open"/></suite></dictionary>')
        self.assertEqual(summary["classes"]["account"]["properties"], ["email address", "name"])
        self.assertEqual(summary["classes"]["account"]["elements"], ["mail folder"])

    def test_native_probe_checks_bundle_and_read_only_permission_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / "Microsoft Outlook.app"
            (app / "Contents").mkdir(parents=True)
            (app / "Contents/Info.plist").write_bytes(plistlib.dumps(dict(CFBundleIdentifier="com.microsoft.Outlook", CFBundleShortVersionString="16.test")))
            calls = []
            def runner(command, **kwargs):
                calls.append(command)
                return subprocess.CompletedProcess(command, 0, '<dictionary><suite><class name="message"/></suite></dictionary>' if "sdef" in command[0] else "16.test\n", "")
            result = probe(True, app, runner, "Darwin")
            self.assertEqual(result["status"], "dictionary-inspected")
            self.assertFalse(result["mailbox_access_tested"])
            self.assertTrue(result["automation_permission_check"]["success"])
            self.assertEqual(calls[1][-1], 'tell application id "com.microsoft.Outlook" to return version')
            self.assertEqual(result["read_adapter_candidates"], ["message"])

    def test_missing_dictionary_and_permission_failure_remain_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / "Microsoft Outlook.app"
            (app / "Contents").mkdir(parents=True)
            (app / "Contents/Info.plist").write_bytes(plistlib.dumps(dict(CFBundleIdentifier="com.microsoft.Outlook")))
            def runner(command, **kwargs):
                return subprocess.CompletedProcess(command, 1, "", "permission denied")
            result = probe(True, app, runner, "Darwin")
            self.assertEqual(result["status"], "scripting-unavailable")
            self.assertFalse(result["automation_permission_check"]["success"])
            self.assertNotIn("permission denied", json.dumps(result))

    def test_portable_initialisation_preserves_existing_user_data(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ):
            os.environ.pop("OUTLOOK_AUTOMATION_PROFILE", None)
            root = Path(tmp)
            value = initialise("Mac pilot", "pilot@example.com", root)
            self.assertEqual(value["access"], "outlook-macos-pilot")
            before = (root / ".local/monitor/profile.json").read_bytes()
            with self.assertRaisesRegex(ValueError, "preserved"):
                initialise("Other", "other@example.com", root)
            self.assertEqual(before, (root / ".local/monitor/profile.json").read_bytes())

    def test_manifest_detects_modified_package_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "PACKAGE_MANIFEST.json").write_text(json.dumps(dict(files={"README.md": "bad hash"})))
            (root / "README.md").write_text("Changed")
            with self.assertRaisesRegex(ValueError, "changed"):
                verify_package(root)
