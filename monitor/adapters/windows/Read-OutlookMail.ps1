param(
    [ValidateSet('Accounts', 'Folders', 'Recent', 'Search')]
    [string]$Action = 'Recent',

    [string]$Store = '*',
    [string]$Folder = 'Inbox',
    [int]$Limit = 10,
    [string]$Query = '',
    [switch]$UnreadOnly,
    [switch]$IncludePreview,

    [ValidateSet('Table', 'List', 'Json', 'Csv')]
    [string]$Output = 'Table',
    [int]$MaxScan = 500
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\OutlookAutomation.Common.ps1"

function Get-Preview {
    param($Message)

    if (-not $IncludePreview) {
        return ''
    }

    $body = [string]$Message.Body
    $body = ($body -replace '\s+', ' ').Trim()

    if ($body.Length -gt 240) {
        return $body.Substring(0, 240)
    }

    return $body
}

function Convert-Message {
    param(
        $Store,
        $Message
    )

    [pscustomobject]@{
        Store = $Store.DisplayName
        Received = Convert-OutlookComDate $Message.ReceivedTime
        Unread = [bool]$Message.UnRead
        From = [string]$Message.SenderName
        SenderEmail = [string]$Message.SenderEmailAddress
        Subject = [string]$Message.Subject
        Preview = Get-Preview $Message
        EntryId = [string]$Message.EntryID
    }
}

function Write-Results {
    param($Rows)

    Write-OutlookRows $Rows $Output 4
}

$namespace = Get-OutlookAutomationNamespace
$rows = @()

switch ($Action) {
    'Accounts' {
        foreach ($account in $namespace.Accounts) {
            $rows += [pscustomobject]@{
                DisplayName = [string]$account.DisplayName
                SmtpAddress = [string]$account.SmtpAddress
                AccountType = [string]$account.AccountType
            }
        }
    }

    'Folders' {
        foreach ($storeItem in (Get-OutlookMatchingStores $namespace $Store)) {
            foreach ($folderItem in (Get-OutlookFolderMap $storeItem)) {
                $rows += [pscustomobject]@{
                    Store = [string]$storeItem.DisplayName
                    Folder = [string]$folderItem.Name
                    FolderPath = [string]$folderItem.FolderPath
                    Items = [int]$folderItem.Items.Count
                }
            }
        }
    }

    'Recent' {
        foreach ($storeItem in (Get-OutlookMatchingStores $namespace $Store)) {
            $folderItem = Find-OutlookFolder $storeItem $Folder
            $items = $folderItem.Items
            $items.Sort('[ReceivedTime]', $true)

            $found = 0
            for ($i = 1; $i -le $items.Count -and $found -lt $Limit -and $i -le $MaxScan; $i++) {
                $message = $items.Item($i)
                # Meeting requests, updates, and cancellations are actionable mail too.
                # Keep them in monitoring results rather than silently treating them as
                # non-mail alongside reports and other Outlook item types.
                if ($null -eq $message -or ($message.MessageClass -notlike 'IPM.Note*' -and $message.MessageClass -notlike 'IPM.Schedule.Meeting.*')) {
                    continue
                }

                if ($UnreadOnly -and -not $message.UnRead) {
                    continue
                }

                $rows += Convert-Message $storeItem $message
                $found++
            }
        }
    }

    'Search' {
        if (-not $Query.Trim()) {
            throw 'Search requires -Query.'
        }

        foreach ($storeItem in (Get-OutlookMatchingStores $namespace $Store)) {
            $folderItem = Find-OutlookFolder $storeItem $Folder
            $items = $folderItem.Items
            $items.Sort('[ReceivedTime]', $true)

            $found = 0
            for ($i = 1; $i -le $items.Count -and $found -lt $Limit -and $i -le $MaxScan; $i++) {
                $message = $items.Item($i)
                # Include calendar-message classes so searches can find meeting
                # cancellations and updates sent by organizers.
                if ($null -eq $message -or ($message.MessageClass -notlike 'IPM.Note*' -and $message.MessageClass -notlike 'IPM.Schedule.Meeting.*')) {
                    continue
                }

                if ($UnreadOnly -and -not $message.UnRead) {
                    continue
                }

                $haystack = @(
                    [string]$message.Subject,
                    [string]$message.SenderName,
                    [string]$message.SenderEmailAddress,
                    [string]$message.Body
                ) -join "`n"

                if ($haystack -like "*$Query*") {
                    $rows += Convert-Message $storeItem $message
                    $found++
                }
            }
        }
    }
}

Write-Results $rows
