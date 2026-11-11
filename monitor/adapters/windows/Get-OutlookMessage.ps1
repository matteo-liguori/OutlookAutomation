param(
    [Parameter(Mandatory = $true)]
    [string]$EntryId,

    [string]$StoreId = '',
    [string]$Store = '',
    [string]$Folder = 'Inbox',
    [string]$CaseId = '',
    [switch]$ForceRefresh,
    [switch]$DownloadLargeAttachments,
    [ValidateRange(1, 5000)][int]$MaxScan = 200,
    [ValidateRange(1, 256)][int]$MaxFolders = 64,

    [ValidateSet('Text', 'Json')]
    [string]$Output = 'Text'
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\OutlookAutomation.Common.ps1"
. "$PSScriptRoot\OutlookMessageLookup.ps1"

if ($CaseId) {
    . "$PSScriptRoot/OutlookHarness.Common.ps1"
    if (-not $ForceRefresh -and -not $DownloadLargeAttachments -and $StoreId) {
        $cached = Invoke-OutlookHarness -Arguments @('thread', 'find', '--input', '-') -InputObject @{case_id=$CaseId;entry_id=$EntryId;store_id=$StoreId}
        if ($cached.found) {
            if ($Output -eq 'Json') { $cached.message | ConvertTo-Json -Depth 10 } else { $cached.message | Format-List }
            return
        }
    }
}

$namespace = Get-OutlookAutomationNamespace

$lookup = Resolve-OutlookExactMessage -Namespace $namespace -EntryId $EntryId -StoreId $StoreId -Store $Store -Folder $Folder -MaxScan $MaxScan -MaxFolders $MaxFolders
$message = $lookup.Message
$unreadBefore = [bool]$message.UnRead

$attachmentNames = @(
    for ($i = 1; $i -le $message.Attachments.Count; $i++) {
        [string]$message.Attachments.Item($i).FileName
    }
)

$row = [pscustomobject]@{
    EntryId = [string]$message.EntryID
    StoreId = $lookup.ActualStoreId
    FolderPath = $lookup.FolderPath
    ReadPath = $lookup.ReadPath
    FallbackScanned = $lookup.Scanned
    FallbackFolders = $lookup.FoldersScanned
    DirectFailure = $lookup.DirectFailure
    MessageClass = [string]$message.MessageClass
    Subject = [string]$message.Subject
    From = [string]$message.SenderName
    SenderEmail = [string]$message.SenderEmailAddress
    To = [string]$message.To
    Cc = [string]$message.CC
    Received = Convert-OutlookComDate $message.ReceivedTime
    Body = [string]$message.Body
    Attachments = [int]$message.Attachments.Count
    AttachmentNames = $attachmentNames
    UnreadUnchanged = ($unreadBefore -eq [bool]$message.UnRead)
}

if ($CaseId) {
    try { $row | Add-Member InternetMessageId ([string]$message.PropertyAccessor.GetProperty('http://schemas.microsoft.com/mapi/proptag/0x1035001F')) } catch { }
    $assets = @()
    for ($i=1; $i -le $message.Attachments.Count; $i++) {
        $attachment=$message.Attachments.Item($i)
        $asset=@{name=[string]$attachment.FileName;index=$i;size=[long]$attachment.Size;downloaded=$false}
        if ($asset.size -le $(if ($env:OUTLOOK_AUTOMATION_SMALL_ATTACHMENT_BYTES) { [long]$env:OUTLOOK_AUTOMATION_SMALL_ATTACHMENT_BYTES } else { 10MB }) -or $DownloadLargeAttachments) {
            $destination=Invoke-OutlookHarness -Arguments @('thread','attachment-path','--input','-') -InputObject @{case_id=$CaseId;name=$asset.name;attachment_id=($EntryId+':'+$i)}
            $parent=Split-Path -Parent $destination.path
            New-Item -ItemType Directory -Path $parent -Force | Out-Null
            if (-not (Test-Path -LiteralPath $destination.path)) { $attachment.SaveAsFile($destination.path) }
            $registered=Invoke-OutlookHarness -Arguments @('thread','attachment','--input','-') -InputObject @{case_id=$CaseId;path=$destination.path;name=$asset.name;message_entry_id=$EntryId;store_id=$row.StoreId;attachment_index=$i}
            $asset.downloaded=$true; $asset.path=$registered.path; $asset.sha256=$registered.sha256
        }
        $assets+=,$asset
    }
    $row | Add-Member CachedAttachments $assets
    $receipt=Invoke-OutlookHarness -Arguments @('thread','put','--input','-') -InputObject @{case_id=$CaseId;message=$row}
    $row | Add-Member CacheEvidenceId $receipt.body_evidence_id
    if ($receipt.standing_sending_permissions) {
        $row | Add-Member standing_sending_permissions $receipt.standing_sending_permissions
        $row | Add-Member standing_sending_instruction $receipt.standing_sending_instruction
    }
}

if ($Output -eq 'Json') {
    $row | ConvertTo-Json -Depth 4
}
else {
    $row | Format-List
}
