param(
    [Parameter(Mandatory = $true)][string]$Store,
    [Parameter(Mandatory = $true)][datetimeoffset]$Since,
    [Parameter(Mandatory = $true)][datetimeoffset]$Until,
    [ValidateRange(1, 500)][int]$PageSize = 150,
    [ValidateRange(1, 5000)][int]$MaxScan = 750,
    [ValidateRange(0, 5000)][int]$Offset = 0,
    [string]$SnapshotId = ''
)
$ErrorActionPreference = 'Stop'
if ($Since -gt $Until) {
    throw 'Since must not exceed Until.'
}
. "$PSScriptRoot/OutlookHarness.Common.ps1"
if ($SnapshotId) {
    if ($SnapshotId -notmatch '^[a-f0-9]{32}$') {
        throw 'Invalid snapshot ID.'
    }
    $snapshot = Invoke-OutlookHarness -Arguments @('thread', 'snapshot-get', $SnapshotId)
    if ($snapshot.Store -ne $Store -or [datetimeoffset]$snapshot.Since -ne $Since -or [datetimeoffset]$snapshot.Until -ne $Until) {
        throw 'Snapshot store/window mismatch.'
    }
}
else {
    if ($Offset) {
        throw 'Continuation requires the SnapshotId returned by the first page.'
    }
    . "$PSScriptRoot/OutlookAutomation.Common.ps1"
    $ns = Get-OutlookAutomationNamespace
    $storeItem = Get-OutlookStoreByName $ns $Store
    $items = (Find-OutlookFolder $storeItem 'Inbox').Items
    $items.Sort('[ReceivedTime]', $true)
    $initialCount = $items.Count
    $membership = New-Object 'System.Collections.Generic.List[string]'
    $rows = @()
    $errors = @()
    $boundary = $false
    $scanned = 0
    $started = [DateTimeOffset]::UtcNow.ToString('o')
    # Capture bounded metadata once; later pages use the immutable copy.
    for ($i = 1; $i -le $items.Count -and $scanned -lt $MaxScan; $i++) {
        $scanned++
        try {
            $m = $items.Item($i)
            $received = ([datetime]$m.ReceivedTime).ToUniversalTime()
            $membership.Add([string]$m.EntryID)
            if ($received -lt $Since.UtcDateTime) {
                $boundary = $true
                break
            }
            if ($received -gt $Until.UtcDateTime) {
                continue
            }
            if ($m.MessageClass -notlike 'IPM.Note*' -and $m.MessageClass -notlike 'IPM.Schedule.Meeting.*') {
                continue
            }
            $internetId = ''
            try {
                $internetId = [string]$m.PropertyAccessor.GetProperty('http://schemas.microsoft.com/mapi/proptag/0x1035001F')
            }
            catch {
            } # Optional MAPI property; store-qualified EntryId remains usable.
            $rows += [pscustomobject]@{Store = $Store
                StoreId = [string]$storeItem.StoreID
                EntryId = [string]$m.EntryID
                InternetMessageId = $internetId
                ConversationId = [string]$m.ConversationID
                MessageClass = [string]$m.MessageClass
                Received = $received.ToString('o')
                Subject = [string]$m.Subject
                SenderEmail = [string]$m.SenderEmailAddress
            }
        }
        catch {
            $errors += [pscustomobject]@{Index = $i
                Error = $_.Exception.Message
            }
        }
    }
    if ($items.Count -ne $initialCount) {
        $errors += 'Inbox membership changed during capture; repeat the bounded snapshot.'
    }
    else {
        for ($j = 0; $j -lt $membership.Count; $j++) {
            if ([string]$items.Item($j + 1).EntryID -ne $membership[$j]) {
                $errors += 'Inbox membership changed during capture; repeat the bounded snapshot.'
                break
            }
        }
    }
    $complete = ($boundary -or $i -gt $items.Count) -and -not $errors.Count
    if (-not $complete -and -not $errors.Count) {
        $errors += 'Bounded snapshot scan cap reached; repeat collection with an appropriate bounded limit.'
    }
    $SnapshotId = [guid]::NewGuid().ToString('N')
    $snapshot = [pscustomobject]@{Store = $Store
        StoreId = [string]$storeItem.StoreID
        Since = $Since.ToUniversalTime().ToString('o')
        Until = $Until.ToUniversalTime().ToString('o')
        SnapshotId = $SnapshotId
        StartedAt = $started
        Scanned = $scanned
        Complete = $complete
        Errors = $errors
        Items = $rows
    }
    Invoke-OutlookHarness -Arguments @('thread', 'snapshot-put', '--input', '-') -InputObject $snapshot | Out-Null
}
$all = @($snapshot.Items)
if ($Offset -gt $all.Count) {
    throw 'Offset exceeds the saved snapshot.'
}
$end = [Math]::Min($Offset + $PageSize, $all.Count)
$page = @()
if ($end -gt $Offset) {
    $page = @($all[$Offset..($end - 1)])
}
$more = $end -lt $all.Count
[pscustomobject]@{Store = $Store
    StoreId = $snapshot.StoreId
    Since = $Since.ToUniversalTime().ToString('o')
    Until = $Until.ToUniversalTime().ToString('o')
    SnapshotId = $SnapshotId
    StartedAt = ([datetimeoffset]$snapshot.StartedAt).ToUniversalTime().ToString('o')
    FirstId = if ($all.Count) {
        $all[0].EntryId
    }
    else {
        ''
    }
    Offset = $Offset
    NextOffset = if ($more) {
        $end
    }
    else {
        $null
    }
    Scanned = $snapshot.Scanned
    Count = $page.Count
    Newest = if ($all.Count) {
        $all[0].Received
    }
    else {
        $null
    }
    Complete = (-not $more -and $snapshot.Complete)
    Errors = @($snapshot.Errors)
    Items = $page
} | ConvertTo-Json -Depth 6
