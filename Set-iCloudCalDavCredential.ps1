[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$AppleAccount = '',
    [string]$CredentialPath,
    [switch]$PasswordFromClipboard,
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot/AutomationEnvironment.Common.ps1"
$defaultCredentialPath = Join-Path (Get-OutlookAutomationDataRoot) 'icloud-caldav.json'
if (-not $CredentialPath) {
    $CredentialPath = $defaultCredentialPath
}
$credentialPath = [System.IO.Path]::GetFullPath($CredentialPath)
$credentialDirectory = Split-Path -Parent $credentialPath

if ($Remove) {
    if ((Test-Path -LiteralPath $credentialPath) -and $PSCmdlet.ShouldProcess($credentialPath, 'Remove the saved iCloud CalDAV credential')) {
        Remove-Item -LiteralPath $credentialPath -Force
        Write-Host 'Removed the saved iCloud CalDAV credential.'
    }
    return
}

if (-not $AppleAccount) { throw 'Supply -AppleAccount with your Apple account address.' }
Write-Host 'Generate an app-specific password at https://account.apple.com/account/manage'
Write-Host 'Use an identifying label such as Codex OutlookAutomation.'
$plainPassword = $null
if ($PasswordFromClipboard) {
    $plainPassword = (Get-Clipboard -Raw).Trim()
    if ($plainPassword -notmatch '^[A-Za-z0-9]{4}(?:-[A-Za-z0-9]{4}){3}$') {
        throw 'The clipboard does not contain an Apple app-specific password in the expected format.'
    }
    $securePassword = ConvertTo-SecureString $plainPassword -AsPlainText -Force
}
else {
    $securePassword = Read-Host 'Paste the Apple app-specific password' -AsSecureString
}
if ($securePassword.Length -eq 0) {
    throw 'No app-specific password was entered.'
}

try {
    if (-not $PSCmdlet.ShouldProcess($credentialPath, "Save a Windows-machine-encrypted CalDAV credential with a restricted ACL for $AppleAccount")) {
        return
    }

    New-Item -ItemType Directory -Path $credentialDirectory -Force | Out-Null
    $entropy = [Text.Encoding]::UTF8.GetBytes('OutlookAutomation-iCloud-CalDAV-v2')
    if ($null -eq $plainPassword) {
        $plainPassword = [Net.NetworkCredential]::new('', $securePassword).Password
    }
    $passwordBytes = [Text.Encoding]::UTF8.GetBytes($plainPassword)
    $protectedBytes = [Security.Cryptography.ProtectedData]::Protect(
        $passwordBytes,
        $entropy,
        [Security.Cryptography.DataProtectionScope]::LocalMachine
    )
    $record = [ordered]@{
        schemaVersion = 2
        appleAccount = $AppleAccount
        protectionScope = 'LocalMachine'
        encryptedPassword = [Convert]::ToBase64String($protectedBytes)
        createdAt = [DateTimeOffset]::Now.ToString('o')
    }
    if (Test-Path -LiteralPath $credentialPath) {
        Remove-Item -LiteralPath $credentialPath -Force
    }
    $record | ConvertTo-Json | Set-Content -LiteralPath $credentialPath -Encoding utf8
    $plainPassword = $null
    [Array]::Clear($passwordBytes, 0, $passwordBytes.Length)
    [Array]::Clear($protectedBytes, 0, $protectedBytes.Length)

    $workspaceLocalDirectory = Get-OutlookAutomationDataRoot
    $isWorkspaceCredential = $credentialPath.StartsWith(
        $workspaceLocalDirectory + [System.IO.Path]::DirectorySeparatorChar,
        [System.StringComparison]::OrdinalIgnoreCase
    )
    if ($isWorkspaceCredential) {
        # LocalMachine DPAPI permits the restricted automation identity to decrypt,
        # while this explicit ACL limits who can read the encrypted blob.
        $currentSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
        $systemSid = [System.Security.Principal.SecurityIdentifier]::new('S-1-5-18')
        $administratorsSid = [System.Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')
        $sandboxSid = ([System.Security.Principal.NTAccount]::new($env:COMPUTERNAME, 'CodexSandboxUsers')).Translate(
            [System.Security.Principal.SecurityIdentifier]
        )
        $acl = New-Object System.Security.AccessControl.FileSecurity
        $acl.SetOwner($currentSid)
        $acl.SetAccessRuleProtection($true, $false)
        foreach ($entry in @(
                @($currentSid, [System.Security.AccessControl.FileSystemRights]::FullControl),
                @($systemSid, [System.Security.AccessControl.FileSystemRights]::FullControl),
                @($administratorsSid, [System.Security.AccessControl.FileSystemRights]::FullControl),
                @($sandboxSid, [System.Security.AccessControl.FileSystemRights]::ReadAndExecute)
            )) {
            $acl.AddAccessRule([System.Security.AccessControl.FileSystemAccessRule]::new(
                    $entry[0],
                    $entry[1],
                    [System.Security.AccessControl.AccessControlType]::Allow
                ))
        }
        Set-Acl -LiteralPath $credentialPath -AclObject $acl
    }
    else {
        # Outside the managed workspace, remove inherited access and grant only the
        # current Windows user full control. Workspace credentials must retain the
        # workspace ACL so restricted automation processes can read the DPAPI blob.
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
            Set-Acl -LiteralPath $credentialPath -AclObject $acl
        }
        catch {
            & icacls.exe $credentialPath /inheritance:r /grant:r "${env:USERNAME}:(F)" | Out-Null
            if ($LASTEXITCODE -ne 0) {
                throw
            }
        }
    }

    Write-Host "Saved the encrypted credential at $credentialPath"
    Write-Host 'The password is protected with Windows machine-scoped DPAPI and a restricted file ACL.'


}
finally {
    $plainPassword = $null
    if ($passwordBytes) {
        [Array]::Clear($passwordBytes, 0, $passwordBytes.Length)
    }
    if ($protectedBytes) {
        [Array]::Clear($protectedBytes, 0, $protectedBytes.Length)
    }
    if ($securePassword) {
        $securePassword.Dispose()
    }
}

