param([Parameter(Mandatory = $true)][datetime]$Start, [Parameter(Mandatory = $true)][datetime]$End, [ValidateRange(1, 5000)][int]$Limit = 300)
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\OutlookAutomation.Common.ps1"
. "$PSScriptRoot\OutlookMonitoring.Common.ps1"
if ($End -le $Start -or ($End - $Start).TotalDays -gt 31) {
    throw 'Calendar window must be 1..31 days.'
}
$ns = Get-OutlookAutomationNamespace
. "$PSScriptRoot/AutomationEnvironment.Common.ps1"
$profile = Get-OutlookAutomationProfile
$calendarSource = if ($profile.roles.work_calendar) { [string]$profile.roles.work_calendar } else { 'calendar:work' }
$mailSource = if ($profile.roles.work_mail) { [string]$profile.roles.work_mail } else { 'mail:work' }
if ($calendarSource -notin $profile.calendars) { throw 'Work calendar is not enabled in the local profile.' }
$storeItem = Get-OutlookStoreByName $ns ([string]$profile.mail.$mailSource)
$folder = $storeItem.GetDefaultFolder(9)
$items = $folder.Items
$items.Sort('[Start]')
$items.IncludeRecurrences = $true
$filter = New-OutlookCalendarFilter -Start $Start -End $End
$restricted = $items.Restrict($filter)
$events = @()
$errors = @()
$scanned = 0
$item = $restricted.GetFirst()
while ($null -ne $item -and $scanned -lt $Limit) {
    $scanned++
    $eventStart = [datetime]$item.Start
    $eventEnd = [datetime]$item.End
    if (-not (Test-OutlookCalendarOverlap -EventStart $eventStart -EventEnd $eventEnd -Start $Start -End $End)) {
        $errors += "Out-of-window or invalid event rejected: $([string]$item.EntryID)"
    }
    else {
        $events += [pscustomobject]@{id = [string]$item.EntryID
            title = [string]$item.Subject
            start = $eventStart.ToUniversalTime().ToString('o')
            end = $eventEnd.ToUniversalTime().ToString('o')
            location = [string]$item.Location
            all_day = [bool]$item.AllDayEvent
        }
    }
    $item = $restricted.GetNext()
}
if ($null -ne $item) {
    $errors += 'Calendar scan limit reached.'
}
[pscustomobject]@{source = $calendarSource
    complete = ($null -eq $item -and $errors.Count -eq 0)
    events = $events
    start = $Start.ToString('o')
    end = $End.ToString('o')
    scanned = $scanned
    errors = $errors
    filter = $filter
} | ConvertTo-Json -Depth 5
