function Get-OutlookSessionDecision {
    param([int]$SessionId, [int]$State, [bool]$UserLoggedOn, $Locked, $Remote)
    $status = if ($SessionId -gt 0 -and $UserLoggedOn -and $State -eq 0) {
        'active'
    }
    elseif ($SessionId -gt 0 -and $UserLoggedOn -and $State -eq 4) {
        'disconnected'
    }
    else {
        'unavailable'
    }
    [pscustomobject]@{Status = $status
        RestartEligible = ($status -in @('active', 'disconnected'))
        Locked = $Locked
        Remote = $Remote
        UserLoggedOn = $UserLoggedOn
        PreferExistingOutlook = $true
    }
}
function Get-OutlookSessionState {
    $sid = [Diagnostics.Process]::GetCurrentProcess().SessionId
    try {
        if (-not ('OutlookSessionState' -as [type])) {
            Add-Type -Path (Join-Path $PSScriptRoot 'OutlookSessionState.cs')
        }
        $state = [OutlookSessionState]::ConnectionState($sid)
        $names = @('active', 'connected', 'connecting', 'shadow', 'disconnected', 'idle', 'listen', 'reset', 'down', 'initializing')
        $name = if ($state -ge 0 -and $state -lt $names.Count) {
            $names[$state]
        }
        else {
            'unknown'
        }
        $decision = Get-OutlookSessionDecision -SessionId $sid -State $state -UserLoggedOn ([OutlookSessionState]::UserLoggedOn($sid)) -Locked ([OutlookSessionState]::Locked($sid)) -Remote ([OutlookSessionState]::Remote($sid))
        [pscustomobject]@{Status = $decision.Status
            SessionId = $sid
            ConnectionState = $name
            RestartEligible = $decision.RestartEligible
            UserLoggedOn = $decision.UserLoggedOn
            Locked = $decision.Locked
            Remote = $decision.Remote
            PreferExistingOutlook = $true
            CheckedAt = [DateTimeOffset]::UtcNow.ToString('o')
            Error = $null
        }
    }
    catch {
        [pscustomobject]@{Status = 'unknown'
            SessionId = $sid
            ConnectionState = 'unknown'
            RestartEligible = $false
            CheckedAt = [DateTimeOffset]::UtcNow.ToString('o')
            Error = $_.Exception.Message
        }
    }
}
function Get-OutlookFailureKind {
    param($Exception, $SessionState)
    $current = $Exception
    while ($current) {
        $code = ([long]$current.HResult) -band 0xffffffffL
        if ($code -eq 0x80070005L) {
            return 'access-denied'
        }
        if ($code -eq 0x80070520L) {
            return 'desktop-session-unavailable'
        }
        $current = $current.InnerException
    }
    if ($SessionState.Status -eq 'unavailable') {
        return 'desktop-session-unavailable'
    }
    return 'outlook-com-unavailable'
}
function Assert-OutlookRestartSession {
    $state = Get-OutlookSessionState
    if (-not $state.RestartEligible) {
        throw "Outlook recovery blocked: desktop-session-$($state.Status) (session $($state.SessionId), $($state.ConnectionState)). A verified logged-in user session is required; no process was stopped or started by this check."
    }
    return $state
}
function Test-OutlookTransientComFailure {
    param($Exception)
    $current = $Exception
    while ($current) {
        $code = ([long]$current.HResult) -band 0xffffffffL
        if ($code -in @(0x80010001L, 0x8001010AL, 0x800706BAL)) {
            return $true
        }
        $current = $current.InnerException
    }
    return $false
}

function Assert-OutlookClassicProcess {
    param($Process, [string]$ExpectedPath, [int]$SessionId)
    if ($SessionId -le 0 -or $Process.SessionId -ne $SessionId -or $Process.ProcessName -ne 'OUTLOOK') {
        throw 'Refusing to touch a process outside the verified Classic Outlook user session.'
    }
    $path = [string]$Process.Path
    if (-not $path) {
        $path = [string](Get-CimInstance Win32_Process -Filter "ProcessId=$($Process.Id)" -ErrorAction Stop).ExecutablePath
    }
    if (-not $path -or -not ([IO.Path]::GetFullPath($path)).Equals([IO.Path]::GetFullPath($ExpectedPath), [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Refusing to touch an Outlook process with an unverified executable path.'
    }
}
