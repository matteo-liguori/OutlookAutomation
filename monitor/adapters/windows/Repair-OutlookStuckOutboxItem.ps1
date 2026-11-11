param(
    [Parameter(Mandatory = $true)]
    [string]$Store,

    [Parameter(Mandatory = $true)]
    [string]$EntryId,

    [ValidateSet('Diagnose', 'RepairApproved')]
    [string]$Action = 'Diagnose',

    [string]$ExpectedTo = '',
    [string]$ExpectedSubject = '',
    [string]$ExpectedCc = '',
    [string]$ExpectedBcc = '',
    [string[]]$ExpectedAttachmentHashes = @(),
    [ValidateRange(1, 5000)][int]$MaxScan = 500,
    [ValidateRange(1, 3650)][int]$LookbackDays = 30,
    [string]$ExpectedBodySha256 = '',
    [int]$ExpectedAttachmentCount = -1,
    [int]$VerifyTimeoutSeconds = 60
)

$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'OutlookAutomation.Common.ps1')
. (Join-Path $PSScriptRoot 'OutlookMessageLookup.ps1')




function Get-ExactTransportMatches {
    param($Namespace, [string]$Subject, [string]$Recipient, [string]$BodySha256, [string]$StoreName = '*', $Envelope = $null)
    $scan = Get-OutlookTransportScan -Namespace $Namespace -Store $StoreName -Subject $Subject -BodySha256 $BodySha256 -Recipients @(Get-OutlookRecipientSet @($Recipient)) -MaxScan $MaxScan -LookbackDays $LookbackDays
    if (-not $scan.Complete) {
        throw ('Transport coverage incomplete: ' + ($scan.Errors -join '; '))
    }
    $matches = @()
    foreach ($row in $scan.Rows) {
        if (-not $row.RecipientOverlap -or (Normalize-OutlookSubject $row.Subject) -ne (Normalize-OutlookSubject $Subject)) {
            continue
        }
        if ($Envelope) {
            $message = (Resolve-OutlookExactMessage $Namespace $row.EntryId -StoreId $row.StoreId -Folder $row.Folder).Message
            $actual = Get-OutlookEnvelope $message
            try {
                Assert-OutlookEnvelope $actual $Envelope
            }
            catch {
                continue
            } # A verified different envelope is a different message.
        }
        $matches += $row
    }
    return $matches
}

function Get-ItemSummary {
    param($Item)

    $account = $null
    try {
        if ($null -ne $Item.SendUsingAccount) {
            $account = [string]$Item.SendUsingAccount.SmtpAddress
        }
    }
    catch {
    }

    $recipientRows = @($Item.Recipients | ForEach-Object {
            [pscustomobject]@{
                Name = [string]$_.Name
                Address = [string]$_.Address
                Type = [int]$_.Type
                Resolved = [bool]$_.Resolved
            }
        })

    return [pscustomobject]@{
        Envelope = Get-OutlookEnvelope $Item
        EntryId = [string]$Item.EntryID
        Store = [string]$Item.Parent.Store.DisplayName
        Folder = [string]$Item.Parent.FolderPath
        Subject = [string]$Item.Subject
        To = [string]$Item.To
        Cc = [string]$Item.CC
        Bcc = [string]$Item.BCC
        BodySha256 = Get-OutlookBodySha256 ([string]$Item.Body)
        AttachmentCount = [int]$Item.Attachments.Count
        Sent = [bool]$Item.Sent
        Submitted = [bool]$Item.Submitted
        DeferredDeliveryTime = Convert-OutlookComDate $Item.DeferredDeliveryTime
        SendUsingAccount = $account
        Recipients = $recipientRows
    }
}

function Assert-RepairInputs {
    $to = @(Get-OutlookRecipientSet @($ExpectedTo))
    if (-not $to.Count) {
        throw 'RepairApproved requires the complete approved To list.'
    }
    Assert-OutlookRecipients ($to + @(Get-OutlookRecipientSet @($ExpectedCc, $ExpectedBcc))) 'approved envelope'
    if (-not $ExpectedSubject.Trim() -or $ExpectedBodySha256 -notmatch '^[0-9a-fA-F]{64}$') {
        throw 'RepairApproved requires the approved subject and body hash.'
    }
    if ($ExpectedAttachmentCount -lt 0 -or $ExpectedAttachmentHashes.Count -ne $ExpectedAttachmentCount) {
        throw 'RepairApproved requires the approved attachment count and hashes (empty for zero attachments).'
    }
    foreach ($hash in $ExpectedAttachmentHashes) {
        if ($hash -notmatch '^[0-9a-fA-F]{64}$') {
            throw 'Invalid approved attachment hash.'
        }
    }
}

function Assert-ItemMatchesApproval {
    param($Item, $Expected)
    $summary = Get-ItemSummary $Item
    if ($summary.Store -ne $Store -or $summary.Sent -or $summary.Submitted) {
        throw 'Item store or submission state does not permit recovery.'
    }

    Assert-OutlookEnvelope -Actual $summary.Envelope -Expected $expected
    return $summary
}

$namespace = Get-OutlookAutomationNamespace
$item = (Resolve-OutlookExactMessage $namespace $EntryId -Store $Store -Folder 'Outbox').Message
$initialSummary = Get-ItemSummary $item

if ($Action -eq 'Diagnose') {
    $matches = @(Get-ExactTransportMatches $namespace $initialSummary.Subject ($initialSummary.Envelope.To -join ';') $initialSummary.BodySha256)
    [pscustomobject]@{
        Action = 'Diagnose'
        Item = $initialSummary
        ExactTransportMatches = @($matches)
        RepairEligible = (-not $initialSummary.Sent -and -not $initialSummary.Submitted -and $initialSummary.Folder -like '*\Outbox' -and @($matches).Count -eq 1)
        NextStep = 'Recover only against the original approved full envelope/body/attachment signature; unchanged approval persists through technical recovery.'
    } | ConvertTo-Json -Depth 7
    exit 0
}

Assert-RepairInputs
$expected = [ordered]@{Sender = $Store
    To = @(Get-OutlookRecipientSet @($ExpectedTo) | Sort-Object -Unique)
    Cc = @(Get-OutlookRecipientSet @($ExpectedCc) | Sort-Object -Unique)
    Bcc = @(Get-OutlookRecipientSet @($ExpectedBcc) | Sort-Object -Unique)
    Subject = $ExpectedSubject
    BodySha256 = $ExpectedBodySha256.ToLowerInvariant()
    AttachmentCount = $ExpectedAttachmentCount
    AttachmentHashes = @($ExpectedAttachmentHashes | ForEach-Object { $_.ToLowerInvariant() } | Sort-Object)
}
$initialSummary = Assert-ItemMatchesApproval $item $expected
if ($initialSummary.Folder -notlike '*\Outbox') {
    throw "RepairApproved requires the sole candidate to be in Outbox; found '$($initialSummary.Folder)'."
}

# A fresh Send/Receive is mandatory. It may release the item without repair, so
# reacquire and repeat the exact duplicate guard before changing anything.
& (Join-Path $PSScriptRoot 'Start-OutlookSendReceive.ps1')

$namespace = Get-OutlookAutomationNamespace
$matches = @(Get-ExactTransportMatches $namespace $ExpectedSubject $ExpectedTo $ExpectedBodySha256 -Envelope $expected)
$sentMatches = @($matches | Where-Object { $_.Sent -or $_.Folder -match '\\(Sent Mail|Sent Items)$' })
if ($sentMatches.Count -gt 0) {
    [pscustomobject]@{
        Action = 'AlreadySentAfterRefresh'
        SentMatches = $sentMatches
        Message = 'A Sent copy appeared during the mandatory refresh. No repair or resend was performed.'
    } | ConvertTo-Json -Depth 6
    exit 0
}

$unsentMatches = @($matches | Where-Object { -not $_.Sent })
if ($unsentMatches.Count -ne 1) {
    throw "Expected exactly one unsent exact match after refresh; found $($unsentMatches.Count). Refusing repair."
}
if ($unsentMatches[0].Folder -notlike '*\Outbox') {
    throw "The sole exact match is not in Outbox: '$($unsentMatches[0].Folder)'. Refusing repair."
}

$item = (Resolve-OutlookExactMessage $namespace $unsentMatches[0].EntryId -Store $Store -Folder 'Outbox').Message
$null = Assert-ItemMatchesApproval $item $expected
$storeItem = Get-OutlookStoreByName $namespace $Store
$account = @($namespace.Accounts | Where-Object { $_.SmtpAddress -eq $Store -or $_.DisplayName -eq $Store }) | Select-Object -First 1
if ($null -eq $account) {
    throw "No Outlook account matched '$Store'."
}

# For IMAP stores, the server-mapped [Gmail]\Drafts folder can preserve the
# broken Outbox state. Move the same item to the store-local Drafts folder.
$drafts = @(Get-OutlookFolderMap $storeItem | Where-Object {
        $_.Name -eq 'Drafts' -and -not ([string]$_.FolderPath).Contains('[Gmail]')
    }) | Select-Object -First 1
if ($null -eq $drafts) {
    throw "No store-local Drafts folder was found in '$Store'."
}

$moved = $item.Move($drafts)
$null = Assert-ItemMatchesApproval $moved $expected
$moved.SendUsingAccount = $account
if (-not $moved.Recipients.ResolveAll()) {
    throw 'Recipient resolution failed after moving the item to local Drafts.'
}
$moved.Save()
$moved.Send()
$namespace.SendAndReceive($false)

$verified = @()
$deadline = (Get-Date).AddSeconds($VerifyTimeoutSeconds)
while ((Get-Date) -lt $deadline -and $verified.Count -eq 0) {
    Start-Sleep -Seconds 3
    $verified = @(Get-ExactTransportMatches $namespace $ExpectedSubject $ExpectedTo $ExpectedBodySha256 $Store -Envelope $expected | Where-Object {
            $_.Sent -or $_.Folder -match '\\(Sent Mail|Sent Items)$'
        })
}

if ($verified.Count -eq 1) {
    $sentItem = (Resolve-OutlookExactMessage $namespace $verified[0].EntryId -Store $Store -Folder $verified[0].Folder).Message
    Assert-OutlookEnvelope (Get-OutlookEnvelope $sentItem) $expected
    [pscustomobject]@{
        Action = 'RepairedAndVerified'
        SentMatch = $verified[0]
        Message = 'Moved the sole stuck item to store-local Drafts, submitted that same item once, and verified one Sent copy.'
    } | ConvertTo-Json -Depth 6
}
elseif ($verified.Count -gt 1) {
    throw "Unexpected duplicate state: $($verified.Count) exact Sent matches appeared. Do not retry."
}
else {
    $remaining = @(Get-ExactTransportMatches $namespace $ExpectedSubject $ExpectedTo $ExpectedBodySha256 $Store -Envelope $expected)
    [pscustomobject]@{
        Action = 'SubmittedButUnverified'
        RemainingMatches = $remaining
        Message = 'The same item was submitted once but no Sent copy appeared within the timeout. Do not retry; verify server-side Sent and inspect Outbox/Drafts.'
    } | ConvertTo-Json -Depth 7
}
