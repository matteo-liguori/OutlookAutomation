# Private iCloud CalDAV

Keep instructions brief and positive; state the desired behaviour, give each rule
one home and link to it elsewhere.

Access private calendars directly through CalDAV.

## Credentials

Generate an app-specific password at <https://account.apple.com/account/manage>,
label Codex OutlookAutomation. Enter it through `Set-iCloudCalDavCredential.ps1 -AppleAccount you@example.com`'s
secure prompt, or clipboard with -PasswordFromClipboard -Confirm:$false.
Keep credentials in these secure input channels.

Machine-DPAPI storage: Git-ignored `.local\icloud-caldav.json`, restricted to creator,
system/admins/CodexSandboxUsers. Legacy `%LOCALAPPDATA%\Codex\OutlookAutomation`
is a fallback; Migrate-iCloudCalDavCredential.ps1 -Confirm:$false reuses a working
credential from normal PowerShell.

## Operations

```powershell
.\Invoke-iCloudCalDav.ps1 -Action Probe -Output Text
.\Invoke-iCloudCalDav.ps1 -Action Calendars -Output Text
.\Invoke-iCloudCalDav.ps1 -Action Events -Calendar 'Your calendar' `
  -Start '2026-10-26T00:00:00+00:00' -End '2026-10-30T00:00:00+00:00' -Output Text
```

Reads: explicit offsets, maximum 31 days. Authorised writes: exact calendar,
-WhatIf/PowerShell confirmation, duplicate guard for Create, UID for Update/Delete;
recurring writes unsupported. Remove local credentials with
Set-iCloudCalDavCredential.ps1 -Remove; revoke separately in Apple settings.
