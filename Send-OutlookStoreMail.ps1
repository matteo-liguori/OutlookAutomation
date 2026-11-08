param(
    [Parameter(Mandatory = $true)]
    [string]$Store,

    [Parameter(Mandatory = $true)]
    [string[]]$To,

    [Parameter(Mandatory = $true)]
    [string]$Subject,

    [string]$Body = '',
    [string[]]$Cc = @(),
    [string[]]$Bcc = @(),
    [string[]]$Attachments = @(),
    [switch]$DisplayOnly,
    [switch]$AllowDuplicate
)

$ErrorActionPreference = 'Stop'

& "$PSScriptRoot\Send-OutlookStoreMailSafe.ps1" `
    -Store $Store `
    -To $To `
    -Subject $Subject `
    -Body $Body `
    -Cc $Cc `
    -Bcc $Bcc `
    -Attachments $Attachments `
    -DisplayOnly:$DisplayOnly `
    -BatchSubmitOnly:(-not $DisplayOnly) `
    -AllowDuplicate:$AllowDuplicate
