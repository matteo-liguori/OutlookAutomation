$script:ProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../..'))
if (-not (Test-Path -LiteralPath (Join-Path $script:ProjectRoot 'monitor/__init__.py'))) { $script:ProjectRoot = $PSScriptRoot }
# Runtime discovery is shared by setup, diagnostics, monitoring and CalDAV.
function Get-OutlookAutomationDataRoot {
    if ($env:OUTLOOK_AUTOMATION_DATA) { $path = $env:OUTLOOK_AUTOMATION_DATA }
    elseif (Test-Path -LiteralPath (Join-Path $script:ProjectRoot '.local/data-root.txt')) {
        $path = (Get-Content -LiteralPath (Join-Path $script:ProjectRoot '.local/data-root.txt') -Raw).Trim()
    }
    else { return (Join-Path $script:ProjectRoot '.local') }
    if (-not [System.IO.Path]::IsPathRooted($path)) { throw 'The private data directory must be an absolute path.' }
    [System.IO.Path]::GetFullPath($path)
}
function Get-OutlookAutomationProfile {
    $path = if ($env:OUTLOOK_AUTOMATION_PROFILE) { $env:OUTLOOK_AUTOMATION_PROFILE } else { Join-Path (Get-OutlookAutomationDataRoot) 'monitor/profile.json' }
    if (-not (Test-Path -LiteralPath $path)) {
        throw 'Create your local profile with Initialize-OutlookAutomation.ps1 first.'
    }
    Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
}

function Get-AutomationPython {
    param([string]$PythonOverride = '', [string[]]$RequiredModules = @())
    $candidates = @()
    if ($PythonOverride) {
        $candidates = @($PythonOverride)
    }
    else {
        $config = Join-Path (Get-OutlookAutomationDataRoot) 'monitor/runtime.json'
        if (Test-Path -LiteralPath $config) {
            $candidates += [string](Get-Content -LiteralPath $config -Raw | ConvertFrom-Json).python
        }
        $candidates += Join-Path $script:ProjectRoot '.venv/Scripts/python.exe'
        $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
        if ($pythonCommand -and $pythonCommand.Source -notlike '*\WindowsApps\*') {
            $candidates += $pythonCommand.Source
        }
        $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
        if ($launcher) {
            $resolved = & $launcher.Source -3 -c 'import sys; print(sys.executable)' 2>$null
            if ($LASTEXITCODE -eq 0) {
                $candidates += [string]$resolved
            }
        }
    }
    foreach ($candidate in @($candidates | Where-Object { $_ } | Select-Object -Unique)) {
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            continue
        }
        if ($RequiredModules.Count) {
            foreach ($module in $RequiredModules) {
                if ($module -notmatch '^[A-Za-z_][A-Za-z0-9_.]*$') {
                    throw 'Invalid required Python module.'
                }
            }
            $imports = ($RequiredModules | ForEach-Object { "import $_" }) -join '; '
            & $candidate -c $imports 2>$null
            if ($LASTEXITCODE -ne 0) {
                continue
            }
        }
        return $candidate
    }
    throw 'No suitable Python runtime found. Run Setup-OutlookAutomation.ps1 or pass a verified runtime explicitly.'
}
function Get-GlobalPowerShellResource {
    param([string]$Name)
    $installed = Get-Module -ListAvailable -Name $Name | Sort-Object Version -Descending | Select-Object -First 1
    if ($installed) {
        return $installed
    }
    $pwsh = Get-Command pwsh.exe -ErrorAction SilentlyContinue
    if ($pwsh) {
        $escaped = $Name.Replace("'", "''")
        $json = & $pwsh.Source -NoProfile -Command "Get-Module -ListAvailable -Name '$escaped' | Sort-Object Version -Descending | Select-Object -First 1 Name,@{Name='Version';Expression={`$_.Version.ToString()}},ModuleBase | ConvertTo-Json -Compress" 2>$null
        if ($LASTEXITCODE -eq 0 -and $json) {
            return ($json | ConvertFrom-Json)
        }
    }
}
