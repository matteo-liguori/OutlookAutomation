param(
    [Parameter(Mandatory = $true)]
    [string]$Store,

    [Parameter(Mandatory = $true)]
    [string]$Subject,

    [string[]]$To = @(),
    [string[]]$Cc = @(),
    [string[]]$Bcc = @(),
    [string]$Body = '',
    [switch]$ExactSubject
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\OutlookAutomation.Common.ps1"

Assert-OutlookRecipients $To 'To'
Assert-OutlookRecipients $Cc 'Cc'
Assert-OutlookRecipients $Bcc 'Bcc'

$namespace = Get-OutlookAutomationNamespace
$storeItem = Get-OutlookStoreByName $namespace $Store
$drafts = Find-OutlookFolder $storeItem 'Drafts'
$items = $drafts.Items
$items.Sort('[LastModificationTime]', $true)
$match = $null

for ($i = 1; $i -le $items.Count; $i++) {
    $candidate = $items.Item($i)
    if ($null -eq $candidate -or $candidate.MessageClass -notlike 'IPM.Note*') {
        continue
    }

    if (($ExactSubject -and [string]$candidate.Subject -eq $Subject) -or
        (-not $ExactSubject -and [string]$candidate.Subject -like "*$Subject*")) {
        $match = $candidate
        break
    }
}

if ($null -eq $match) {
    throw "No matching draft found for subject '$Subject'."
}

if ($To.Count -gt 0) {
    $match.To = Join-OutlookRecipients $To
}
if ($Cc.Count -gt 0) {
    $match.CC = Join-OutlookRecipients $Cc
}
if ($Bcc.Count -gt 0) {
    $match.BCC = Join-OutlookRecipients $Bcc
}
if ($Body) {
    $match.Body = $Body
}

$match.Save()
Write-Host "Draft updated in store '$Store': $($match.Subject)"
