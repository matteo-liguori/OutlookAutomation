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

Reads use explicit offsets and a maximum of 31 days. Authorised writes use an exact
calendar and PowerShell confirmation. Create checks for duplicates. Update and
Delete need the UID.

Use `-Action AnnualMonth -Calendar NAME -Uid UID -KeepMonth 4 -EffectiveDate YYYY-MM-DD`
to preview keeping a single month each year from that date. It supports one-day
all-day series with a fixed day up to 28 and a simple monthly interval that
divides the year. It preserves past dates and the UID. After checking the preview,
pass its revision as `-ExpectedRevision` to apply and verify the conditional
update. Series with exceptions or invitation participants require another workflow.

Remove local credentials with
Set-iCloudCalDavCredential.ps1 -Remove; revoke separately in Apple settings.
