[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$SourcePath = (Join-Path $env:LOCALAPPDATA 'Codex\OutlookAutomation\icloud-caldav.json'),
    [string]$DestinationPath
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot/AutomationEnvironment.Common.ps1"
if (-not $DestinationPath) { $DestinationPath = Join-Path (Get-OutlookAutomationDataRoot) 'icloud-caldav.json' }
$source = [System.IO.Path]::GetFullPath($SourcePath)
$destination = [System.IO.Path]::GetFullPath($DestinationPath)

if (-not (Test-Path -LiteralPath $source)) {
    throw "The legacy iCloud CalDAV credential was not found at '$source'."
}

$record = Get-Content -LiteralPath $source -Raw | ConvertFrom-Json
if ($record.schemaVersion -ne 1 -or -not $record.appleAccount -or -not $record.encryptedPassword) {
    throw 'The legacy iCloud CalDAV credential has an unsupported or incomplete format.'
}

if (-not $PSCmdlet.ShouldProcess($destination, "Copy the existing Windows-user-encrypted iCloud CalDAV credential from '$source'")) {
    return
}

$directory = Split-Path -Parent $destination
New-Item -ItemType Directory -Path $directory -Force | Out-Null
Copy-Item -LiteralPath $source -Destination $destination -Force

$workspaceLocalDirectory = Get-OutlookAutomationDataRoot
$isWorkspaceCredential = $destination.StartsWith(
    $workspaceLocalDirectory + [System.IO.Path]::DirectorySeparatorChar,
    [System.StringComparison]::OrdinalIgnoreCase
)
if (-not $isWorkspaceCredential) {
    $currentSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
    $acl = New-Object System.Security.AccessControl.FileSecurity
    $acl.SetOwner($currentSid)
    $acl.SetAccessRuleProtection($true, $false)
    $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
        $currentSid,
        [System.Security.AccessControl.FileSystemRights]::FullControl,
        [System.Security.AccessControl.AccessControlType]::Allow
    )
    $acl.AddAccessRule($rule)
    try {
        Set-Acl -LiteralPath $destination -AclObject $acl
    }
    catch {
        & icacls.exe $destination /inheritance:r /grant:r "${env:USERNAME}:(F)" | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw
        }
    }
}

Write-Host "Copied the encrypted iCloud CalDAV credential to $destination"
Write-Host 'No plaintext password was read or written.'
