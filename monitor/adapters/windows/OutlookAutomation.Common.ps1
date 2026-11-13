$script:OutlookFolderCache = @{}
. "$PSScriptRoot\OutlookSession.Common.ps1"
. "$PSScriptRoot\OutlookTransport.Common.ps1"

function Assert-OutlookScheduledChatDate {
    if (-not $env:CODEX_THREAD_ID) { return }
    . "$PSScriptRoot\AutomationEnvironment.Common.ps1"
    $guardPython = Get-AutomationPython
    Push-Location $script:ProjectRoot
    try {
        $result = & $guardPython -X utf8 -m monitor --format json guard
        if ($LASTEXITCODE -eq 0) {
            $notice = ($result -join "`n") | ConvertFrom-Json
            if ($notice.status -eq 'warning') { Write-Warning $notice.instruction }
        }
    }
    finally { Pop-Location }
}

function Get-OutlookAutomationNamespace {
    param([ValidateRange(1, 3)][int]$Attempts = 3)
    Assert-OutlookScheduledChatDate
    $sessionState = Get-OutlookSessionState
    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        try {
            $outlook = New-Object -ComObject Outlook.Application
            $namespace = $outlook.GetNamespace('MAPI')
            break
        }
        catch {
            if ($attempt -lt $Attempts -and (Test-OutlookTransientComFailure $_.Exception)) {
                Start-Sleep -Milliseconds 1000
                continue
            }
            $kind = Get-OutlookFailureKind -Exception $_.Exception -SessionState $sessionState
            throw [InvalidOperationException]::new("Outlook $kind (Windows session $($sessionState.SessionId), $($sessionState.ConnectionState)): $($_.Exception.Message)", $_.Exception)
        }
    }

    try {
        $namespace.Logon($null, $null, $false, $false)
    }
    catch {
        Write-Warning "Outlook profile logon did not complete cleanly: $($_.Exception.Message)"
    }

    return $namespace
}

function Convert-OutlookComDate {
    param($Value)

    if ($null -eq $Value) {
        return $null
    }

    try {
        return ([datetime]$Value).ToString('yyyy-MM-dd HH:mm:ss')
    }
    catch {
        return [string]$Value
    }
}

function Normalize-OutlookText {
    param([string]$Value)
    return (($Value -replace '\s+', ' ').Trim()).ToLowerInvariant()
}

function Normalize-OutlookSubject {
    param([string]$Value)

    $subjectText = Normalize-OutlookText $Value
    while ($subjectText -match '^(re|fw|fwd):\s*') {
        $subjectText = ($subjectText -replace '^(re|fw|fwd):\s*', '').Trim()
    }

    return $subjectText
}

function Test-OutlookSubjectsRelated {
    param([string]$A, [string]$B)

    $left = Normalize-OutlookSubject $A
    $right = Normalize-OutlookSubject $B

    if (-not $left -or -not $right) {
        return $false
    }

    if ($left -eq $right) {
        return $true
    }

    if ($left.Length -ge 14 -and $right.Contains($left)) {
        return $true
    }

    if ($right.Length -ge 14 -and $left.Contains($right)) {
        return $true
    }

    return $false
}

function Get-OutlookBodyFingerprint {
    param([string]$Value, [int]$MaxLength = 220)

    $normalizedBody = Normalize-OutlookText $Value
    if (-not $normalizedBody) {
        return ''
    }

    if ($normalizedBody.Length -gt $MaxLength) {
        return $normalizedBody.Substring(0, $MaxLength)
    }

    return $normalizedBody
}

function Get-OutlookRecipientSet {
    param([string[]]$Groups)

    $joined = ($Groups -join ';')
    $parts = $joined -split '[;,]'
    return @($parts | ForEach-Object { $_.Trim().Trim("'").ToLowerInvariant() } | Where-Object { $_ })
}

function Assert-OutlookRecipients {
    param([string[]]$Recipients, [string]$FieldName)

    foreach ($recipient in $Recipients) {
        if (-not $recipient -or -not $recipient.Trim()) {
            continue
        }

        $trimmed = $recipient.Trim().Trim("'")
        if ($trimmed -notmatch '^[^@\s]+@[^@\s]+\.[^@\s]+$') {
            throw "Invalid or incomplete recipient in $FieldName '$recipient'. Use a full email address; do not guess."
        }
    }
}

function Join-OutlookRecipients {
    param([string[]]$Recipients)
    return ($Recipients | Where-Object { $_ -and $_.Trim() } | ForEach-Object { $_.Trim().Trim("'") }) -join ';'
}

function Get-OutlookMatchingStores {
    param($Namespace, [string]$Pattern = '*')

    $matches = @()
    foreach ($storeItem in $Namespace.Stores) {
        if ($Pattern -eq '*' -or $storeItem.DisplayName -like $Pattern) {
            $matches += $storeItem
        }
    }

    return $matches
}

function Get-OutlookStoreByName {
    param($Namespace, [string]$Store)

    $matches = @($Namespace.Stores | Where-Object { $_.DisplayName -eq $Store })
    if ($matches.Count -ne 1) {
        throw "Expected exactly one Outlook store named '$Store'; found $($matches.Count)."
    }
    return $matches[0]
}

function Get-OutlookFolderMap {
    param($StoreItem, [ValidateRange(1, 2048)][int]$MaxFolders = 256)

    $cacheKey = [string]$StoreItem.StoreID
    if ($script:OutlookFolderCache.ContainsKey($cacheKey)) {
        return $script:OutlookFolderCache[$cacheKey]
    }

    $folders = @()
    $queue = New-Object System.Collections.Queue
    $queue.Enqueue($StoreItem.GetRootFolder())

    while ($queue.Count -gt 0) {
        if ($folders.Count -ge $MaxFolders) {
            throw 'Folder discovery cap reached; coverage is incomplete.'
        }
        $current = $queue.Dequeue()
        $folders += $current

        foreach ($child in $current.Folders) {
            $queue.Enqueue($child)
        }
    }

    $script:OutlookFolderCache[$cacheKey] = $folders
    return $folders
}

function Find-OutlookFolder {
    param(
        $StoreItem,
        [string]$FolderName,
        [switch]$AllowMissing
    )

    if ($FolderName -eq 'Inbox') {
        try {
            return $StoreItem.GetDefaultFolder(6)
        }
        catch {
        }
    }

    $matches = @(Get-OutlookFolderMap $StoreItem | Where-Object {
            $_.Name -eq $FolderName -or $_.FolderPath -eq $FolderName
        })

    if ($matches.Count -gt 0) {
        if ($StoreItem.DisplayName -like '*@gmail.com') {
            $gmailFolder = $matches | Where-Object { ([string]$_.FolderPath).Contains('[Gmail]') } | Select-Object -First 1
            if ($null -ne $gmailFolder) {
                return $gmailFolder
            }
        }

        return $matches[0]
    }

    if ($AllowMissing) {
        return $null
    }

    throw "Folder '$FolderName' was not found in store '$($StoreItem.DisplayName)'."
}

function Get-OutlookMessageTime {
    param($Message)

    foreach ($property in @('SentOn', 'CreationTime', 'LastModificationTime', 'ReceivedTime')) {
        try {
            $value = $Message.$property
            if ($null -ne $value) {
                return [datetime]$value
            }
        }
        catch {
        }
    }

    return $null
}

function Write-OutlookRows {
    param(
        $Rows,
        [ValidateSet('Table', 'List', 'Json', 'Csv')]
        [string]$Output = 'Json',
        [int]$Depth = 4
    )

    switch ($Output) {
        'Json' {
            if ($null -eq $Rows -or @($Rows).Count -eq 0) {
                '[]'
            }
            else {
                $Rows | ConvertTo-Json -Depth $Depth
            }
        }
        'Csv' {
            $Rows | ConvertTo-Csv -NoTypeInformation
        }
        'List' {
            $Rows | Format-List
        }
        default {
            $Rows | Format-Table -AutoSize
        }
    }
}
