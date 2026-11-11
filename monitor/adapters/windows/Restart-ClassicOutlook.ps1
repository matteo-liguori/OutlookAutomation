[CmdletBinding()]
param(
    [ValidateRange(5, 120)]
    [int]$CloseTimeoutSeconds = 30,

    [ValidateRange(60, 180)]
    [int]$StartTimeoutSeconds = 75,

    [ValidateRange(60, 180)]
    [int]$MinimumInitializationSeconds = 60,

    [switch]$NoForceAfterGracefulFailure,

    [switch]$ShowWindow
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\OutlookAutomation.Common.ps1"
. "$PSScriptRoot\OutlookMonitoring.Common.ps1"
[void](Assert-OutlookRestartSession)

function Get-ClassicOutlookPath {
    $registered = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\OUTLOOK.EXE' -ErrorAction SilentlyContinue
    if ($registered -and $registered.'(default)' -and (Test-Path -LiteralPath $registered.'(default)')) {
        return [string]$registered.'(default)'
    }

    foreach ($candidate in @(
            'C:\Program Files\Microsoft Office\root\Office16\OUTLOOK.EXE',
            'C:\Program Files (x86)\Microsoft Office\root\Office16\OUTLOOK.EXE'
        )) {
        if (Test-Path -LiteralPath $candidate) {
            return $candidate
        }
    }

    throw 'Classic Outlook executable was not found.'
}

$outlookPath = Get-ClassicOutlookPath
$expectedOutlookPath = [IO.Path]::GetFullPath($outlookPath).TrimEnd('\\')
$currentSessionId = [System.Diagnostics.Process]::GetCurrentProcess().SessionId
$outlookAlreadyRestarted = $false
$existing = @(Get-Process -Name OUTLOOK -ErrorAction SilentlyContinue | Where-Object { $_.SessionId -eq $currentSessionId })
if ($existing.Count -gt 0) {
    foreach ($process in $existing) {
        Assert-OutlookClassicProcess $process $expectedOutlookPath $currentSessionId
        if ($process.MainWindowHandle -ne 0) {
            [void]$process.CloseMainWindow()
        }
    }

    $closeDeadline = (Get-Date).AddSeconds($CloseTimeoutSeconds)
    do {
        Start-Sleep -Milliseconds 500
        $existing = @(Get-Process -Name OUTLOOK -ErrorAction SilentlyContinue | Where-Object { $_.SessionId -eq $currentSessionId })
    } while ($existing.Count -gt 0 -and (Get-Date) -lt $closeDeadline)

    if ($existing.Count -gt 0) {
        if ($NoForceAfterGracefulFailure) {
            throw "Classic Outlook did not close gracefully within $CloseTimeoutSeconds seconds and forced recovery was disabled."
        }

        [void](Assert-OutlookRestartSession)

        foreach ($process in $existing) {
            Assert-OutlookClassicProcess $process $expectedOutlookPath $currentSessionId
        }

        $terminatedIds = @($existing.Id)
        foreach ($process in $existing) {
            Stop-Process -Id $process.Id -Force -ErrorAction Stop
        }
        Wait-Process -Id $existing.Id -Timeout 15 -ErrorAction SilentlyContinue
        $remaining = @(Get-Process -Name OUTLOOK -ErrorAction SilentlyContinue | Where-Object { $_.SessionId -eq $currentSessionId })
        foreach ($process in $remaining) {
            Assert-OutlookClassicProcess $process $expectedOutlookPath $currentSessionId
            if ($process.Id -in $terminatedIds) {
                throw 'Verified Classic Outlook did not terminate cleanly.'
            }
        }
        $outlookAlreadyRestarted = $remaining.Count -gt 0

        Write-Host 'Classic Outlook did not close gracefully; force-terminated the verified same-session process.'
    }
}

$initializationTimer = Start-OutlookInitializationTimer
if (-not $outlookAlreadyRestarted) {
    [void](Assert-OutlookRestartSession)
    $startArgs = @{ FilePath = $outlookPath }
    if (-not $ShowWindow) {
        $startArgs.WindowStyle = 'Hidden'
    }
    Start-Process @startArgs
}

$startDeadline = (Get-Date).AddSeconds([Math]::Max($StartTimeoutSeconds, $MinimumInitializationSeconds))
$ready = $false
do {
    Start-Sleep -Seconds 1
    $ready = $false
    try {
        $namespace = Get-OutlookAutomationNamespace
        if ($namespace -and $namespace.Stores.Count -gt 0) {
            $ready = $true
        }
    }
    catch {
    }
} while ((-not (Test-OutlookInitializationReady -ProfileReady $ready -ElapsedSeconds $initializationTimer.Elapsed.TotalSeconds -MinimumSeconds $MinimumInitializationSeconds)) -and (Get-Date) -lt $startDeadline)

if (-not (Test-OutlookInitializationReady -ProfileReady $ready -ElapsedSeconds $initializationTimer.Elapsed.TotalSeconds -MinimumSeconds $MinimumInitializationSeconds)) {
    throw "Classic Outlook did not initialise a usable COM profile within $StartTimeoutSeconds seconds."
}

Write-Host 'Classic Outlook restarted and its COM profile is ready.'
[pscustomobject]@{status = 'ready'
    success = $true
    initialized_seconds = [Math]::Round($initializationTimer.Elapsed.TotalSeconds, 2)
    minimum_initialization_seconds = $MinimumInitializationSeconds
    completed_at = [DateTimeOffset]::UtcNow.ToString('o')
} | ConvertTo-Json
