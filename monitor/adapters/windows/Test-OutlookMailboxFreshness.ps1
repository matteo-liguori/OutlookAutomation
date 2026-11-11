param(
    [Parameter(Mandatory = $true)]
    [string[]]$Store,

    [string]$Folder = 'Inbox',
    [ValidateRange(1, 1440)]
    [int]$FreshnessMinutes = 1440,
    [ValidateRange(1, 300)]
    [int]$ProbeIntervalSeconds = 15,
    [ValidateRange(0, 900)]
    [int]$MaxWaitSeconds = 180,
    [ValidateRange(1, 200)]
    [int]$MaxScan = 50,
    [ValidateRange(1, 64)]
    [int]$MaxTopicFolders = 32,
    [ValidateSet('Table', 'Json')]
    [string]$Output = 'Json'
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\OutlookAutomation.Common.ps1"
. "$PSScriptRoot\OutlookMonitoring.Common.ps1"

function Get-FolderWatermark {
    param($StoreItem, $FolderName, [int]$ScanLimit)

    $folderItem = $FolderName
    $items = $folderItem.Items
    $items.Sort('[ReceivedTime]', $true)

    for ($i = 1; $i -le $items.Count -and $i -le $ScanLimit; $i++) {
        $message = $items.Item($i)
        if ($null -eq $message -or ($message.MessageClass -notlike 'IPM.Note*' -and $message.MessageClass -notlike 'IPM.Schedule.Meeting.*')) {
            continue
        }

        return [pscustomobject]@{
            Received = [datetime]$message.ReceivedTime
            EntryId = [string]$message.EntryID
            Subject = [string]$message.Subject
            Folder = [string]$folderItem.FolderPath
            StoreId = [string]$StoreItem.StoreID
            MessageClass = [string]$message.MessageClass
        }
    }

    return $null
}

function Get-TopicFolders {
    param($StoreItem, [int]$Limit)
    $root = $StoreItem.GetRootFolder()
    $queue = New-Object System.Collections.Queue
    $checked = 0
    foreach ($child in $root.Folders) {
        if (++$checked -gt 100) {
            break
        }
        if ([string]$child.Name -eq 'Codex Triage') {
            $queue.Enqueue($child)
            break
        }
    }
    $result = @()
    while ($queue.Count -gt 0 -and $result.Count -lt $Limit) {
        $folderItem = $queue.Dequeue()
        $result += $folderItem
        foreach ($child in $folderItem.Folders) {
            if ($queue.Count + $result.Count -ge $Limit) {
                break
            }
            $queue.Enqueue($child)
        }
    }
    return $result
}

function Get-StoreTransportDiagnostic {
    param($Namespace, $StoreItem)
    $exchange = $false
    foreach ($account in $Namespace.Accounts) {
        try {
            if ([string]$account.DeliveryStore.StoreID -eq [string]$StoreItem.StoreID) {
                $exchange = ([int]$account.AccountType -eq 0)
                break
            }
        }
        catch {
        }
    }
    $mode = if ($exchange) {
        [int]$Namespace.ExchangeConnectionMode
    }
    else {
        $null
    }
    $logs = @()
    if ($exchange) {
        foreach ($folderId in @(20, 21, 22)) {
            try {
                $diagnosticFolder = $StoreItem.GetDefaultFolder($folderId)
                $items = $diagnosticFolder.Items
                $items.Sort('[LastModificationTime]', $true)
                for ($i = 1; $i -le [Math]::Min(3, $items.Count); $i++) {
                    $m = $items.Item($i)
                    $logs += [pscustomobject]@{Folder = [string]$diagnosticFolder.FolderPath
                        Modified = Convert-OutlookComDate $m.LastModificationTime
                        Subject = [string]$m.Subject
                    }
                }
            }
            catch {
            }
        }
    }
    [pscustomobject]@{Offline = [bool]$Namespace.Offline
        Exchange = $exchange
        ExchangeConnectionMode = $mode
        Disconnected = ($exchange -and $mode -in @(100, 200, 300, 400))
        RecentSyncLogHeaders = $logs
        Note = 'Connection mode and log headers are diagnostics, not proof of mailbox synchronization; no mail bodies were read.'
    }
}

$namespace = Get-OutlookAutomationNamespace
$history = @{}
$diagnostics = @{}
$folderSets = @{}
$diagnosticErrors = @{}
foreach ($storeName in $Store) {
    $history[$storeName] = @()
    $diagnosticErrors[$storeName] = @()
}

$started = Get-Date
do {
    foreach ($storeName in $Store) {
        try {
            $storeItem = Get-OutlookStoreByName $namespace $storeName
            $inbox = Find-OutlookFolder $storeItem $Folder
            if (-not $folderSets.ContainsKey($storeName)) {
                $folderSets[$storeName] = @($inbox) + @(Get-TopicFolders $storeItem $MaxTopicFolders)
            }
            $watermarks = @()
            foreach ($folderItem in $folderSets[$storeName]) {
                try {
                    $mark = Get-FolderWatermark $storeItem $folderItem $MaxScan
                    if ($mark) {
                        $watermarks += $mark
                    }
                }
                catch {
                    $diagnosticErrors[$storeName] += [string]$folderItem.FolderPath + ': ' + $_.Exception.Message
                }
            }
            $watermark = $watermarks | Sort-Object Received -Descending | Select-Object -First 1
            $inboxWatermark = $watermarks | Where-Object { $_.Folder -eq [string]$inbox.FolderPath } | Select-Object -First 1
            $diagnostics[$storeName] = Get-StoreTransportDiagnostic $namespace $storeItem
            $history[$storeName] += [pscustomobject]@{
                SampledAt = Get-Date
                Watermark = $watermark
                Error = $null
                InboxFolder = [string]$inbox.FolderPath
                InboxWatermark = $inboxWatermark
            }
        }
        catch {
            $history[$storeName] += [pscustomobject]@{
                SampledAt = Get-Date
                Watermark = $null
                Error = $_.Exception.Message
            }
        }
    }

    $allCurrent = $true
    foreach ($storeName in $Store) {
        $transport = $diagnostics[$storeName]
        $decision = Get-OutlookFreshnessDecision -Samples @($history[$storeName]) -Now (Get-Date) -FreshnessMinutes $FreshnessMinutes -Offline ([bool]$transport.Offline) -ExchangeDisconnected ([bool]$transport.Disconnected)
        if (-not $decision.Current) {
            $allCurrent = $false
            break
        }
    }
    if ($allCurrent) {
        break
    }
    $elapsed = ((Get-Date) - $started).TotalSeconds
    if ($elapsed -ge $MaxWaitSeconds) {
        break
    }

    Start-Sleep -Seconds ([Math]::Min($ProbeIntervalSeconds, [Math]::Max(1, [int]($MaxWaitSeconds - $elapsed))))
} while ($true)

$now = Get-Date
$refreshReceipt = $null
try {
    . "$PSScriptRoot/OutlookHarness.Common.ps1"
    $refreshReceipt = Invoke-OutlookHarness -Arguments @('refresh','get')
} catch { }
$rows = foreach ($storeName in $Store) {
    $samples = @($history[$storeName])
    $latestSample = $samples[-1]
    $watermark = $latestSample.Watermark

    $ageMinutes = if ($null -ne $watermark) {
        [Math]::Round(($now - $watermark.Received).TotalMinutes, 1)
    }
    else {
        $null
    }
    $transport = $diagnostics[$storeName]
    $decision = Get-OutlookFreshnessDecision -Samples $samples -Now $now -FreshnessMinutes $FreshnessMinutes -Offline ([bool]$transport.Offline) -ExchangeDisconnected ([bool]$transport.Disconnected)
    $current = $decision.Current
    $status = $decision.Status

    [pscustomobject]@{
        Store = $storeName
        CheckedAt = $now.ToUniversalTime().ToString('o')
        Folder = $Folder
        Status = $status
        Current = $current
        Samples = $samples.Count
        WaitedSeconds = [Math]::Round(($now - $started).TotalSeconds, 1)
        LatestReceived = if ($null -ne $watermark) {
            Convert-OutlookComDate $watermark.Received
        }
        else {
            $null
        }
        LatestSubject = if ($null -ne $watermark) {
            $watermark.Subject
        }
        else {
            $null
        }
        LatestAgeMinutes = $ageMinutes
        Diagnosis = $decision.Diagnosis
        LatestFolder = if ($watermark) {
            $watermark.Folder
        }
        else {
            $null
        }
        LatestEntryId = if ($watermark) {
            $watermark.EntryId
        }
        else {
            $null
        }
        LatestStoreId = if ($watermark) {
            $watermark.StoreId
        }
        else {
            $null
        }
        InboxLatestReceived = if ($latestSample.InboxWatermark) {
            Convert-OutlookComDate $latestSample.InboxWatermark.Received
        }
        else {
            $null
        }
        DiagnosticFolders = @($folderSets[$storeName] | ForEach-Object { [string]$_.FolderPath })
        DiagnosticErrors = @($diagnosticErrors[$storeName] | Select-Object -Unique)
        Transport = $transport
        LastRefresh = if ($refreshReceipt) {
            [pscustomobject]@{status = $refreshReceipt.status
                completed_at = $refreshReceipt.completed_at
                scope = $refreshReceipt.scope
            }
        }
        else {
            $null
        }
        Detail = if ($current) {
            'Two matching samples show recent received mail in the reported store/folder; this is separate from Send/Receive completion.'
        }
        elseif ($status -eq 'stale' -or $status -eq 'empty') {
            'No recent received item found in the bounded Inbox/topic-folder probe. Quiet versus stale remains unverified; connection state alone is insufficient.'
        }
        elseif ($status -eq 'not-stable') {
            'The final watermark samples differ; wait and re-check.'
        }
        elseif ($latestSample.Error) {
            $latestSample.Error
        }
        else {
            'Offline or disconnected transport; current mailbox coverage is unavailable.'
        }
    }
}

if ($Output -eq 'Json') {
    $rows | ConvertTo-Json -Depth 4
}
else {
    $rows | Format-Table -AutoSize
}
