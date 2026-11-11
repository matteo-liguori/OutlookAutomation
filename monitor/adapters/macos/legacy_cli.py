"""Inspect Outlook for Mac's installed scripting interface before choosing an adapter."""

import argparse
import json
from pathlib import Path
import platform
import plistlib
import subprocess
import sys
import xml.etree.ElementTree as ET


def dictionary_summary(xml):
    tree = ET.fromstring(xml)
    classes = {}
    commands = set()
    for node in tree.iter():
        if node.tag in {"class", "class-extension"}:
            name = node.get("name") or node.get("extends")
            if name:
                entry = classes.setdefault(name, {"properties": set(), "elements": set()})
                entry["properties"].update(p.get("name") for p in node.findall("property") if p.get("name"))
                entry["elements"].update(p.get("type") for p in node.findall("element") if p.get("type"))
        if node.tag == "command" and node.get("name"):
            commands.add(node.get("name"))
    return dict(classes={name: {k: sorted(v) for k, v in fields.items()} for name, fields in sorted(classes.items())}, commands=sorted(commands))


def probe(check_permission=False, app_path=None, runner=subprocess.run, system=None):
    if (system or platform.system()) != "Darwin":
        return dict(native_macos=False, status="requires-macos", reason="Run this probe on the borrowed Mac; Windows cannot establish native Outlook compatibility.")
    candidates = [Path(app_path)] if app_path else [Path("/Applications/Microsoft Outlook.app"), Path.home() / "Applications/Microsoft Outlook.app"]
    app = next((p for p in candidates if p.is_dir()), None)
    if app is None:
        return dict(native_macos=True, status="outlook-not-found", reason="Locate or install Outlook for Mac, then pass --app with its application path.")
    with (app / "Contents/Info.plist").open("rb") as handle:
        info = plistlib.load(handle)
    bundle = info.get("CFBundleIdentifier")
    if bundle != "com.microsoft.Outlook":
        raise ValueError("Selected application is not Microsoft Outlook")
    result = dict(native_macos=True, app=str(app), version=info.get("CFBundleShortVersionString"), bundle_id=bundle, mailbox_access_tested=False)
    dictionary_xml = None
    try:
        dictionary = runner(["/usr/bin/sdef", str(app)], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        dictionary = None
    if dictionary is not None and dictionary.returncode == 0:
        dictionary_xml = dictionary.stdout
        result["dictionary_source"] = "sdef"
    else:
        # Command Line Tools alone may not provide sdef. Read only the dictionary
        # declared by this verified application, never a path outside its bundle.
        name = info.get("OSAScriptingDefinition")
        resources = (app / "Contents/Resources").resolve()
        if isinstance(name, str) and Path(name).name == name and name.endswith(".sdef"):
            bundled = resources / name
            if bundled.resolve().is_relative_to(app.resolve()) and not bundled.is_symlink() and bundled.is_file():
                dictionary_xml = bundled.read_text(encoding="utf-8")
                result["dictionary_source"] = "bundled-sdef"
                result["dictionary_tool_warning"] = "sdef could not run; inspected Outlook's declared bundled dictionary instead. Runtime mail/calendar support remains untested."
    if dictionary_xml is None:
        result.update(status="scripting-unavailable", reason="The installed Outlook did not expose a readable AppleScript dictionary; inspect its supported integrations on this Mac.")
    else:
        try:
            summary = dictionary_summary(dictionary_xml)
        except ET.ParseError:
            result.update(status="scripting-unavailable", reason="Outlook returned an unreadable scripting dictionary.")
        else:
            result.update(status="dictionary-inspected", dictionary=summary)
            result["read_adapter_candidates"] = [name for name in summary["classes"] if any(word in name.lower() for word in ("account", "mail", "message", "calendar", "event"))]
    if check_permission:
        # Version read only; the local user controls any macOS Automation prompt.
        permission = runner(["/usr/bin/osascript", "-e", 'tell application id "com.microsoft.Outlook" to return version'], capture_output=True, text=True, timeout=30)
        result["automation_permission_check"] = dict(success=permission.returncode == 0, version=permission.stdout.strip() if permission.returncode == 0 else None, reason=None if permission.returncode == 0 else "Outlook version read failed; inspect the local Automation permission or app state.")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["probe"])
    parser.add_argument("--app")
    parser.add_argument("--check-permission", action="store_true")
    args = parser.parse_args()
    try:
        result = probe(args.check_permission, args.app)
        print(json.dumps(result, indent=2))
        raise SystemExit(0 if result["status"] == "dictionary-inspected" else 2)
    except (ValueError, OSError, subprocess.TimeoutExpired) as error:
        print("Outlook capability check failed:", error, file=sys.stderr)
        raise SystemExit(1)
