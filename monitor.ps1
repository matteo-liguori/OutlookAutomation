# Explicit runtime override is useful when the project venv cannot launch in a sandbox.
[CmdletBinding(PositionalBinding = $false)]
param([string]$Python = '', [Parameter(Position = 0, ValueFromRemainingArguments = $true)][string[]]$MonitorArgs)
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot/monitor/adapters/windows/AutomationEnvironment.Common.ps1"
$Python = Get-AutomationPython -PythonOverride $Python
Push-Location $PSScriptRoot
try {
    & $Python -X utf8 -m monitor @MonitorArgs
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
