param(
    [Parameter(Mandatory = $true)]
    [string]$PayloadPath,

    [switch]$DisplayOnly,
    [switch]$SendApproved
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\OutlookAutomation.Common.ps1"

function Get-JsonStringArray {
    param(
        $Payload,
        [string]$Name,
        [switch]$Required
    )

    $value = $Payload.$Name
    if ($null -eq $value) {
        if ($Required) {
            throw "Payload is missing required '$Name' array."
        }

        return @()
    }

    if ($value -is [string]) {
        return @($value)
    }

    return @($value | ForEach-Object { [string]$_ })
}

function Get-JsonString {
    param(
        $Payload,
        [string]$Name,
        [switch]$Required
    )

    $value = $Payload.$Name
    if ($null -eq $value -or -not [string]$value) {
        if ($Required) {
            throw "Payload is missing required '$Name' string."
        }

        return ''
    }

    return [string]$value
}

if (-not (Test-Path -LiteralPath $PayloadPath)) {
    throw "Payload file not found: $PayloadPath"
}

$resolvedPayloadPath = (Resolve-Path -LiteralPath $PayloadPath).Path
$payload = Get-Content -LiteralPath $resolvedPayloadPath -Raw | ConvertFrom-Json

$store = Get-JsonString $payload 'store' -Required
$to = Get-JsonStringArray $payload 'to' -Required
$cc = Get-JsonStringArray $payload 'cc'
$bcc = Get-JsonStringArray $payload 'bcc'
$subject = Get-JsonString $payload 'subject' -Required
$body = Get-JsonString $payload 'body'
$attachments = Get-JsonStringArray $payload 'attachments'
$allowDuplicate = [bool]$payload.allowDuplicate

$duplicateLookbackDays = 30
if ($null -ne $payload.duplicateLookbackDays) {
    $duplicateLookbackDays = [int]$payload.duplicateLookbackDays
}


Assert-OutlookRecipients $to 'To'
Assert-OutlookRecipients $cc 'Cc'
Assert-OutlookRecipients $bcc 'Bcc'

foreach ($attachment in $attachments) {
    if (-not (Test-Path -LiteralPath $attachment)) {
        throw "Attachment not found: $attachment"
    }
}

$summary = [pscustomobject]@{
    Payload = $resolvedPayloadPath
    Store = $store
    To = $to
    Cc = $cc
    Bcc = $bcc
    Subject = $subject
    BodyChars = $body.Length
    Attachments = $attachments
    DisplayOnly = [bool]$DisplayOnly
    SendApproved = [bool]$SendApproved
}

$summary | ConvertTo-Json -Depth 4

if (-not $DisplayOnly -and -not $SendApproved) {
    throw 'Real sends require -SendApproved. Use -DisplayOnly to display a draft without sending.'
}

$sendArgs = @{
    Store = $store
    To = $to
    Subject = $subject
    Body = $body
    Cc = $cc
    Bcc = $bcc
    Attachments = $attachments
    DuplicateLookbackDays = $duplicateLookbackDays
    BatchSubmitOnly = (-not $DisplayOnly)
}

if ($DisplayOnly) {
    $sendArgs.DisplayOnly = $true
}

if ($allowDuplicate) {
    $sendArgs.AllowDuplicate = $true
}

& "$PSScriptRoot\Send-OutlookStoreMailSafe.ps1" @sendArgs
