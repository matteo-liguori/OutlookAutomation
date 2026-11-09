"""Portable harness setup and native-platform smoke tests for the Mac pilot."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import venv
from automation_paths import data_root, configure_data

ROOT = Path(__file__).resolve().parent


def verify_package(root=ROOT):
    root = Path(root).resolve()
    receipt = root / "PACKAGE_MANIFEST.json"
    if not receipt.is_file():
        return "source checkout"
    manifest = json.loads(receipt.read_text(encoding="utf-8"))
    for name, expected in manifest["files"].items():
        file = root / name
        if file.is_symlink() or not file.resolve().is_relative_to(root):
            raise ValueError("Unexpected package path: " + name)
        if hashlib.sha256(file.read_bytes()).hexdigest() != expected:
            raise ValueError("Package file changed: " + name)
    return "manifest verified"


def python_path(root=ROOT):
    return Path(root) / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def initialise(name, store, root=ROOT, data_dir=None):
    root = Path(root)
    if not name.strip() or store.count("@") != 1 or any(c.isspace() for c in store):
        raise ValueError("Supply your name and exact work email address")
    if data_dir and any((data_root(root)/'monitor'/name).exists() for name in ('profile.json','monitor.db')):
        raise ValueError('Existing profile/database preserved; use manage_data.py to relocate it')
    folder = (Path(data_dir).resolve() if data_dir else data_root(root)) / 'monitor'
    target = folder / "profile.json"
    if target.exists() or (folder / "monitor.db").exists():
        raise ValueError("Existing profile/database preserved; use a fresh folder for a new user")
    if os.environ.get("OUTLOOK_AUTOMATION_PROFILE"):
        raise ValueError("Clear OUTLOOK_AUTOMATION_PROFILE before first-run setup")
    if data_dir:
        configure_data(data_dir, root)
    folder.mkdir(parents=True, exist_ok=True)
    profile = dict(schema_version=1, name=name, timezone="Europe/London", mail={"mail:work": store}, calendars=["calendar:work"], access="outlook-macos-pilot")
    with target.open("x", encoding="utf-8") as output:
        json.dump(profile, output, indent=2)
        output.write("\n")
    return profile


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name")
    parser.add_argument("--work-store")
    parser.add_argument("--smoke-test", action="store_true")
    parser.add_argument('--data-dir', help='Dedicated private data directory; reusable code stays in this checkout')
    args = parser.parse_args(argv)
    if sys.version_info < (3, 12):
        parser.error("Python 3.12 or later is required")
    if bool(args.name) != bool(args.work_store):
        parser.error("Supply --name and --work-store together")
    print("Package:", verify_package(), flush=True)
    python = python_path()
    if not python.is_file():
        venv.EnvBuilder(with_pip=True).create(ROOT / ".venv")
    subprocess.run([str(python), "-m", "pip", "install", "--disable-pip-version-check", "-r", str(ROOT / "requirements-mac.lock")], check=True)
    if args.name:
        initialise(args.name, args.work_store, data_dir=args.data_dir)
        subprocess.run([str(python), "-X", "utf8", "-m", "monitor", "init"], cwd=ROOT, check=True)
        subprocess.run([str(python), "-X", "utf8", "-m", "monitor", "doctor"], cwd=ROOT, check=True)
    if args.smoke_test:
        env = dict(os.environ, OUTLOOK_AUTOMATION_PROFILE=str(ROOT / "profile.example.json"))
        # These tests exercise the portable harness with synthetic data; platform
        # specific PowerShell fixtures are a separate Windows check.
        patterns = ["test_monitor.py", "test_cards.py", "test_followups.py", "test_approvals.py", "test_threadcache.py", "test_mac_support.py", "test_mac_probe_fallback.py", "test_mac_native.py", "test_mac_test_actions.py", "test_mac_calendar.py", "test_mac_collector.py", "test_mac_freshness.py", "test_mac_operations.py", "test_platform_commands.py", "test_reviews.py", "test_guidance.py", "test_data_separation.py", "test_profile_backups.py"]
        for pattern in patterns:
            subprocess.run([str(python), "-X", "utf8", "-m", "unittest", "discover", "-s", "tests", "-p", pattern], cwd=ROOT, env=env, check=True)
        print(json.dumps(dict(smoke_test="passed", system=platform.system(), machine=platform.machine(), python=platform.python_version(), native_macos=platform.system() == "Darwin", mailbox_access_tested=False)))
    print("Harness ready. Native Outlook capability check: .venv/bin/python mac_outlook.py probe", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print("Setup failed:", error, file=sys.stderr)
        raise SystemExit(1)
