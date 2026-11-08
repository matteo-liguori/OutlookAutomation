param(
    [string]$Store = '*',
    [string]$Folder = 'Inbox',
    [Parameter(Mandatory = $true)]
    [string]$Query,
    [int]$Limit = 10,
    [int]$BodyChars = 2000,
    [int]$MaxScan = 500
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\OutlookAutomation.Common.ps1"

function Shorten {
    param([string]$Value, [int]$Max)

    $text = ($Value -replace '\s+', ' ').Trim()
    if ($text.Length -gt $Max) {
        return $text.Substring(0, $Max)
    }

    return $text
}

$namespace = Get-OutlookAutomationNamespace
$rows = @()

foreach ($storeItem in (Get-OutlookMatchingStores $namespace $Store)) {
    $folderItem = Find-OutlookFolder $storeItem $Folder
    $items = $folderItem.Items
    $items.Sort('[ReceivedTime]', $true)

    for ($i = 1; $i -le $items.Count -and $rows.Count -lt $Limit -and $i -le $MaxScan; $i++) {
        $message = $items.Item($i)
        if ($null -eq $message -or ($message.MessageClass -notlike 'IPM.Note*' -and $message.MessageClass -notlike 'IPM.Schedule.Meeting.*')) {
            continue
        }

        $body = [string]$message.Body
        $haystack = @(
            [string]$message.Subject,
            [string]$message.SenderName,
            [string]$message.SenderEmailAddress,
            [string]$message.To,
            [string]$message.CC,
            $body
        ) -join "`n"

        if ($haystack -like "*$Query*") {
            $attachmentNames = @(
                for ($attachmentIndex = 1; $attachmentIndex -le $message.Attachments.Count; $attachmentIndex++) {
                    [string]$message.Attachments.Item($attachmentIndex).FileName
                }
            )
            $rows += [pscustomobject]@{
                Store = [string]$storeItem.DisplayName
                StoreId = [string]$storeItem.StoreID
                MessageClass = [string]$message.MessageClass
                Folder = [string]$folderItem.FolderPath
                Received = ([datetime]$message.ReceivedTime).ToString('yyyy-MM-dd HH:mm:ss')
                From = [string]$message.SenderName
                SenderEmail = [string]$message.SenderEmailAddress
                To = [string]$message.To
                Cc = [string]$message.CC
                Subject = [string]$message.Subject
                BodyPreview = Shorten $body $BodyChars
                AttachmentNames = $attachmentNames
                EntryId = [string]$message.EntryID
            }
        }
    }
}

Write-OutlookRows $rows 'Json' 4
