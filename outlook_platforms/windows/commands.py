"""Preserve the existing Windows helpers and their PowerShell parameters."""
from pathlib import Path
import json
import platform
import shutil
import subprocess
import os

ROOT = Path(__file__).resolve().parents[2]
HELPERS = {name:name + '.ps1' for name in (
    'Inspect-Outlook', 'Read-OutlookMail', 'Get-OutlookMessage', 'Read-OutlookMonitorCalendar')}

# The JSON interface keeps argument parsing out of shell command strings.
JSON_HELPERS = {
    'accounts':('Inspect-Outlook.ps1',{},dict(Action='Accounts',Output='Json')),
    'stores':('Inspect-Outlook.ps1',{},dict(Action='Stores',Output='Json')),
    'probe':('Test-OutlookSession.ps1',{},dict(CheckCom=True)),
    'session':('Test-OutlookSession.ps1',{},dict(CheckCom=True)),
    'folders':('Read-OutlookMail.ps1',{},dict(Action='Folders',Output='Json')),
    'recent':('Read-OutlookMail.ps1',dict(folder='Folder',limit='Limit',max_scan='MaxScan',unread_only='UnreadOnly',include_preview='IncludePreview'),dict(Action='Recent',Output='Json')),
    'search':('Search-OutlookMailDetails.ps1',dict(query='Query',folder='Folder',limit='Limit',body_chars='BodyChars',max_scan='MaxScan'),{}),
    'read':('Get-OutlookMessage.ps1',dict(entry_id='EntryId',store_id='StoreId',folder='Folder',case_id='CaseId',force_refresh='ForceRefresh',download_large_attachments='DownloadLargeAttachments',max_scan='MaxScan',max_folders='MaxFolders'),dict(Output='Json')),
    'collect':('Collect-OutlookMonitor.ps1',dict(start='Since',end='Until',page_size='PageSize',max_scan='MaxScan',offset='Offset',snapshot_id='SnapshotId'),{}),
    'calendar':('Read-OutlookMonitorCalendar.ps1',dict(start='Start',end='End',limit='Limit'),{}),
    'refresh':('Start-OutlookSendReceive.ps1',dict(wait_seconds='WaitSeconds'),{}),
    'contacts':('Search-OutlookContacts.ps1',dict(query='Query',limit='Limit',max_items_per_folder='MaxItemsPerFolder',include_recipient_cache='IncludeRecipientCache'),{}),
    'duplicates':('Test-OutlookSendDuplicate.ps1',dict(subject='Subject',body='Body',recipients='Recipients',lookback_days='LookbackDays',max_scan='MaxScan',limit='Limit'),{}),
    'send':('Send-OutlookStoreMailSafe.ps1',dict(to='To',cc='Cc',bcc='Bcc',subject='Subject',body='Body',attachments='Attachments',high_importance='HighImportance',reply_to_entry_id='ReplyToEntryId'),dict(BatchSubmitOnly=True)),
}


def execute_json(operation,payload,store,h=None,runner=subprocess.run):
    helper,mapping,fixed=JSON_HELPERS[operation]
    values=dict(payload);values.pop('approval_id',None)
    if set(values)-set(mapping):raise ValueError('Unsupported '+operation+' field: '+', '.join(sorted(set(values)-set(mapping))))
    parameters={**fixed,**{mapping[k]:v for k,v in values.items()}}
    if operation not in {'accounts','stores','probe','session','refresh','calendar'}:parameters['Store']=store
    shell=shutil.which('pwsh') or shutil.which('powershell.exe') or shutil.which('powershell')
    if not shell:raise ValueError('PowerShell runtime unavailable')
    env=dict(os.environ)
    if h:env['OUTLOOK_AUTOMATION_DATABASE']=str(h.path.resolve())
    try:
        result=runner([shell,'-NoLogo','-NoProfile','-NonInteractive','-File',str(Path(__file__).with_name('invoke.ps1'))],
                      input=json.dumps(dict(helper=helper,parameters=parameters)),capture_output=True,text=True,encoding='utf-8-sig',
                      env=env,timeout=650 if operation=='refresh' else 180)
    except subprocess.TimeoutExpired:
        raise ValueError('Outlook helper timed out; reconcile transport before retrying any send') from None
    if result.returncode:
        raise ValueError('Outlook helper failed; inspect its recorded state before retrying: '+result.stderr.strip()[-1000:])
    try:data=json.loads(result.stdout)
    except ValueError:raise ValueError('Outlook helper returned invalid JSON; do not infer completion') from None
    if operation=='accounts':data=[r for r in (data if isinstance(data,list) else [data]) if r.get('SmtpAddress','').casefold()==store.casefold()]
    if operation=='stores':data=[r for r in (data if isinstance(data,list) else [data]) if r.get('DisplayName','').casefold()==store.casefold()]
    return dict(data=data)


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
