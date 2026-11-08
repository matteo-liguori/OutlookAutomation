param(
    [Parameter(Mandatory = $true)][string]$Store,
    [Parameter(Mandatory = $true)][string[]]$To,
    [Parameter(Mandatory = $true)][string]$Subject,
    [string]$Body = '', [string[]]$Cc = @(), [string[]]$Bcc = @(),
    [string[]]$Attachments = @(), [switch]$HighImportance,
    [switch]$DisplayOnly, [switch]$BatchSubmitOnly, [switch]$AllowDuplicate,
    [string]$ReplyToEntryId = '',
    [ValidateRange(1, 3650)][int]$DuplicateLookbackDays = 30,
    [ValidateRange(1, 5000)][int]$MaxScan = 500
)
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot/OutlookAutomation.Common.ps1"
if ($DisplayOnly -and $BatchSubmitOnly) {
    throw 'DisplayOnly and BatchSubmitOnly cannot be combined.'
}
if (-not $DisplayOnly -and -not $BatchSubmitOnly) {
    throw 'Real sends require -BatchSubmitOnly under the AGENTS.md send-session workflow.'
}
Assert-OutlookRecipients $To 'To'
Assert-OutlookRecipients $Cc 'Cc'
Assert-OutlookRecipients $Bcc 'Bcc'
if (-not (Join-OutlookRecipients $To)) {
    throw 'At least one To recipient is required.'
}
foreach ($path in $Attachments) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Attachment not found: $path"
    }
}
$ns = Get-OutlookAutomationNamespace
$storeItem = Get-OutlookStoreByName $ns $Store
$account = @($ns.Accounts | Where-Object { $_.SmtpAddress -eq $Store })
if ($account.Count -ne 1) {
    throw 'Exactly one matching sender account is required.'
}
if ([string]$account[0].DeliveryStore.StoreID -ne [string]$storeItem.StoreID) {
    throw 'Sender account and target store do not agree.'
}
if (-not $DisplayOnly -and -not $AllowDuplicate) {
    $scan = Get-OutlookTransportScan -Namespace $ns -Subject $Subject -Body $Body -Recipients ($To + $Cc + $Bcc) -LookbackDays $DuplicateLookbackDays -MaxScan $MaxScan
    if (-not $scan.Complete) {
        throw ('Duplicate inspection incomplete: ' + ($scan.Errors -join '; '))
    }
    $matches = @($scan.Rows | Where-Object { $_.BodyMatch -or (-not $Body -and $_.RecipientOverlap) })
    if ($matches.Count) {
        throw 'Related mail exists in Sent/Outbox/Drafts; reconcile it before submission.'
    }
}
$drafts = Find-OutlookFolder $storeItem 'Drafts'
if ($ReplyToEntryId) {
    $original = $ns.GetItemFromID($ReplyToEntryId, [string]$storeItem.StoreID)
    if ($original.MessageClass -notlike 'IPM.Note*') {
        throw 'Reply source must be a mail message in the intended sender store.'
    }
    $mail = $original.Reply()
} else {
    $mail = $drafts.Items.Add('IPM.Note')
}
$mail.To = Join-OutlookRecipients $To
$mail.CC = Join-OutlookRecipients $Cc
$mail.BCC = Join-OutlookRecipients $Bcc
$mail.Subject = $Subject
$mail.Body = $Body
if ($ReplyToEntryId) {
    while ($mail.Attachments.Count -gt 0) {
        $mail.Attachments.Remove(1)
    }
}
$mail.SendUsingAccount = $account[0]
if ($HighImportance) {
    $mail.Importance = 2
}
if ($Store -notlike '*@gmail.com') {
    $sent = Find-OutlookFolder $storeItem 'Sent Items' -AllowMissing
    if ($sent) {
        $mail.SaveSentMessageFolder = $sent
    }
}
foreach ($path in $Attachments) {
    $null = $mail.Attachments.Add((Resolve-Path -LiteralPath $path).Path)
}
$mail.Save()
if ($DisplayOnly) {
    $mail.Display()
    Write-Host "Draft displayed from '$Store'."
    return
}
$mail.Send()
[pscustomobject]@{Store = $Store
    Status = 'submitted'
    Verified = $false
    NextStep = 'Complete the single end-of-session refresh and exact Sent/Outbox/Drafts reconciliation.'
} | ConvertTo-Json
