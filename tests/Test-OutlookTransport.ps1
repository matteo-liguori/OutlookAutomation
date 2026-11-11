$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '../monitor/adapters/windows/OutlookAutomation.Common.ps1')
$script:count = 0
function Assert($value, $reason) {
    if (-not $value) {
        throw $reason
    }
    $script:count++
}
$expected = [ordered]@{Sender = 'fixture@example.invalid'
    To = @('approved@example.invalid')
    Cc = @()
    Bcc = @()
    Subject = 'Approved'
    BodySha256 = ('a' * 64)
    AttachmentCount = 0
    AttachmentHashes = @()
}
Assert-OutlookEnvelope $expected $expected
$script:count++
Assert ((Get-OutlookBodySha256 'Case') -cne (Get-OutlookBodySha256 'case')) 'Full body hashes preserve case'
foreach ($field in @('Sender', 'Subject', 'BodySha256', 'AttachmentCount', 'To', 'Cc', 'Bcc', 'AttachmentHashes')) {
    $actual = [ordered]@{}
    foreach ($key in $expected.Keys) {
        $actual[$key] = $expected[$key]
    }
    $actual[$field] = switch ($field) {
        'AttachmentCount' {
            1
        }'To' {
            @('approved@example.invalid', 'extra@example.invalid')
        }'Cc' {
            @('extra@example.invalid')
        }'Bcc' {
            @('hidden@example.invalid')
        }'AttachmentHashes' {
            @('b' * 64)
        }default {
            'different'
        }
    }
    $blocked = $false
    try {
        Assert-OutlookEnvelope $actual $expected
    }
    catch {
        $blocked = $true
    }
    Assert $blocked "Changed $field must be rejected"
}
$process = [pscustomobject]@{ProcessName = 'OUTLOOK'
    SessionId = 2
    Path = 'C:\Office\OUTLOOK.EXE'
    Id = 123
}
Assert-OutlookClassicProcess $process 'C:\Office\OUTLOOK.EXE' 2
$script:count++
foreach ($field in @('SessionId', 'ProcessName', 'Path')) {
    $copy = [pscustomobject]@{ProcessName = 'OUTLOOK'
        SessionId = 2
        Path = 'C:\Office\OUTLOOK.EXE'
        Id = 123
    }
    $copy.$field = switch ($field) {
        'SessionId' {
            3
        }'ProcessName' {
            'other'
        }default {
            'C:\Unexpected\OUTLOOK.EXE'
        }
    }
    $blocked = $false
    try {
        Assert-OutlookClassicProcess $copy 'C:\Office\OUTLOOK.EXE' 2
    }
    catch {
        $blocked = $true
    }
    Assert $blocked 'Process identity boundary'
}
# Exercise the actual bounded scanner with a store full of unrelated messages.
$global:scanReads = 0
$items = [pscustomobject]@{Count = 1000 }
$items | Add-Member ScriptMethod Sort { param($key, $reverse) }
$items | Add-Member ScriptMethod Item { param($index)
    $global:scanReads++
    [pscustomobject]@{LastModificationTime = [datetime]::Now
        MessageClass = 'IPM.Note'
        Subject = 'Unrelated'
    } }
$global:scanFolder = [pscustomobject]@{Name = 'Sent Items'
    FolderPath = 'Sent Items'
    Items = $items
}
function Get-OutlookMatchingStores {
    param($ns, $pattern)
    [pscustomobject]@{DisplayName = 'fixture'
        StoreID = 'fixture'
    }
}
function Get-OutlookFolderMap {
    param($store)
    $global:scanFolder
}
$result = Get-OutlookTransportScan -Namespace $null -Subject 'Requested subject' -MaxScan 5
Assert (-not $result.Complete) 'Cap must report incomplete'
Assert ($global:scanReads -eq 5) 'No-match search must be bounded'
@{assertions = $script:count } | ConvertTo-Json -Compress
