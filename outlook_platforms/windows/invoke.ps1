# JSON transport to existing helpers; no command strings are evaluated.
$ErrorActionPreference='Stop'
[Console]::InputEncoding=[Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
$OutputEncoding=[Text.UTF8Encoding]::new($false)
$root=Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
try {
    $request=([Console]::In.ReadToEnd()).TrimStart([char]0xFEFF) | ConvertFrom-Json
    $allowed=@('Inspect-Outlook.ps1','Test-OutlookSession.ps1','Read-OutlookMail.ps1','Search-OutlookMailDetails.ps1','Get-OutlookMessage.ps1','Collect-OutlookMonitor.ps1','Read-OutlookMonitorCalendar.ps1','Start-OutlookSendReceive.ps1','Test-OutlookMailboxFreshness.ps1','Search-OutlookContacts.ps1','Test-OutlookSendDuplicate.ps1','Send-OutlookStoreMailSafe.ps1')
    if($request.helper -notin $allowed){throw 'Unsupported helper.'}
    $parameters=@{}
    foreach($property in $request.parameters.PSObject.Properties){$parameters[$property.Name]=$property.Value}
    if($request.helper -eq 'Collect-OutlookMonitor.ps1'){
        foreach($key in @('Since','Until')){if($parameters.ContainsKey($key)){$parameters[$key]=[datetimeoffset]$parameters[$key]}}
    }
    if($request.helper -eq 'Read-OutlookMonitorCalendar.ps1'){
        foreach($key in @('Start','End')){if($parameters.ContainsKey($key)){$parameters[$key]=[datetimeoffset]::Parse($parameters[$key]).LocalDateTime}}
    }
    # Missing required arguments fail instead of opening an unattended prompt.
    $helper=Join-Path $root $request.helper
    if($parameters['Folder'] -in @('inbox','sent','drafts','trash')){
        . (Join-Path $root 'OutlookAutomation.Common.ps1')
        $store=Get-OutlookStoreByName (Get-OutlookAutomationNamespace) $parameters['Store']
        $folderNumbers=@{inbox=6;sent=5;drafts=16;trash=3}
        $parameters['Folder']=[string]$store.GetDefaultFolder($folderNumbers[$parameters['Folder'].ToLowerInvariant()]).FolderPath
    }
    if($request.helper -eq 'Get-OutlookMessage.ps1' -and $parameters['StoreId']){
        . (Join-Path $root 'OutlookAutomation.Common.ps1')
        $store=Get-OutlookStoreByName (Get-OutlookAutomationNamespace) $parameters['Store']
        if([string]$store.StoreID -cne [string]$parameters['StoreId']){throw 'StoreId does not match the configured source mailbox.'}
    }
    $command=Get-Command -Name $helper
    foreach($parameter in $command.Parameters.Values){
        if(@($parameter.Attributes | Where-Object {$_ -is [Management.Automation.ParameterAttribute] -and $_.Mandatory}).Count -and -not $parameters.ContainsKey($parameter.Name)){throw "Missing required field: $($parameter.Name)"}
    }
    $output=@(& $helper @parameters 6>$null)
    if($output.Count){
        $json=$output -join "`n"
        if($json.TrimStart().StartsWith('[')){$value=@($json | ConvertFrom-Json)}else{$value=$json | ConvertFrom-Json}
    }else{$value=@()}
    ConvertTo-Json -InputObject $value -Depth 40 -Compress
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 2
}
