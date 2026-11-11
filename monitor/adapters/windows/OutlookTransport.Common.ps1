# Shared bounded transport inspection. No submission or recovery occurs here.
function Get-OutlookBodySha256 {
    param([string]$Body)
    $bytes = [Text.Encoding]::UTF8.GetBytes((($Body -replace '\s+', ' ').Trim()))
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant()
    }
    finally {
        $sha.Dispose()
    }
}
function Get-OutlookAttachmentHashes {
    param($Message)
    $result = @()
    . "$PSScriptRoot/AutomationEnvironment.Common.ps1"
    $folder = Join-Path (Get-OutlookAutomationDataRoot) 'monitor/scratch/transport'
    for ($i = 1; $i -le $Message.Attachments.Count; $i++) {
        New-Item -ItemType Directory -Path $folder -Force | Out-Null
        $path = Join-Path $folder ([guid]::NewGuid().ToString('N'))
        try {
            $attachment = $Message.Attachments.Item($i)
            $attachment.SaveAsFile($path)
            $result += (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
        }
        finally {
            if (Test-Path -LiteralPath $path) {
                Remove-Item -LiteralPath $path -Force
            }
        }
    }
    return @($result | Sort-Object)
}
function Get-OutlookRecipientGroups {
    param($Message)
    $groups = @{To = @()
        Cc = @()
        Bcc = @()
    }
    foreach ($recipient in $Message.Recipients) {
        $address = [string]$recipient.Address
        if ($address -notmatch '^[^@\s]+@[^@\s]+\.[^@\s]+$') {
            try {
                $address = [string]$recipient.PropertyAccessor.GetProperty('http://schemas.microsoft.com/mapi/proptag/0x39FE001E')
            }
            catch {
                throw 'Cannot verify the SMTP recipient; transport identity remains unresolved.'
            }
        }
        Assert-OutlookRecipients @($address) 'transport recipient'
        $field = switch ([int]$recipient.Type) {
            1 {
                'To'
            }2 {
                'Cc'
            }3 {
                'Bcc'
            }default {
                throw 'Unknown recipient type.'
            }
        }
        $groups[$field] += $address.Trim().ToLowerInvariant()
    }
    return $groups
}
function Get-OutlookEnvelope {
    param($Message)
    $groups = Get-OutlookRecipientGroups $Message
    $sender = [string]$Message.Parent.Store.DisplayName
    if ($null -ne $Message.SendUsingAccount) {
        $sender = [string]$Message.SendUsingAccount.SmtpAddress
    }
    [ordered]@{Sender = $sender
        To = @($groups.To | Sort-Object -Unique)
        Cc = @($groups.Cc | Sort-Object -Unique)
        Bcc = @($groups.Bcc | Sort-Object -Unique)
        Subject = [string]$Message.Subject
        BodySha256 = Get-OutlookBodySha256 ([string]$Message.Body)
        AttachmentCount = [int]$Message.Attachments.Count
        AttachmentHashes = @(Get-OutlookAttachmentHashes $Message)
    }
}
function Assert-OutlookEnvelope {
    param($Actual, $Expected)
    foreach ($field in @('Sender', 'Subject', 'BodySha256', 'AttachmentCount')) {
        if ($Actual[$field] -cne $Expected[$field]) {
            throw "Approved message $field mismatch."
        }
    }
    foreach ($field in @('To', 'Cc', 'Bcc', 'AttachmentHashes')) {
        $actualValues = @($Actual[$field] | Sort-Object)
        $expectedValues = @($Expected[$field] | Sort-Object)
        if (($actualValues | ConvertTo-Json -Compress) -cne ($expectedValues | ConvertTo-Json -Compress)) {
            throw "Approved message $field mismatch."
        }
    }
}
function Get-OutlookTransportScan {
    param($Namespace, [string]$Store = '*', [string]$Subject, [string]$Body = '',
        [string[]]$Recipients = @(), [ValidateRange(1, 3650)][int]$LookbackDays = 30,
        [ValidateRange(1, 5000)][int]$MaxScan = 500, [string]$BodySha256 = '')
    $rows = @()
    $errors = @()
    $scanned = 0
    $cutoff = (Get-Date).AddDays(-$LookbackDays)
    $fingerprint = Get-OutlookBodyFingerprint $Body
    $wanted = Get-OutlookRecipientSet $Recipients
    $stores = @(Get-OutlookMatchingStores $Namespace $Store)
    if (-not $stores.Count) {
        $errors += 'No intended Outlook store is available.'
    }
    foreach ($storeItem in $stores) {
        foreach ($folder in (Get-OutlookFolderMap $storeItem)) {
            if ($folder.Name -notin @('Sent Mail', 'Sent Items', 'Outbox', 'Drafts')) {
                continue
            }
            try {
                $items = $folder.Items
                $items.Sort('[LastModificationTime]', $true)
                $examined = 0
                $boundary = $false
                for ($i = 1; $i -le $items.Count -and $examined -lt $MaxScan; $i++) {
                    $m = $items.Item($i)
                    $examined++
                    $scanned++
                    if ([datetime]$m.LastModificationTime -lt $cutoff) {
                        $boundary = $true
                        break
                    }
                    if ($m.MessageClass -notlike 'IPM.Note*' -or -not (Test-OutlookSubjectsRelated ([string]$m.Subject) $Subject)) {
                        continue
                    }
                    $bodyText = [string]$m.Body
                    if ($BodySha256 -and (Get-OutlookBodySha256 $bodyText) -ne $BodySha256) {
                        continue
                    }
                    $groups = Get-OutlookRecipientGroups $m
                    $overlap = @(($groups.To + $groups.Cc + $groups.Bcc) | Where-Object { $wanted -contains $_ })
                    $bodyMatch = [bool]($fingerprint -and (Normalize-OutlookText $bodyText).Contains($fingerprint))
                    $rows += [pscustomobject]@{Store = [string]$storeItem.DisplayName
                        StoreId = [string]$storeItem.StoreID
                        Folder = [string]$folder.FolderPath
                        Time = Get-OutlookMessageTime $m
                        Subject = [string]$m.Subject
                        To = [string]$m.To
                        Cc = [string]$m.CC
                        Bcc = [string]$m.BCC
                        BodyMatch = $bodyMatch
                        RecipientOverlap = ($overlap -join '; ')
                        EntryId = [string]$m.EntryID
                        Sent = [bool]$m.Sent
                        Submitted = [bool]$m.Submitted
                    }
                }
                if (-not $boundary -and $i -le $items.Count) {
                    $errors += "Transport scan cap reached: $($folder.FolderPath)"
                }
            }
            catch {
                $errors += "$($folder.FolderPath): $($_.Exception.Message)"
            }
        }
    }
    [pscustomobject]@{Complete = ($errors.Count -eq 0)
        Scanned = $scanned
        Errors = $errors
        Rows = $rows
    }
}
