param(
    [ValidateSet('Accounts', 'Stores')]
    [string]$Action = 'Accounts',

    [ValidateSet('Json', 'Table', 'List', 'Csv')]
    [string]$Output = 'Json'
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\OutlookAutomation.Common.ps1"

$namespace = Get-OutlookAutomationNamespace
$rows = @()

if ($Action -eq 'Accounts') {
    foreach ($account in $namespace.Accounts) {
        $rows += [pscustomobject]@{
            DisplayName = [string]$account.DisplayName
            SmtpAddress = [string]$account.SmtpAddress
            AccountType = [string]$account.AccountType
        }
    }
}
else {
    foreach ($store in $namespace.Stores) {
        $rows += [pscustomobject]@{
            DisplayName = [string]$store.DisplayName
            FilePath = [string]$store.FilePath
        }
    }
}

Write-OutlookRows $rows $Output 4
