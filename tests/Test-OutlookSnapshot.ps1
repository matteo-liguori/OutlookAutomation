$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$fixture = Join-Path $env:TEMP ([guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $fixture | Out-Null
try {
    Copy-Item -LiteralPath (Join-Path $root 'Collect-OutlookMonitor.ps1') -Destination $fixture
    @'
function Invoke-OutlookHarness {
 param($Arguments,$InputObject)
 if(-not $global:fixtureMailSnapshots){$global:fixtureMailSnapshots=@{}}
 if($Arguments[1] -eq 'snapshot-put'){$global:fixtureMailSnapshots[$InputObject.SnapshotId]=$InputObject;return @{id=$InputObject.SnapshotId}}
 return $global:fixtureMailSnapshots[$Arguments[2]]
}
'@ | Set-Content (Join-Path $fixture 'OutlookHarness.Common.ps1') -Encoding utf8
    @'
function Get-OutlookAutomationNamespace {[pscustomobject]@{Fixture=$true}}
function Get-OutlookStoreByName {param($ns,$store);[pscustomobject]@{DisplayName='fixture';StoreID='store'}}
function Find-OutlookFolder {param($store,$folder);[pscustomobject]@{Items=$global:snapshotItems}}
'@| Set-Content (Join-Path $fixture 'OutlookAutomation.Common.ps1') -Encoding utf8
    $values = @(foreach ($id in @('A', 'B', 'C')) {
            $accessor = [pscustomobject]@{}
            $accessor | Add-Member ScriptMethod GetProperty { param($key)
                '' }
            [pscustomobject]@{EntryID = $id
                ReceivedTime = [datetime]'2026-10-13T12:00:00'
                MessageClass = 'IPM.Schedule.Meeting.Canceled'
                Subject = 'mock'
                SenderEmailAddress = 'test@example.invalid'
                ConversationID = 'mock'
                PropertyAccessor = $accessor
            }
        })
    $global:snapshotItems = [pscustomobject]@{Values = $values
        Count = 3
    }
    $global:snapshotItems | Add-Member ScriptMethod Sort { param($key, $reverse) }
    $global:snapshotItems | Add-Member ScriptMethod Item { param($index)
        $this.Values[$index - 1] }
    $args = @{Store = 'fixture'
        Since = '2026-10-13T00:00:00+01:00'
        Until = '2026-10-14T00:00:00+01:00'
        PageSize = 2
        MaxScan = 5
    }
    $first = & (Join-Path $fixture 'Collect-OutlookMonitor.ps1') @args | ConvertFrom-Json
    $global:snapshotItems.Values = @($values[0], $values[2])
    $global:snapshotItems.Count = 2
    $second = & (Join-Path $fixture 'Collect-OutlookMonitor.ps1') @args -Offset $first.NextOffset -SnapshotId $first.SnapshotId | ConvertFrom-Json
    if (-not $second.Complete -or @($second.Items).Count -ne 1 -or $second.Items[0].EntryId -ne 'C') {
        throw 'Snapshot continuation lost a current message.'
    }
    $blocked = $false
    try {
        & (Join-Path $fixture 'Collect-OutlookMonitor.ps1') @args -Offset 2 -ExpectedFirstId 'A' | Out-Null
    }
    catch {
        $blocked = $true
    }
    if (-not $blocked) {
        throw 'Unsafe live-offset continuation remained available.'
    }
    $global:snapshotItems.Values = @()
    $global:snapshotItems.Count = 0
    $empty = & (Join-Path $fixture 'Collect-OutlookMonitor.ps1') @args | ConvertFrom-Json
    if (-not $empty.Complete -or $empty.Items.Count -ne 0) {
        throw 'Empty snapshots must return an empty array.'
    }
    $accessor = [pscustomobject]@{}
    $accessor | Add-Member ScriptMethod GetProperty {param($key)
        if($key -eq 'http://schemas.microsoft.com/mapi/proptag/0x0E060040'){return [datetime]'2026-10-13T11:15:00'}
        return '<delivery-report@example.invalid>'
    }
    $report=[pscustomobject]@{EntryID='NDR';ReceivedTime=$null;MessageClass='REPORT.IPM.Note.NDR';Subject='Undeliverable: synthetic test';SenderEmailAddress='';ConversationID='report';PropertyAccessor=$accessor}
    $unsupported=[pscustomobject]@{EntryID='other';ReceivedTime=$null;MessageClass='IPM.Contact';Subject='Not mail'}
    $global:snapshotItems.Values=@($unsupported,$report,$values[0]);$global:snapshotItems.Count=3
    $reportPage=& (Join-Path $fixture 'Collect-OutlookMonitor.ps1') @args | ConvertFrom-Json
    if(-not $reportPage.Complete -or $reportPage.Errors.Count -or $reportPage.Items[0].EntryId -ne 'NDR' -or [datetimeoffset]$reportPage.Items[0].Received -ne [datetimeoffset]'2026-10-13T11:15:00Z' -or $reportPage.Items[0].ReceivedSource -ne 'PidTagMessageDeliveryTime'){
        throw ('Delivery report timestamp fallback or unsupported-item handling failed. ' + ($reportPage | ConvertTo-Json -Depth 6 -Compress))
    }
    $report.PropertyAccessor=[pscustomobject]@{}
    $report.PropertyAccessor | Add-Member ScriptMethod GetProperty {param($key);throw 'Missing delivery timestamp'}
    $failed=& (Join-Path $fixture 'Collect-OutlookMonitor.ps1') @args | ConvertFrom-Json
    if($failed.Complete -or $failed.Errors.Count -ne 1 -or $failed.Errors[0].EntryId -ne 'NDR' -or $failed.Errors[0].MessageClass -ne 'REPORT.IPM.Note.NDR'){
        throw 'Real timestamp failure must stay partial and identify the item without a false membership warning.'
    }
    @{assertions = 7 } | ConvertTo-Json -Compress
}
finally {
    $resolved = [IO.Path]::GetFullPath($fixture)
    $temporaryRoot = [IO.Path]::GetFullPath($env:TEMP).TrimEnd('\') + '\'
    if (-not $resolved.StartsWith($temporaryRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Fixture cleanup escaped the temporary root.'
    }
    Remove-Item -LiteralPath $resolved -Recurse -Force
}
