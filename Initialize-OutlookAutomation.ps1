[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Name,
    [Parameter(Mandatory)][string]$WorkStore,
    [string]$PersonalStore,
    [string]$SharedStore,
    [switch]$GooglePrimary,
    [string]$GoogleSharedCalendarId,
    [switch]$ICloud,
    [string]$DataDirectory
)
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot/AutomationEnvironment.Common.ps1"
if ($DataDirectory) {
    if ($env:OUTLOOK_AUTOMATION_DATA -and [IO.Path]::GetFullPath($env:OUTLOOK_AUTOMATION_DATA) -ne [IO.Path]::GetFullPath($DataDirectory)) { throw 'Clear the data-directory environment override before configuring another location.' }
    if (-not [System.IO.Path]::IsPathRooted($DataDirectory)) { throw 'Use an absolute data directory.' }
    $DataDirectory = [System.IO.Path]::GetFullPath($DataDirectory)
    if ($DataDirectory -eq $PSScriptRoot -or $PSScriptRoot.StartsWith($DataDirectory + [System.IO.Path]::DirectorySeparatorChar)) { throw 'Data must not contain the source checkout.' }
    if ($DataDirectory.StartsWith($PSScriptRoot + [System.IO.Path]::DirectorySeparatorChar) -and $DataDirectory -ne (Join-Path $PSScriptRoot '.local')) { throw 'Use an external data directory or the ignored .local directory.' }
    if ((Test-Path -LiteralPath (Join-Path $PSScriptRoot '.local/monitor/profile.json')) -or (Test-Path -LiteralPath (Join-Path $PSScriptRoot '.local/data-root.txt'))) { throw 'Existing data preserved; use manage_data.py to relocate it.' }
}
$directory = Join-Path $(if ($DataDirectory) {$DataDirectory} else {Get-OutlookAutomationDataRoot}) 'monitor'
$path = Join-Path $directory 'profile.json'
$database = Join-Path $directory 'monitor.db'
if ((Test-Path -LiteralPath $path) -or (Test-Path -LiteralPath $database)) {
    throw 'This installation already has a profile or database. Use a fresh folder for a new user.'
}
if ($env:OUTLOOK_AUTOMATION_PROFILE) { throw 'Clear OUTLOOK_AUTOMATION_PROFILE for first-run setup.' }
$mail = [ordered]@{'mail:work'=$WorkStore}
if ($PersonalStore) { $mail['mail:personal']=$PersonalStore }
if ($SharedStore) { $mail['mail:shared']=$SharedStore }
foreach ($address in $mail.Values) {
    if ($address -notmatch '^[^@\s]+@[^@\s]+\.[^@\s]+$' -or $address.EndsWith('.invalid')) {
        throw 'Use the exact SMTP address of each configured Classic Outlook store.'
    }
}
if (@($mail.Values | ForEach-Object {$_.ToLowerInvariant()} | Select-Object -Unique).Count -ne $mail.Count) {
    throw 'Each role needs a distinct mailbox.'
}
if (-not $Name.Trim()) { throw 'Supply your name.' }
$python = Get-AutomationPython
& $python -c "from zoneinfo import ZoneInfo; ZoneInfo('Europe/London')"
if ($LASTEXITCODE -ne 0) { throw 'Install the locked dependencies with Setup-OutlookAutomation.ps1 first.' }
$calendars = @('calendar:work')
$google = [ordered]@{}
if ($GooglePrimary) { $calendars += 'calendar:primary'; $google['calendar:primary']='primary' }
if ($GoogleSharedCalendarId) {
    if ($GoogleSharedCalendarId -eq 'primary') { throw 'Use the shared Google calendar ID.' }
    $calendars += 'calendar:shared'; $google['calendar:shared']=$GoogleSharedCalendarId
}
if ($ICloud) { $calendars += 'calendar:icloud' }
New-Item -ItemType Directory -Path $directory -Force | Out-Null
if ($DataDirectory) {
    New-Item -ItemType Directory -Path (Join-Path $PSScriptRoot '.local') -Force | Out-Null
    $DataDirectory | Set-Content -LiteralPath (Join-Path $PSScriptRoot '.local/data-root.txt') -Encoding utf8
}
@{schema_version=1;name=$Name;timezone='Europe/London';platform=@{os='windows';outlook_backend='com';bindings=@{}};mail=$mail;calendars=$calendars} |
    ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $path -Encoding utf8
if ($google.Count) {
    $google | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $directory 'google-calendar-config.json') -Encoding utf8
}
Push-Location $PSScriptRoot
try {
    & $python -X utf8 -m monitor init
    if ($LASTEXITCODE -ne 0) { throw 'Harness initialisation failed; review the local profile before retrying.' }
} finally { Pop-Location }
Write-Host 'Fresh profile and empty harness ready. Follow README.md for the first supervised pass.'
