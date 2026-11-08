$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$fixture = Join-Path $root ('tmp/credential-test-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $fixture -Force | Out-Null
function Read-Host {
    param($Prompt, [switch]$AsSecureString)
    ConvertTo-SecureString 'fixture-password' -AsPlainText -Force
}
function Set-Acl {
    param($LiteralPath, $AclObject)
} # Never change ACLs in a regression test.
try {
    $path = Join-Path $fixture 'credential.json'
    & (Join-Path $root 'Set-iCloudCalDavCredential.ps1') -AppleAccount 'fixture@example.invalid' -CredentialPath $path -Confirm:$false | Out-Null
    $record = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
    $bytes = [Security.Cryptography.ProtectedData]::Unprotect([Convert]::FromBase64String($record.encryptedPassword), [Text.Encoding]::UTF8.GetBytes('OutlookAutomation-iCloud-CalDAV-v2'), [Security.Cryptography.DataProtectionScope]::LocalMachine)
    try {
        if ([Text.Encoding]::UTF8.GetString($bytes) -cne 'fixture-password') {
            throw 'Secure prompt did not encrypt its supplied value.'
        }
    }
    finally {
        [Array]::Clear($bytes, 0, $bytes.Length)
    }
    if ($record.protectionScope -ne 'LocalMachine') {
        throw 'Credential scope mismatch.'
    }
    Write-Output 'Synthetic secure-prompt encryption verified.'
}
finally {
    $absolute = [IO.Path]::GetFullPath($fixture)
    $allowed = [IO.Path]::GetFullPath((Join-Path $root 'tmp')).TrimEnd('\') + '\'
    if (-not $absolute.StartsWith($allowed, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Fixture cleanup escaped the test root.'
    }
    Remove-Item -LiteralPath $absolute -Recurse -Force
}
