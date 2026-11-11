"""Synthetic regressions for Macs with Command Line Tools but no full Xcode."""
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest

from monitor.adapters.macos.legacy_cli import probe


class BundledDictionaryTests(unittest.TestCase):
    def app(self, root, name="Outlook.sdef", xml=None):
        app = Path(root) / "Microsoft Outlook.app"
        resources = app / "Contents/Resources"
        resources.mkdir(parents=True)
        (app / "Contents/Info.plist").write_bytes(plistlib.dumps({
            "CFBundleIdentifier": "com.microsoft.Outlook",
            "CFBundleShortVersionString": "16.test",
            "OSAScriptingDefinition": name,
        }))
        if xml is not None:
            (resources / "Outlook.sdef").write_text(xml)
        return app

    @staticmethod
    def unavailable(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, "", "sdef requires Xcode")

    def test_bundled_dictionary_is_declaration_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self.app(tmp, xml='<dictionary><suite><class name="message"/><class name="calendar event"/></suite></dictionary>')
            result = probe(app_path=app, runner=self.unavailable, system="Darwin")
            self.assertEqual(result["status"], "dictionary-inspected")
            self.assertEqual(result["dictionary_source"], "bundled-sdef")
            self.assertEqual(result["read_adapter_candidates"], ["calendar event", "message"])
            self.assertFalse(result["mailbox_access_tested"])
            self.assertNotIn("automation_permission_check", result)

    def test_missing_tool_uses_bundled_dictionary(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self.app(tmp, xml="<dictionary/>")
            def missing(command, **kwargs):
                raise FileNotFoundError("sdef")
            self.assertEqual(probe(app_path=app, runner=missing, system="Darwin")["dictionary_source"], "bundled-sdef")

    def test_tool_timeout_uses_bundled_dictionary(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self.app(tmp, xml="<dictionary/>")
            def timeout(command, **kwargs):
                raise subprocess.TimeoutExpired(command, 30)
            self.assertEqual(probe(app_path=app, runner=timeout, system="Darwin")["dictionary_source"], "bundled-sdef")

    def test_missing_or_malformed_dictionary_stays_explicit(self):
        for xml in (None, "not XML"):
            with self.subTest(xml=xml), tempfile.TemporaryDirectory() as tmp:
                app = self.app(tmp, xml=xml)
                self.assertEqual(probe(app_path=app, runner=self.unavailable, system="Darwin")["status"], "scripting-unavailable")

    def test_path_traversal_and_absolute_paths_are_rejected(self):
        for name in ("../outside.sdef", "/tmp/outside.sdef"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                app = self.app(tmp, name=name)
                self.assertEqual(probe(app_path=app, runner=self.unavailable, system="Darwin")["status"], "scripting-unavailable")

    def test_symlink_to_other_file_is_not_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self.app(tmp)
            outside = Path(tmp) / "outside.sdef"
            outside.write_text("<dictionary/>")
            try:
                (app / "Contents/Resources/Outlook.sdef").symlink_to(outside)
            except OSError as error:
                if getattr(error,'winerror',None)==1314:self.skipTest('Host denies symlink creation; native macOS test remains enabled')
                raise
            self.assertEqual(probe(app_path=app, runner=self.unavailable, system="Darwin")["status"], "scripting-unavailable")

    def test_resources_symlink_cannot_escape_app(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self.app(tmp)
            resources = app / "Contents/Resources"
            resources.rmdir()
            outside = Path(tmp) / "other-resources"
            outside.mkdir()
            (outside / "Outlook.sdef").write_text("<dictionary/>")
            try:
                resources.symlink_to(outside, target_is_directory=True)
            except OSError as error:
                if getattr(error,'winerror',None)==1314:self.skipTest('Host denies symlink creation; native macOS test remains enabled')
                raise
            self.assertEqual(probe(app_path=app, runner=self.unavailable, system="Darwin")["status"], "scripting-unavailable")
