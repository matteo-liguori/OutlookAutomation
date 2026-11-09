"""Preserve the existing Windows helpers and their PowerShell parameters."""
from pathlib import Path
import json
import platform
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]
HELPERS = {name:name + '.ps1' for name in (
    'Inspect-Outlook', 'Read-OutlookMail', 'Get-OutlookMessage', 'Read-OutlookMonitorCalendar', 'Collect-OutlookMonitor','Test-OutlookMailboxFreshness','Save-OutlookStoreDraft','Update-OutlookStoreDraft','Send-OutlookStoreMail')}


def command_line(command, arguments, powershell):
    if command not in HELPERS:
        raise ValueError('Command is not implemented for Windows')
    return [powershell, '-NoLogo', '-NoProfile', '-NonInteractive', '-File', str(ROOT / HELPERS[command]), *arguments]


def main(command, arguments):
    if platform.system() != 'Windows':
        print(json.dumps(dict(status='blocked', code='windows-helper-requires-windows')))
        return 2
    powershell = shutil.which('pwsh') or shutil.which('powershell')
    if not powershell or command not in HELPERS:
        print(json.dumps(dict(status='blocked', code='windows-command-unavailable')))
        return 2
    return subprocess.run(command_line(command, arguments, powershell), check=False).returncode
