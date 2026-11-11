[CmdletBinding()]
param(
    [switch]$LiveOutlook,
    [ValidateRange(5, 120)]
    [int]$TimeoutSeconds = 20
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot/AutomationEnvironment.Common.ps1"
$results = New-Object System.Collections.ArrayList

function Add-Check {
    param(
        [string]$Status,
        [string]$Name,
        [string]$Detail
    )

    $null = $results.Add([pscustomobject]@{
            Status = $Status
            Check = $Name
            Detail = $Detail
        })
}

function Get-SourcePowerShellFiles {
    $files = @(Get-ChildItem -LiteralPath $PSScriptRoot -File -Filter '*.ps1' -ErrorAction Stop)
    $excludedDirectories = @('.git', '.local', '.venv', '__pycache__', 'archive', 'tmp', 'output', 'diagnostics')

    foreach ($directory in @(Get-ChildItem -LiteralPath $PSScriptRoot -Directory -ErrorAction Stop)) {
        if ($directory.Name -in $excludedDirectories) {
            continue
        }

        $files += @(Get-ChildItem -LiteralPath $directory.FullName -Recurse -File -Filter '*.ps1' -ErrorAction SilentlyContinue)
    }

    return @($files | Sort-Object FullName -Unique)
}

$requiredFiles = @(
    'AGENTS.md',
    'monitor/adapters/windows/OutlookAutomation.Common.ps1',
    'monitor/adapters/windows/Read-OutlookMail.ps1',
    'monitor/adapters/windows/Send-OutlookStoreMailSafe.ps1',
    'monitor/adapters/windows/Test-OutlookSendDuplicate.ps1',
    'packaging/tooling.json',
    'packaging/requirements/requirements.lock'
)
$tooling = Get-Content -LiteralPath (Join-Path $script:ProjectRoot 'packaging/tooling.json') -Raw | ConvertFrom-Json

$missing = @($requiredFiles | Where-Object { -not (Test-Path -LiteralPath (Join-Path $script:ProjectRoot $_)) })
if ($missing.Count -eq 0) {
    Add-Check 'PASS' 'Tracked runtime files' 'All required source and manifest files are present.'
}
else {
    Add-Check 'FAIL' 'Tracked runtime files' ("Missing: " + ($missing -join ', '))
}

$parseErrors = @()
foreach ($file in (Get-SourcePowerShellFiles)) {
    $tokens = $null
    $errors = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile($file.FullName, [ref]$tokens, [ref]$errors)
    foreach ($errorItem in $errors) {
        $parseErrors += "$($file.Name): $($errorItem.Message)"
    }
}

if ($parseErrors.Count -eq 0) {
    Add-Check 'PASS' 'PowerShell syntax' 'Every PowerShell file parses successfully.'
}
else {
    Add-Check 'FAIL' 'PowerShell syntax' ($parseErrors -join '; ')
}

foreach ($commandName in @('powershell.exe', 'pwsh.exe', 'git.exe', 'gh.exe')) {
    $command = Get-Command $commandName -ErrorAction SilentlyContinue
    if ($command) {
        Add-Check 'PASS' $commandName $command.Source
    }
    else {
        Add-Check 'WARN' $commandName 'Not found on PATH; run Setup-OutlookAutomation.ps1 -InstallGlobalTools.'
    }
}

foreach ($resource in $tooling.globalPowerShellResources) {
    $installed = Get-GlobalPowerShellResource ([string]$resource.name)
    if ($installed -and [version]$installed.Version -ge [version]$resource.minimumVersion) {
        Add-Check 'PASS' ([string]$resource.name) "$($installed.Version) at $($installed.ModuleBase)"
    }
    else {
        Add-Check 'WARN' ([string]$resource.name) "Version $($resource.minimumVersion) or newer is not available; run setup with -InstallGlobalTools."
    }
}

$venvPython = Join-Path $script:ProjectRoot '.venv\Scripts\python.exe'
try {
    $python = Get-AutomationPython
}
catch {
    $python = $null
} # Missing Python is a reported setup warning.
if (-not $python) {
    Add-Check 'WARN' 'Python' 'No usable Python runtime found.'
}
elseif (-not (Test-Path -LiteralPath $venvPython)) {
    Add-Check 'WARN' 'Python environment' "Global Python exists at $python; run Setup-OutlookAutomation.ps1 to create .venv."
}
else {
    $version = (& $venvPython --version 2>&1 | Out-String).Trim()
    & $venvPython -c 'import PIL, pypdf, pypdfium2, caldav, icalendar' 2>$null
    if ($LASTEXITCODE -eq 0) {
        Add-Check 'PASS' 'Python environment' "$version; PDF and calendar dependencies import successfully."
    }
    else {
        Add-Check 'FAIL' 'Python environment' 'The local environment exists but its PDF/calendar dependencies do not import.'
    }
}

$outlookRegistration = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\OUTLOOK.EXE' -ErrorAction SilentlyContinue
$outlookPath = if ($outlookRegistration) {
    [string]$outlookRegistration.'(default)'
}
else {
    ''
}
if ($outlookPath -and (Test-Path -LiteralPath $outlookPath)) {
    $outlookVersion = (Get-Item -LiteralPath $outlookPath).VersionInfo.FileVersion
    Add-Check 'PASS' 'Classic Outlook' "$outlookPath ($outlookVersion)"
}
else {
    Add-Check 'FAIL' 'Classic Outlook' 'Classic Outlook is required; New Outlook does not expose the COM API used here.'
}

$profiles = @(Get-ChildItem 'HKCU:\Software\Microsoft\Office\16.0\Outlook\Profiles' -ErrorAction SilentlyContinue)
if ($profiles.Count -gt 0) {
    Add-Check 'PASS' 'Classic Outlook profile' (($profiles | ForEach-Object { $_.PSChildName }) -join ', ')
}
else {
    Add-Check 'WARN' 'Classic Outlook profile' 'No profile key found. Open Classic Outlook and add the intended accounts.'
}

$newOutlook = @(Get-Process olk -ErrorAction SilentlyContinue)
if ($newOutlook.Count -gt 0) {
    Add-Check 'WARN' 'Active Outlook app' 'New Outlook is running. Mailbox automation requires a separately configured Classic Outlook profile.'
}
else {
    Add-Check 'PASS' 'Active Outlook app' 'New Outlook is not currently running.'
}

if ($LiveOutlook) {
    $inspectScript = Join-Path $PSScriptRoot 'Inspect-Outlook.ps1'
    $stdoutPath = [System.IO.Path]::GetTempFileName()
    $stderrPath = [System.IO.Path]::GetTempFileName()
    $process = $null
    $liveSucceeded = $false

    try {
        $process = Start-Process -FilePath 'powershell.exe' -ArgumentList @(
            '-NoProfile',
            '-ExecutionPolicy', 'Bypass',
            '-File', ('"' + $inspectScript + '"'),
            '-Action', 'Stores',
            '-Output', 'Json'
        ) -PassThru -WindowStyle Hidden -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath

        if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
            Add-Check 'FAIL' 'Live Outlook COM' "Timed out after $TimeoutSeconds seconds. Open Classic Outlook, finish account setup, and let it sync before retrying."
        }
        else {
            $storeJson = [string](Get-Content -LiteralPath $stdoutPath -Raw)
            $stores = $null
            if (-not [string]::IsNullOrWhiteSpace($storeJson)) {
                try {
                    $stores = @($storeJson | ConvertFrom-Json)
                }
                catch {
                }
            }

            if ($stores -and $stores.Count -gt 0) {
                $storeNames = @($stores | ForEach-Object { [string]$_.DisplayName })
                Add-Check 'PASS' 'Live Outlook COM' ("Classic Outlook returned: " + ($storeNames -join ', '))
                $liveSucceeded = $true

                $profile = Get-OutlookAutomationProfile
                $expectedStores = @($profile.mail.PSObject.Properties | ForEach-Object { [string]$_.Value })
                $missingStores = @($expectedStores | Where-Object { $_ -notin $storeNames })
                if ($missingStores.Count -eq 0) {
                    Add-Check 'PASS' 'Expected Outlook stores' 'Every store in tooling.json is available.'
                }
                else {
                    Add-Check 'FAIL' 'Expected Outlook stores' ("Missing from Classic Outlook: " + ($missingStores -join ', '))
                }
            }
            else {
                $errorText = [string](Get-Content -LiteralPath $stderrPath -Raw -ErrorAction SilentlyContinue)
                if ([string]::IsNullOrWhiteSpace($errorText)) {
                    $errorText = "Inspect-Outlook.ps1 exited with code $($process.ExitCode) and returned no usable store data."
                }
                Add-Check 'FAIL' 'Live Outlook COM' $errorText
            }
        }
    }
    finally {
        if (-not $liveSucceeded) {
            if ($process -and -not $process.HasExited) {
                Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
            }

            # COM activation does not prove ownership of a newly observed Outlook
            # process. Only the diagnostic's own PowerShell worker is stopped.
        }
        Remove-Item -LiteralPath $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue
    }
}

$results | Format-Table -AutoSize -Wrap

if (@($results | Where-Object { $_.Status -eq 'FAIL' }).Count -gt 0) {
    exit 1
}
