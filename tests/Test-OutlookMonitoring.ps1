$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '..\monitor\adapters\windows\OutlookMonitoring.Common.ps1')
$count = 0
function Assert-True($value, $message) {
    if (-not $value) {
        throw $message
    }
    $script:count++
}
$s = [datetime]'2026-10-01T00:00:00'
$e = [datetime]'2026-10-04T00:00:00'
$filter = New-OutlookCalendarFilter $s $e
Assert-True ($filter.Contains('01 Oct 2026') -and $filter.Contains('04 Oct 2026')) 'Calendar dates must have unambiguous month names.'
Assert-True (Test-OutlookCalendarOverlap '2026-09-30T10:00:00' '2026-10-01T12:00:00' $s $e) 'Spanning event must overlap.'
Assert-True (-not (Test-OutlookCalendarOverlap '2026-09-30T10:00:00' '2026-10-01T00:00:00' $s $e)) 'Exclusive end boundary must not overlap.'
Assert-True (-not (Test-OutlookCalendarOverlap '2026-10-04T00:00:00' '2026-10-05T00:00:00' $s $e)) 'Exclusive start boundary must not overlap.'
Assert-True (-not (Test-OutlookCalendarOverlap '2026-06-01T00:00:00' '2026-06-02T00:00:00' $s $e)) 'Wrong-month event must be rejected.'
$ok = [pscustomobject]@{StartedAt = [datetime]::UtcNow
    EndedAt = [datetime]::UtcNow
    Errors = @()
}
Assert-True ((Get-OutlookSyncOutcome @($ok) $false $true 75 75) -eq 'success') 'Observed completion must succeed.'
Assert-True ((Get-OutlookSyncOutcome @($ok) $false $true 0.1 120) -eq 'success') 'Observed completion must not wait out the timeout.'
Assert-True ((Get-OutlookSyncOutcome @($ok) $true $true 75 75) -eq 'failed') 'Offline cannot succeed.'
Assert-True ((Get-OutlookSyncOutcome @() $false $true 75 75) -eq 'unverified') 'No observable groups cannot succeed.'
$pending = [pscustomobject]@{StartedAt = [datetime]::UtcNow
    EndedAt = $null
    Errors = @()
}
Assert-True ((Get-OutlookSyncOutcome @($pending) $false $true 75 75) -eq 'timeout') 'Missing completion must timeout.'
Assert-True ((Get-OutlookSyncOutcome @($ok,$pending) $false $true 1 120) -eq 'unverified') 'One complete group cannot hide another pending group.'
Assert-True ((Get-OutlookSyncOutcome @($ok,$pending) $false $true 120 120) -eq 'timeout') 'A pending group must still time out.'
Assert-True ((Get-OutlookSyncOutcome @($ok,$pending) $false $true 210 300) -eq 'unverified') 'A normal three-plus-minute sync must remain pending within its bound.'
Assert-True ((Get-OutlookSyncOutcome @($ok) $false $true 210 300) -eq 'success') 'A completed long sync succeeds before the five-minute bound.'
Assert-True ((Get-OutlookSyncOutcome @($pending) $false $true 300 300) -eq 'timeout') 'A missing completion still fails at the configured bound.'
Assert-True ((Get-OutlookSyncOutcome @($ok) $false $false 1 120) -eq 'unverified') 'Old completion cannot replace a refresh trigger.'
$reversed=[pscustomobject]@{StartedAt=[datetime]'2026-10-26T10:01:00Z';EndedAt=[datetime]'2026-10-26T10:00:00Z';Errors=@()}
Assert-True ((Get-OutlookSyncOutcome @($reversed) $false $true 1 120) -eq 'failed') 'End before start is invalid evidence.'
$bad = [pscustomobject]@{StartedAt = [datetime]::UtcNow
    EndedAt = [datetime]::UtcNow
    Errors = @('sync failure')
}
Assert-True ((Get-OutlookSyncOutcome @($bad) $false $true 75 75) -eq 'failed') 'SyncEnd cannot erase OnError.'
$now = [datetime]'2026-10-13T20:00:00'
function Sample($received, $id = 'item', $folder = 'topic') {
    [pscustomobject]@{Error = $null
        InboxFolder = 'Inbox'
        Watermark = [pscustomobject]@{Received = [datetime]$received
            EntryId = $id
            Folder = $folder
        }
    }
}
$recent = Sample '2026-10-13T19:00:00'
$d = Get-OutlookFreshnessDecision @($recent, $recent) $now 1440 $false $false
Assert-True ($d.Current -and $d.Diagnosis -eq 'recent-mail-outside-inbox') 'Recent filed mail must establish a stable received watermark.'
$old = Sample '2026-10-10T19:00:00'
$d = Get-OutlookFreshnessDecision @($old, $old) $now 1440 $false $false
Assert-True (-not $d.Current -and $d.Diagnosis -eq 'quiet-or-stale-unverified') 'Connected but quiet must remain unverified.'
Assert-True (-not (Get-OutlookFreshnessDecision @($recent, $recent) $now 1440 $true $false).Current) 'Offline recent cache cannot establish current coverage.'
Assert-True (-not (Get-OutlookFreshnessDecision @($recent, $recent) $now 1440 $false $true).Current) 'Disconnected Exchange cannot establish current coverage.'
Assert-True (-not (Get-OutlookFreshnessDecision @($recent, (Sample '2026-10-13T19:01:00' 'new')) $now 1440 $false $false).Current) 'Changing watermark must remain unverified.'
$future = Sample '2026-10-14T19:00:00'
Assert-True (-not (Get-OutlookFreshnessDecision @($future, $future) $now 1440 $false $false).Current) 'Future timestamp cannot establish freshness.'
Assert-True (-not (Test-OutlookInitializationReady $true 59.9 60)) 'Early COM readiness must not bypass initialization.'
Assert-True (Test-OutlookInitializationReady $true 60 60) 'Ready profile after minimum wait must succeed.'
Assert-True (-not (Test-OutlookInitializationReady $false 90 60)) 'Elapsed wait cannot replace COM readiness.'
[pscustomobject]@{assertions = $count
    status = 'passed'
} | ConvertTo-Json -Compress
