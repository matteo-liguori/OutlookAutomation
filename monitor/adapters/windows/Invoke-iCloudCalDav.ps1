[CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'High')]
param(
    [Parameter(Mandatory)]
    [ValidateSet('Probe', 'Calendars', 'Events', 'Create', 'Update', 'Delete')]
    [string]$Action,

    [string]$Calendar,
    [string]$Uid,
    [string]$Title,
    [string]$Location,
    [string]$Description,
    [datetimeoffset]$Start,
    [datetimeoffset]$End,
    [switch]$AllDay,
    [switch]$AllowDuplicate,
    [string]$CredentialPath,
    [ValidateRange(5, 120)]
    [int]$TimeoutSeconds = 30,
    [ValidateSet('Json', 'Text')]
    [string]$Output = 'Json'
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot/AutomationEnvironment.Common.ps1"
$workspaceCredentialPath = Join-Path (Get-OutlookAutomationDataRoot) 'icloud-caldav.json'
$legacyCredentialPath = Join-Path $env:LOCALAPPDATA 'Codex\OutlookAutomation\icloud-caldav.json'
if ($CredentialPath) {
    $credentialPath = [System.IO.Path]::GetFullPath($CredentialPath)
}
elseif (Test-Path -LiteralPath $workspaceCredentialPath) {
    $credentialPath = $workspaceCredentialPath
}
else {
    $credentialPath = $legacyCredentialPath
}
. "$PSScriptRoot/AutomationEnvironment.Common.ps1"
$python = Get-AutomationPython -RequiredModules @('caldav', 'icalendar')
$helper = Join-Path $script:ProjectRoot 'monitor/integrations/icloud.py'

if (-not (Test-Path -LiteralPath $credentialPath)) {
    throw "No iCloud CalDAV credential is configured at '$credentialPath'. Run .\Set-iCloudCalDavCredential.ps1 first."
}
if (-not (Test-Path -LiteralPath $python)) {
    throw "The project Python environment is missing. Run .\Setup-OutlookAutomation.ps1 first."
}
if (-not (Test-Path -LiteralPath $helper)) {
    throw "Missing helper: $helper"
}

$record = Get-Content -LiteralPath $credentialPath -Raw | ConvertFrom-Json
if ($record.schemaVersion -notin @(1, 2) -or -not $record.appleAccount -or -not $record.encryptedPassword) {
    throw 'The saved iCloud CalDAV credential has an unsupported or incomplete format.'
}

if ($record.schemaVersion -eq 2) {
    if ($record.protectionScope -ne 'LocalMachine') {
        throw 'The saved iCloud CalDAV credential has an unsupported protection scope.'
    }
    $entropy = [Text.Encoding]::UTF8.GetBytes('OutlookAutomation-iCloud-CalDAV-v2')
    $protectedBytes = [Convert]::FromBase64String([string]$record.encryptedPassword)
    $passwordBytes = [Security.Cryptography.ProtectedData]::Unprotect(
        $protectedBytes,
        $entropy,
        [Security.Cryptography.DataProtectionScope]::LocalMachine
    )
    $plainPassword = [Text.Encoding]::UTF8.GetString($passwordBytes)
    [Array]::Clear($protectedBytes, 0, $protectedBytes.Length)
    [Array]::Clear($passwordBytes, 0, $passwordBytes.Length)
    $securePassword = $null
}
else {
    $securePassword = $record.encryptedPassword | ConvertTo-SecureString
    $plainPassword = [System.Net.NetworkCredential]::new('', $securePassword).Password
}
$credentialJson = @{ username = [string]$record.appleAccount
    password = $plainPassword
} | ConvertTo-Json -Compress

$arguments = @(
    $helper,
    '--action', $Action.ToLowerInvariant(),
    '--timeout', [string]$TimeoutSeconds
)
if ($Output -eq 'Text') {
    $arguments += '--pretty'
}

if ($Action -in @('Events', 'Create')) {
    if (-not $PSBoundParameters.ContainsKey('Start') -or -not $PSBoundParameters.ContainsKey('End')) {
        throw 'Events requires both -Start and -End.'
    }
    if ($End -le $Start) {
        throw '-End must be later than -Start.'
    }
    if (($End - $Start).TotalDays -gt 31) {
        throw 'The event window is limited to 31 days per request.'
    }
    $arguments += @('--start', $Start.ToString('o'), '--end', $End.ToString('o'))
    if ($Calendar) {
        $arguments += @('--calendar', $Calendar)
    }
}

if ($Action -in @('Create', 'Update', 'Delete')) {
    if (-not $Calendar) {
        throw "$Action requires -Calendar."
    }
    if ($Action -in @('Update', 'Delete') -and -not $Uid) {
        throw "$Action requires -Uid."
    }
    if ($Action -eq 'Create' -and -not $Title) {
        throw 'Create requires -Title.'
    }
    if ($Action -in @('Update', 'Delete')) {
        $arguments += @('--calendar', $Calendar)
    }
    if ($Uid) {
        $arguments += @('--uid', $Uid)
    }
    if ($Title) {
        $arguments += @('--title', $Title)
    }
    if ($PSBoundParameters.ContainsKey('Location')) {
        $arguments += @('--location', $Location)
    }
    if ($PSBoundParameters.ContainsKey('Description')) {
        $arguments += @('--description', $Description)
    }
    if ($AllDay) {
        $arguments += '--all-day'
    }
    if ($AllowDuplicate) {
        $arguments += '--allow-duplicate'
    }

    $target = if ($Uid) {
        "$Calendar / $Uid"
    }
    else {
        "$Calendar / $Title"
    }
    if (-not $PSCmdlet.ShouldProcess($target, "$Action iCloud calendar event")) {
        return
    }
}

try {
    $credentialJson | & $python @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "The iCloud CalDAV helper failed with exit code $LASTEXITCODE."
    }
}
finally {
    $plainPassword = $null
    $credentialJson = $null
    if ($securePassword) {
        $securePassword.Dispose()
    }
}
