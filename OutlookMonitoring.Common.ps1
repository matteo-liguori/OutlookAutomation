# Pure decisions shared by live helpers and deterministic regression tests.
function Start-OutlookInitializationTimer {
    [Diagnostics.Stopwatch]::StartNew()
}
function Test-OutlookInitializationReady {
    param([bool]$ProfileReady, [double]$ElapsedSeconds, [int]$MinimumSeconds)
    return ($ProfileReady -and $ElapsedSeconds -ge $MinimumSeconds)
}
function New-OutlookCalendarFilter {
    param([datetime]$Start, [datetime]$End)
    $culture = [Globalization.CultureInfo]::InvariantCulture
    "[Start] < '" + $End.ToString('dd MMM yyyy HH:mm', $culture) + "' AND [End] > '" + $Start.ToString('dd MMM yyyy HH:mm', $culture) + "'"
}
function Test-OutlookCalendarOverlap {
    param([datetime]$EventStart, [datetime]$EventEnd, [datetime]$Start, [datetime]$End)
    return ($EventEnd -gt $EventStart -and $EventStart -lt $End -and $EventEnd -gt $Start)
}
function Get-OutlookSyncOutcome {
    param([object[]]$Groups, [bool]$Offline, [bool]$Triggered, [double]$Elapsed, [int]$WaitSeconds, [string]$Failure = '')
    if ($Failure) {
        return 'failed'
    }
    if ($Offline) {
        return 'failed'
    }
    if (-not $Triggered -or $Groups.Count -eq 0) {
        return 'unverified'
    }
    if (@($Groups | Where-Object { @($_.Errors).Count -gt 0 }).Count) {
        return 'failed'
    }
    if (@($Groups | Where-Object { -not $_.StartedAt -or -not $_.EndedAt }).Count) {
        if ($Elapsed -ge $WaitSeconds) { return 'timeout' }
        return 'unverified'
    }
    if (@($Groups | Where-Object { $_.EndedAt -lt $_.StartedAt }).Count) {
        return 'failed'
    }
    return 'success'
}
function Get-OutlookFreshnessDecision {
    param([object[]]$Samples, [datetime]$Now, [int]$FreshnessMinutes, [bool]$Offline, [bool]$ExchangeDisconnected)
    $latest = $Samples[-1]
    $previous = if ($Samples.Count -gt 1) {
        $Samples[-2]
    }
    else {
        $null
    }
    $watermark = $latest.Watermark
    $stable = ($null -ne $watermark -and $null -ne $previous -and $null -ne $previous.Watermark -and
        $watermark.EntryId -eq $previous.Watermark.EntryId -and $watermark.Received -eq $previous.Watermark.Received -and $watermark.Folder -eq $previous.Watermark.Folder)
    $age = if ($watermark) {
        ($Now - $watermark.Received).TotalMinutes
    }
    else {
        $null
    }
    $status = if ($latest.Error) {
        'unavailable'
    }
    elseif ($Offline -or $ExchangeDisconnected) {
        'unavailable'
    }
    elseif (-not $watermark) {
        'empty'
    }
    elseif (-not $stable) {
        'not-stable'
    }
    elseif ($age -lt -5 -or $age -gt $FreshnessMinutes) {
        'stale'
    }
    else {
        'current'
    }
    $diagnosis = if ($latest.Error) {
        'probe-error'
    }
    elseif ($Offline) {
        'outlook-offline'
    }
    elseif ($ExchangeDisconnected) {
        'exchange-disconnected'
    }
    elseif ($status -eq 'current' -and $watermark.Folder -ne $latest.InboxFolder) {
        'recent-mail-outside-inbox'
    }
    elseif ($status -eq 'current') {
        'recent-inbox-mail'
    }
    elseif ($status -eq 'stale' -or $status -eq 'empty') {
        'quiet-or-stale-unverified'
    }
    else {
        $status
    }
    [pscustomobject]@{Status = $status
        Current = ($status -eq 'current')
        Stable = $stable
        AgeMinutes = $age
        Diagnosis = $diagnosis
        Watermark = $watermark
    }
}
