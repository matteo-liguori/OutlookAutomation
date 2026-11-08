param(
    [switch]$Disable
)

$errorActionPreference = 'Stop'

$securityPaths = @(
    'HKCU:\Software\Microsoft\Office\16.0\Outlook\Security',
    'HKCU:\Software\Policies\Microsoft\Office\16.0\Outlook\Security'
)

$promptValues = @(
    'PromptOOMSend',
    'PromptOOMAddressInformationAccess',
    'PromptOOMAddressBookAccess',
    'PromptOOMMeetingTaskRequestResponse',
    'PromptOOMSaveAs',
    'PromptOOMFormulaAccess',
    'PromptOOMAddressUserPropertyFind'
)

if ($Disable) {
    foreach ($path in $securityPaths) {
        if (Test-Path $path) {
            Remove-ItemProperty -Path $path -Name ObjectModelGuard -ErrorAction SilentlyContinue
            Remove-ItemProperty -Path $path -Name AdminSecurityMode -ErrorAction SilentlyContinue
            foreach ($name in $promptValues) {
                Remove-ItemProperty -Path $path -Name $name -ErrorAction SilentlyContinue
            }
        }
    }

    Write-Host 'Outlook automation policy values removed for the current user.'
    exit 0
}

foreach ($path in $securityPaths) {
    try {
        if (-not (Test-Path $path)) {
            New-Item -Path $path -Force | Out-Null
        }

        New-ItemProperty -Path $path -Name ObjectModelGuard -PropertyType DWord -Value 2 -Force | Out-Null

        foreach ($name in $promptValues) {
            New-ItemProperty -Path $path -Name $name -PropertyType DWord -Value 1 -Force | Out-Null
        }
    }
    catch {
        Write-Warning "Could not update ${path}: $($_.Exception.Message)"
    }
}

$policyPath = 'HKCU:\Software\Policies\Microsoft\Office\16.0\Outlook\Security'
if (Test-Path $policyPath) {
    try {
        New-ItemProperty -Path $policyPath -Name AdminSecurityMode -PropertyType DWord -Value 3 -Force | Out-Null
    }
    catch {
        Write-Warning "Could not update ${policyPath}: $($_.Exception.Message)"
    }
}

Write-Host 'Outlook 16 current-user automation policy values are enabled.'
Write-Host 'Restart Outlook before testing command-line sends.'
