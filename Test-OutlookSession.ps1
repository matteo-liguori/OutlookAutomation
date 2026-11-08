param([switch]$CheckCom)
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\OutlookAutomation.Common.ps1"
$state = Get-OutlookSessionState
$available = $null
$failure = $null
$count = $null
if ($CheckCom) {
    try {
        $ns = Get-OutlookAutomationNamespace
        $count = $ns.Stores.Count
        $available = ($count -gt 0)
    }
    catch {
        $available = $false
        $failure = Get-OutlookFailureKind -Exception $_.Exception -SessionState $state
    }
}
[pscustomobject]@{Session = $state
    ComAvailable = $available
    StoreCount = $count
    FailureKind = $failure
    RestartEligible = $state.RestartEligible
    NeedsDesktopReconnect = ($available -eq $false -and $failure -eq 'desktop-session-unavailable')
    NeedsPermissionReview = ($failure -eq 'access-denied')
    Note = 'Locking or disconnecting a logged-in desktop does not itself invalidate existing Outlook COM. Recovery stays in the same verified user session; authentication prompts may still require attendance.'
} | ConvertTo-Json -Depth 5
