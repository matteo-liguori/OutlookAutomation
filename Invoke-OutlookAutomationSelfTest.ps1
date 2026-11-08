param(
    [ValidateSet('Static', 'DryRun', 'SendApproved')]
    [string]$Mode = 'DryRun',

    [string]$Store = '',
    [string[]]$Stores = @(),
    [string]$To = '',
    [int]$SendCountPerStore = 1,
    [switch]$ApproveRealSend,
    [switch]$AllowSelfTestRecipient
)

$ErrorActionPreference = 'Stop'
if ($Mode -ne 'Static' -and -not $Store -and -not $Stores.Count) {
    throw 'Supply -Store or -Stores with exact configured mailbox addresses.'
}

function Add-Result {
    param(
        [string]$Name,
        [string]$Status,
        [string]$Detail = ''
    )

    [pscustomobject]@{
        Name = $Name
        Status = $Status
        Detail = $Detail
    }
}

function Invoke-Step {
    param(
        [string]$Name,
        [scriptblock]$Script,
        [switch]$ExpectFailure
    )

    try {
        $output = & $Script 2>&1 | Out-String
        $detail = ($output -replace '\s+', ' ').Trim()
        if ($ExpectFailure) {
            return Add-Result $Name 'FAIL' "Expected failure, but command succeeded. $detail"
        }

        return Add-Result $Name 'PASS' $detail
    }
    catch {
        if ($ExpectFailure) {
            return Add-Result $Name 'PASS' "Blocked as expected: $($_.Exception.Message)"
        }

        return Add-Result $Name 'FAIL' $_.Exception.Message
    }
}

function Test-PowerShellParsing {
    $errors = @()
    Get-ChildItem -LiteralPath $PSScriptRoot -Filter '*.ps1' | ForEach-Object {
        $tokens = $null
        $parseErrors = $null
        [System.Management.Automation.Language.Parser]::ParseFile($_.FullName, [ref]$tokens, [ref]$parseErrors) | Out-Null
        foreach ($err in $parseErrors) {
            $errors += "$($_.Name):$($err.Extent.StartLineNumber):$($err.Message)"
        }
    }

    if ($errors.Count -gt 0) {
        throw ($errors -join '; ')
    }

    'All ps1 files parsed successfully.'
}

function Assert-FullEmailAddress {
    param([string]$Address)

    if (-not $Address -or -not $Address.Trim() -or $Address -notmatch '@') {
        throw 'A real send requires -To with the exact full recipient email address.'
    }

    if ($Address -notmatch '^[^@\s]+@[^@\s]+\.[^@\s]+$') {
        throw "Invalid recipient email address: '$Address'."
    }
}

function Assert-SelfTestRecipientAllowed {
    param([string]$Address, [bool]$AllowSelfTest)

    if (-not $ApproveRealSend) {
        throw 'SendApproved mode requires -ApproveRealSend for every recipient.'
    }
}

function New-TestMessage {
    param([string]$FromStore, [int]$Index, [int]$Total)

    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $id = [guid]::NewGuid().ToString('N').Substring(0, 10)
    [pscustomobject]@{
        Subject = "Outlook automation self-test $stamp $FromStore #$Index $id"
        Body = @"
Outlook automation self-test.

Store: $FromStore
Recipient: $To
Message: $Index of $Total
Unique id: $id

This is an approved test email for validating the local Outlook automation tooling.
"@
    }
}

$results = @()
$results += Invoke-Step 'PowerShell parser check' { Test-PowerShellParsing }
if ($Mode -ne 'Static') {
    $results += Invoke-Step 'Inspect Outlook stores' { & "$PSScriptRoot\Inspect-Outlook.ps1" -Action Stores }
}

$probeRecipient = if ($To) {
    $To
}
else {
    'self-test@example.invalid'
}
$probeSubject = "Outlook automation dry-run duplicate probe $(Get-Date -Format 'yyyyMMdd-HHmmss')"
$probeBody = "Dry-run duplicate probe only. No send is performed. $(New-Guid)"

if ($Mode -in @('DryRun', 'SendApproved')) {
    $results += Invoke-Step 'Duplicate probe across stores' {
        & "$PSScriptRoot\Test-OutlookSendDuplicate.ps1" `
            -Store '*' `
            -Subject $probeSubject `
            -Body $probeBody `
            -Recipients @($probeRecipient) `
            -Limit 10
    }

    $results += Invoke-Step 'Block direct account send path' {
        & "$PSScriptRoot\Send-OutlookMail.ps1" `
            -To @($probeRecipient) `
            -Subject $probeSubject `
            -Body $probeBody
    } -ExpectFailure

    $results += Invoke-Step 'Block subject-matched draft send path' {
        & "$PSScriptRoot\Send-OutlookStoreDraft.ps1" `
            -Store $Store `
            -Subject $probeSubject `
            -ExactSubject
    } -ExpectFailure

    $results += Invoke-Step 'Block SMTP fallback send path' {
        & "$PSScriptRoot\Send-OutlookStoreSmtpMail.ps1" `
            -Store $Store `
            -To @($probeRecipient) `
            -Subject $probeSubject `
            -Body $probeBody
    } -ExpectFailure
}

if ($Mode -eq 'SendApproved') {
    Assert-FullEmailAddress $To
    Assert-SelfTestRecipientAllowed $To $AllowSelfTestRecipient
    $sendStores = if ($Stores.Count) {
        $Stores
    }
    else {
        @($Store)
    }
    $batch = @(foreach ($sender in $sendStores) {
            for ($i = 1; $i -le [Math]::Max(1, $SendCountPerStore); $i++) {
                $message = New-TestMessage $sender $i $SendCountPerStore
                [pscustomobject]@{Store = $sender
                    Subject = $message.Subject
                    Body = $message.Body
                }
            }
        })
    & "$PSScriptRoot/Start-OutlookSendReceive.ps1"
    foreach ($message in $batch) {
        $preflight = & "$PSScriptRoot/Test-OutlookSendDuplicate.ps1" -Subject $message.Subject -Body $message.Body -Recipients @($To) -LookbackDays 1 | ConvertFrom-Json
        if (-not $preflight.Complete -or $preflight.Rows.Count) {
            throw 'Self-test preflight is incomplete or found an existing item.'
        }
    }
    foreach ($message in $batch) {
        $results += Invoke-Step "Submit self-test from $($message.Store)" {
            & "$PSScriptRoot/Send-OutlookStoreMailSafe.ps1" -Store $message.Store -To @($To) -Subject $message.Subject -Body $message.Body -BatchSubmitOnly
        }
    }
    & "$PSScriptRoot/Start-OutlookSendReceive.ps1"
    . "$PSScriptRoot/OutlookAutomation.Common.ps1"
    . "$PSScriptRoot/OutlookMessageLookup.ps1"
    $ns = Get-OutlookAutomationNamespace
    foreach ($message in $batch) {
        $results += Invoke-Step "Verify self-test from $($message.Store)" {
            $scan = Get-OutlookTransportScan -Namespace $ns -Subject $message.Subject -Body $message.Body -Recipients @($To) -LookbackDays 1
            if (-not $scan.Complete) {
                throw 'Self-test transport inspection is incomplete.'
            }
            $sent = @($scan.Rows | Where-Object { $_.Sent -and $_.Store -eq $message.Store })
            if ($sent.Count -ne 1) {
                throw 'Self-test not verified as exactly one Sent copy; reconcile under the unchanged approved-send recovery policy.'
            }
            $item = (Resolve-OutlookExactMessage $ns $sent[0].EntryId -StoreId $sent[0].StoreId -Folder $sent[0].Folder).Message
            $expected = [ordered]@{Sender = $message.Store
                To = @($To.ToLowerInvariant())
                Cc = @()
                Bcc = @()
                Subject = $message.Subject
                BodySha256 = Get-OutlookBodySha256 $message.Body
                AttachmentCount = 0
                AttachmentHashes = @()
            }
            Assert-OutlookEnvelope (Get-OutlookEnvelope $item) $expected
            'One exact Sent copy verified.'
        }
    }
}

$results | ConvertTo-Json -Depth 4
if (@($results | Where-Object { $_.Status -eq 'FAIL' }).Count) {
    throw 'Self-test has failures; inspect the recorded per-item results.'
}
