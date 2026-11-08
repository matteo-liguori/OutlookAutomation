[CmdletBinding()]
param(
    [switch]$InstallGlobalTools,
    [switch]$UpdateGlobalTools,
    [switch]$SkipPythonEnvironment
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot/AutomationEnvironment.Common.ps1"

$manifestPath = Join-Path $PSScriptRoot 'tooling.json'
$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json

if ($InstallGlobalTools -or $UpdateGlobalTools) {
    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if (-not $winget) {
        throw 'winget is required to install or update global tools.'
    }

    foreach ($tool in $manifest.globalTools) {
        $arguments = @()
        if ($UpdateGlobalTools) {
            $arguments += 'upgrade'
        }
        else {
            $arguments += 'install'
        }

        $arguments += @(
            '--id', [string]$tool.wingetId,
            '--exact',
            '--silent',
            '--accept-package-agreements',
            '--accept-source-agreements',
            '--disable-interactivity'
        )

        Write-Host "Checking $($tool.name)..."
        & $winget.Source @arguments
        if ($LASTEXITCODE -notin @(0, -1978335189)) {
            throw "winget failed for $($tool.name) with exit code $LASTEXITCODE."
        }
    }

    foreach ($resource in $manifest.globalPowerShellResources) {
        $installed = Get-GlobalPowerShellResource ([string]$resource.name)
        $needsInstall = (-not $installed) -or ([version]$installed.Version -lt [version]$resource.minimumVersion)

        if ($UpdateGlobalTools -or $needsInstall) {
            Write-Host "Installing current-user PowerShell resource $($resource.name)..."
            $installResource = Get-Command Install-PSResource -ErrorAction SilentlyContinue
            if ($installResource) {
                Install-PSResource -Name ([string]$resource.name) -Scope CurrentUser -TrustRepository -Quiet -ErrorAction Stop
            }
            else {
                Set-PSRepository -Name PSGallery -InstallationPolicy Trusted
                Install-Module -Name ([string]$resource.name) -Scope CurrentUser -Force -AllowClobber -ErrorAction Stop
            }
        }
    }
}

if (-not $SkipPythonEnvironment) {
    $python = Get-AutomationPython
    if (-not $python) {
        throw 'A supported Python runtime was not found. Re-run with -InstallGlobalTools, then open a new terminal.'
    }

    $venvPath = Join-Path $PSScriptRoot ([string]$manifest.projectEnvironment.venv)
    $venvPython = Join-Path $venvPath 'Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $venvPython)) {
        Write-Host "Creating project environment at $venvPath..."
        & $python -m venv $venvPath
        if ($LASTEXITCODE -ne 0) {
            throw "Python failed to create $venvPath."
        }
    }

    $requirements = Join-Path $PSScriptRoot ([string]$manifest.projectEnvironment.requirements)
    Write-Host 'Installing exact project-local Python dependencies...'
    & $venvPython -m pip install --disable-pip-version-check -r $requirements
    if ($LASTEXITCODE -ne 0) {
        throw 'pip failed to install the locked project dependencies.'
    }
}

Write-Host ''
& (Join-Path $PSScriptRoot 'Test-OutlookAutomationEnvironment.ps1')
