param(
    [Parameter(Mandatory = $true)]
    [string]$Store,

    [Parameter(Mandatory = $true)]
    [string[]]$To,

    [Parameter(Mandatory = $true)]
    [string]$Subject,

    [string]$Body = '',
    [string[]]$Cc = @(),
    [string[]]$Bcc = @(),
    [string[]]$Attachments = @()
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\OutlookAutomation.Common.ps1"

Assert-OutlookRecipients $To 'To'
Assert-OutlookRecipients $Cc 'Cc'
Assert-OutlookRecipients $Bcc 'Bcc'

$namespace = Get-OutlookAutomationNamespace
$storeItem = Get-OutlookStoreByName $namespace $Store
$drafts = Find-OutlookFolder $storeItem 'Drafts'
$mail = $drafts.Items.Add('IPM.Note')
$mail.To = Join-OutlookRecipients $To
$mail.CC = Join-OutlookRecipients $Cc
$mail.BCC = Join-OutlookRecipients $Bcc
$mail.Subject = $Subject
$mail.Body = $Body

foreach ($path in $Attachments) {
    if (-not (Test-Path -LiteralPath $path)) {
        throw "Attachment not found: $path"
    }

    $mail.Attachments.Add((Resolve-Path -LiteralPath $path).Path) | Out-Null
}

$mail.Save()
Write-Host "Draft saved in store '$Store'."
