$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '../OutlookSession.Common.ps1')
. (Join-Path $PSScriptRoot '../OutlookMessageLookup.ps1')
$script:count = 0
function Assert($value, $why) {
    if (-not $value) {
        throw $why
    }
    $script:count++
}
$active = Get-OutlookSessionDecision -SessionId 2 -State 0 -UserLoggedOn $true -Locked $true -Remote $false
Assert $active.RestartEligible 'Locked logged-in session must remain eligible'
$remote = Get-OutlookSessionDecision -SessionId 2 -State 4 -UserLoggedOn $true -Locked $true -Remote $true
Assert $remote.RestartEligible 'Disconnected logged-in remote session must remain eligible'
Assert (-not (Get-OutlookSessionDecision -SessionId 0 -State 0 -UserLoggedOn $true).RestartEligible) 'Service session denied'
Assert (-not (Get-OutlookSessionDecision -SessionId 2 -State 4 -UserLoggedOn $false).RestartEligible) 'Logged-out session denied'
$denied = [System.Runtime.InteropServices.COMException]::new('denied', -2147024891)
$lost = [System.Runtime.InteropServices.COMException]::new('lost', -2147023584)
$busy = [System.Runtime.InteropServices.COMException]::new('busy', -2147417846)
Assert ((Get-OutlookFailureKind $denied $active) -eq 'access-denied') 'Access denied classification'
Assert ((Get-OutlookFailureKind $lost $active) -eq 'desktop-session-unavailable') 'Lost session classification'
Assert (Test-OutlookTransientComFailure $busy) 'Busy COM is retryable'
Assert (-not (Test-OutlookTransientComFailure $denied)) 'Access denied is not transient'
$parent = [pscustomobject]@{StoreID = 'store-1'
    FolderPath = 'Inbox'
    Store = [pscustomobject]@{DisplayName = 'personal' }
}
$target = [pscustomobject]@{EntryID = 'EXACT'
    Body = 'verified'
    UnRead = $true
    Parent = $parent
}
$other = [pscustomobject]@{EntryID = 'OTHER'
    Subject = 'same subject'
    Parent = $parent
}
$items = [pscustomobject]@{Values = @($other, $target)
    Count = 2
}
$items | Add-Member ScriptMethod Sort { param($key, $reverse) }
$items | Add-Member ScriptMethod Item { param($index)
    $this.Values[$index - 1] }
$folder = [pscustomobject]@{Name = 'Inbox'
    FolderPath = 'Inbox'
    Items = $items
    Folders = @()
}
$store = [pscustomobject]@{StoreID = 'store-1'
    DisplayName = 'personal'
    Root = $folder
}
$store | Add-Member ScriptMethod GetRootFolder { $this.Root }
$ns = [pscustomobject]@{Stores = @($store) }
$ns | Add-Member ScriptMethod GetItemFromID { param($id, $sid)
    throw 'fixture direct failure' }
$resolved = Resolve-OutlookExactMessage $ns 'exact' 'store-1' -MaxScan 10
Assert ($resolved.ReadPath -eq 'bounded-folder-exact-id') 'Fallback path'
Assert ($resolved.Message.Body -eq 'verified') 'Exact message body'
Assert $target.UnRead 'Unread state preserved'
try {
    Resolve-OutlookExactMessage $ns 'exact' 'wrong'
    throw 'unexpected success'
}
catch {
    Assert ($_.Exception.Message -match 'verified store') 'Wrong store rejected'
}
try {
    Resolve-OutlookExactMessage $ns 'exact' 'store-1' -MaxScan 1
    throw 'unexpected success'
}
catch {
    Assert ($_.Exception.Message -match '1 items') 'Scan cap enforced'
}
try {
    Resolve-OutlookExactMessage $ns 'moved' 'store-1'
    throw 'unexpected success'
}
catch {
    Assert ($_.Exception.Message -match 'no subject/sender substitution') 'No guessed locator'
}
$direct = [pscustomobject]@{Target = $target }
$direct | Add-Member ScriptMethod GetItemFromID { param($id, $sid)
    $this.Target }
Assert ((Resolve-OutlookExactMessage $direct 'EXACT' 'store-1').ReadPath -eq 'direct-id') 'Direct path retained'
. (Join-Path $PSScriptRoot '../OutlookAutomation.Common.ps1')
function Get-OutlookSessionState {
    [pscustomobject]@{Status = 'disconnected'
        SessionId = 2
        ConnectionState = 'disconnected'
    }
}
$global:accessCalls = 0
$global:accessSleeps = 0
$global:accessNamespace = [pscustomobject]@{Ready = $true }
$global:accessNamespace | Add-Member ScriptMethod Logon { param($profile, $password, $dialog, $newSession)
    if ($dialog -or $newSession) {
        throw 'Unexpected interactive logon'
    } }
$global:accessApplication = [pscustomobject]@{Namespace = $global:accessNamespace }
$global:accessApplication | Add-Member ScriptMethod GetNamespace { param($name)
    $this.Namespace }
function New-Object {
    param($ComObject)
    $global:accessCalls++
    if ($global:accessCalls -lt 3) {
        throw [System.Runtime.InteropServices.COMException]::new('busy', -2147417846)
    }
    $global:accessApplication
}
function Start-Sleep {
    param($Milliseconds)
    $global:accessSleeps++
}
Assert ((Get-OutlookAutomationNamespace).Ready) 'Disconnected session reuses usable COM after bounded transient retries'
Assert ($global:accessCalls -eq 3 -and $global:accessSleeps -eq 2) 'Transient retry cap'
$global:accessCalls = 0
function New-Object {
    param($ComObject)
    $global:accessCalls++
    throw [System.Runtime.InteropServices.COMException]::new('denied', -2147024891)
}
try {
    Get-OutlookAutomationNamespace
    throw 'unexpected success'
}
catch {
    Assert ($_.Exception.Message -match 'access-denied') 'Permission error preserved'
}
Assert ($global:accessCalls -eq 1) 'Permission error not retried'
@{assertions = $script:count } | ConvertTo-Json -Compress
