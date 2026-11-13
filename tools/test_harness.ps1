# Offline tests, then an installed-configuration smoke check. No mail/calendar calls.
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot/../monitor/adapters/windows/AutomationEnvironment.Common.ps1"
$python = Get-AutomationPython -RequiredModules @('caldav', 'icalendar')
$testTemp = Join-Path (Get-OutlookAutomationDataRoot) 'monitor/test-temp'
New-Item -ItemType Directory -Path $testTemp -Force | Out-Null
$oldTemp = $env:TEMP
$oldTmp = $env:TMP
$oldProfile = $env:OUTLOOK_AUTOMATION_PROFILE
Push-Location $script:ProjectRoot
try {
    $env:TEMP = $testTemp
    $env:TMP = $testTemp
    # Full synthetic topology makes regression tests independent of user accounts.
    $env:OUTLOOK_AUTOMATION_PROFILE = Join-Path $script:ProjectRoot 'tests/fixtures/profile.example.json'
    & $python -X utf8 -m unittest discover -s tests @args
    if ($LASTEXITCODE -ne 0) {
        throw 'Harness regression tests failed.'
    }
    $env:OUTLOOK_AUTOMATION_PROFILE = $oldProfile
    & $python -X utf8 tools/smoke_harness.py
    if ($LASTEXITCODE -ne 0) {
        throw 'Installed harness smoke check failed; do not report success or publish.'
    }
}
finally {
    Pop-Location
    $env:TEMP = $oldTemp
    $env:TMP = $oldTmp
    $env:OUTLOOK_AUTOMATION_PROFILE = $oldProfile
}
