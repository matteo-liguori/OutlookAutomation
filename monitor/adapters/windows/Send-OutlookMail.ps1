param(
    [Parameter(Mandatory = $true)]
    [string[]]$To,

    [Parameter(Mandatory = $true)]
    [string]$Subject,

    [string]$Body = '',
    [string[]]$Cc = @(),
    [string[]]$Bcc = @(),
    [string[]]$Attachments = @(),
    [string]$Account,
    [switch]$BodyHtml,
    [switch]$DisplayOnly
)

$errorActionPreference = 'Stop'
. "$PSScriptRoot\OutlookAutomation.Common.ps1"

if (-not $DisplayOnly) {
    throw "Direct account/default sending is blocked before Outlook item creation. Use Send-OutlookStoreMailSafe.ps1 with -Store so the mailbox is explicit and duplicate checks run."
}

Assert-OutlookRecipients $To 'To'
Assert-OutlookRecipients $Cc 'Cc'
Assert-OutlookRecipients $Bcc 'Bcc'

$namespace = Get-OutlookAutomationNamespace
$outlook = $namespace.Application

$mail = $outlook.CreateItem(0)
$mail.To = Join-OutlookRecipients $To
$mail.CC = Join-OutlookRecipients $Cc
$mail.BCC = Join-OutlookRecipients $Bcc
$mail.Subject = $Subject

if ($BodyHtml) {
    $mail.HTMLBody = $Body
}
else {
    $mail.Body = $Body
}

if ($Account) {
    $matchedAccount = $null
    foreach ($candidate in $namespace.Accounts) {
        if ($candidate.SmtpAddress -eq $Account -or $candidate.DisplayName -eq $Account) {
            $matchedAccount = $candidate
            break
        }
    }

    if (-not $matchedAccount) {
        throw "No Outlook account matched '$Account'."
    }

    $mail.SendUsingAccount = $matchedAccount
}

foreach ($path in $Attachments) {
    if (-not (Test-Path -LiteralPath $path)) {
        throw "Attachment not found: $path"
    }

    $mail.Attachments.Add((Resolve-Path -LiteralPath $path).Path) | Out-Null
}

$mail.Display()
Write-Host 'Draft displayed in Outlook.'
