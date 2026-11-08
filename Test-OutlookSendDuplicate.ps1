param(
    [string]$Store = '*',
    [Parameter(Mandatory = $true)][string]$Subject,
    [string]$Body = '', [string[]]$Recipients = @(),
    [ValidateRange(1, 3650)][int]$LookbackDays = 30,
    [ValidateRange(1, 5000)][int]$MaxScan = 500,
    [ValidateRange(1, 5000)][int]$Limit = 50
)
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot/OutlookAutomation.Common.ps1"
$result = Get-OutlookTransportScan -Namespace (Get-OutlookAutomationNamespace) -Store $Store -Subject $Subject -Body $Body -Recipients $Recipients -LookbackDays $LookbackDays -MaxScan $MaxScan
if ($result.Rows.Count -gt $Limit) {
    $result.Complete = $false
    $result.Errors += 'Match output limit reached; increase the bounded Limit to inspect every match.'
}
$result.Rows = @($result.Rows | Select-Object -First $Limit)
$result | ConvertTo-Json -Depth 5
if (-not $result.Complete) {
    throw 'Duplicate review incomplete; no clean-send conclusion is available.'
}
