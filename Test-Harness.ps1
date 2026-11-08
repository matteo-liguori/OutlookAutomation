# Offline tests only. No Outlook instance, mailbox or calendar is mutated.
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot/AutomationEnvironment.Common.ps1"
$python = Get-AutomationPython -RequiredModules @('caldav', 'icalendar')
$testTemp = Join-Path (Get-OutlookAutomationDataRoot) 'monitor/test-temp'
New-Item -ItemType Directory -Path $testTemp -Force | Out-Null
$oldTemp = $env:TEMP
$oldTmp = $env:TMP
$oldProfile = $env:OUTLOOK_AUTOMATION_PROFILE
Push-Location $PSScriptRoot
try {
    $env:TEMP = $testTemp
    $env:TMP = $testTemp
    # Full synthetic topology makes regression tests independent of user accounts.
    $env:OUTLOOK_AUTOMATION_PROFILE = Join-Path $PSScriptRoot 'profile.example.json'
    & $python -X utf8 -m unittest discover -s tests @args
    if ($LASTEXITCODE -ne 0) {
        throw 'Harness regression tests failed.'
    }
}
finally {
    Pop-Location
    $env:TEMP = $oldTemp
    $env:TMP = $oldTmp
    $env:OUTLOOK_AUTOMATION_PROFILE = $oldProfile
}
