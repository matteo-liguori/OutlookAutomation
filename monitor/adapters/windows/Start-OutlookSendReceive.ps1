param(
    [ValidateRange(60, 600)]
    [int]$WaitSeconds = 300,
    [string]$ResultPath = ''
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\OutlookAutomation.Common.ps1"
. "$PSScriptRoot\OutlookMonitoring.Common.ps1"

$started = [DateTimeOffset]::UtcNow
$timer = [Diagnostics.Stopwatch]::StartNew()
$observers = @()
$groups = @()
$failure = ''
$triggered = $false
$offline = $false
try {
    $namespace = Get-OutlookAutomationNamespace
    $offline = [bool]$namespace.Offline
    if ($offline) {
        throw 'Outlook is offline; Send/Receive cannot be verified.'
    }
    if (-not ('OutlookSyncObserver' -as [type])) {
        Add-Type -Path (Join-Path $PSScriptRoot 'OutlookSyncObserver.cs')
    }
    $syncs = @($namespace.SyncObjects | ForEach-Object { $_ })
    if ($syncs.Count -eq 0) {
        throw 'No Send/Receive groups expose observable completion.'
    }
    foreach ($sync in $syncs) {
        $observers += [OutlookSyncObserver]::new($sync, [string]$sync.Name)
    }
    # Subscribe before triggering, and start each group only once. Namespace
    # SendAndReceive starts the configured groups; a second Start races events.
    $namespace.SendAndReceive($false)
    $triggered = $true
    $timer.Restart()
    while ($timer.Elapsed.TotalSeconds -lt $WaitSeconds) {
        [OutlookSyncObserver]::Pump()
        $outcome = Get-OutlookSyncOutcome -Groups $observers -Offline $offline -Triggered $triggered -Elapsed $timer.Elapsed.TotalSeconds -WaitSeconds $WaitSeconds
        if ($outcome -in @('success', 'failed')) { break }
        Start-Sleep -Milliseconds 100
    }
    [OutlookSyncObserver]::Pump()
    $offline = [bool]$namespace.Offline
}
catch {
    $failure = $_.Exception.Message
}
finally {
    foreach ($observer in $observers) {
        $groups += [pscustomobject]@{Name = $observer.Name
            StartedAt = $observer.StartedAt
            EndedAt = $observer.EndedAt
            Errors = @($observer.Errors)
        }
        try {
            $observer.Dispose()
        }
        catch {
            if (-not $failure) {
                $failure = 'Could not clean up sync observer: ' + $_.Exception.Message
            }
        }
    }
}
$status = Get-OutlookSyncOutcome -Groups $groups -Offline $offline -Triggered $triggered -Elapsed $timer.Elapsed.TotalSeconds -WaitSeconds $WaitSeconds -Failure $failure
if (-not $failure -and $status -eq 'timeout') {
    $failure = 'No complete SyncStart/SyncEnd sequence was observed for every configured group within the bounded wait.'
}
if (-not $failure -and $status -eq 'failed') {
    $failure = (@($groups | ForEach-Object { $_.Errors }) -join '; ')
    if (-not $failure) {
        $failure = 'Outlook became offline during synchronization.'
    }
}
$result = [pscustomobject]@{schema = 1
    status = $status
    success = ($status -eq 'success')
    started_at = $started.ToString('o')
    completed_at = [DateTimeOffset]::UtcNow.ToString('o')
    wait_seconds = $WaitSeconds
    elapsed_seconds = [Math]::Round($timer.Elapsed.TotalSeconds, 2)
    triggered = $triggered
    offline = $offline
    groups = $groups
    error = $failure
    scope = 'Observed configured Send/Receive groups; per-store freshness remains a separate required check.'
}
$json = $result | ConvertTo-Json -Depth 7
try {
    if (-not $ResultPath) {
        . "$PSScriptRoot/OutlookHarness.Common.ps1"
        Invoke-OutlookHarness -Arguments @('refresh','put','--input','-') -InputObject $result | Out-Null
    } else {
    $parent = Split-Path -Parent $ResultPath
    if ($parent) {
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
    }
    $temporary = $ResultPath + '.' + [guid]::NewGuid().ToString('N') + '.tmp'
    $json | Set-Content -LiteralPath $temporary -Encoding utf8
    Move-Item -LiteralPath $temporary -Destination $ResultPath -Force
    }
}
catch {
    $result.status = 'failed'
    $result.success = $false
    $result.error = 'Could not record refresh result: ' + $_.Exception.Message
    $json = $result | ConvertTo-Json -Depth 7
}
Write-Output $json
if (-not $result.success) {
    throw "Outlook Send/Receive $($result.status): $($result.error)"
}
Write-Host 'Outlook send/receive completed successfully.'
