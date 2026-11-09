# Structured stdin/stdout bridge. Never creates intermediate payload files.
function Invoke-OutlookHarness {
    param([Parameter(Mandatory)][string[]]$Arguments, $InputObject, [string]$Database = '')
    . "$PSScriptRoot/AutomationEnvironment.Common.ps1"
    $runtime = Get-AutomationPython
    $cli = @('-X', 'utf8', '-m', 'monitor', '--format', 'json')
    if (-not $Database -and $env:OUTLOOK_AUTOMATION_DATABASE) { $Database = $env:OUTLOOK_AUTOMATION_DATABASE }
    if ($Database) { $cli += @('--db', $Database) }
    $cli += $Arguments
    Push-Location $PSScriptRoot
    $priorEncoding = $OutputEncoding
    try {
        $OutputEncoding = [System.Text.UTF8Encoding]::new($false)
        if ($PSBoundParameters.ContainsKey('InputObject')) {
            $result = $InputObject | ConvertTo-Json -Depth 40 -Compress | & $runtime @cli
        } else { $result = & $runtime @cli }
        if ($LASTEXITCODE -ne 0) { throw 'Harness operation failed; no payload file was created.' }
        return ($result | ConvertFrom-Json)
    } finally { $OutputEncoding = $priorEncoding; Pop-Location }
}
