function Resolve-OutlookExactMessage {
    param($Namespace, [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string]$EntryId, [string]$StoreId = '', [string]$Store = '',
        [string]$Folder = 'Inbox', [ValidateRange(1, 5000)][int]$MaxScan = 200, [ValidateRange(1, 256)][int]$MaxFolders = 64)
    $directFailure = ''
    try {
        $message = if ($StoreId) {
            $Namespace.GetItemFromID($EntryId, $StoreId)
        }
        else {
            $Namespace.GetItemFromID($EntryId)
        }
        if ($null -eq $message -or -not ([string]$message.EntryID).Equals($EntryId, [StringComparison]::OrdinalIgnoreCase)) {
            throw 'Direct lookup returned a different or missing EntryId.'
        }
        $actualStore = [string]$message.Parent.StoreID
        if (-not $actualStore -or ($StoreId -and -not $actualStore.Equals($StoreId, [StringComparison]::OrdinalIgnoreCase))) {
            throw 'Direct lookup store identity could not be verified.'
        }
        if ($Store -and [string]$message.Parent.Store.DisplayName -ne $Store) {
            throw 'Direct lookup returned a different sender store.'
        }
        return [pscustomobject]@{Message = $message
            ReadPath = 'direct-id'
            ActualStoreId = $actualStore
            FolderPath = [string]$message.Parent.FolderPath
            Scanned = 0
            FoldersScanned = 0
            DirectFailure = $null
        }
    }
    catch {
        $directFailure = $_.Exception.Message
    }
    if (-not $StoreId -and -not $Store) {
        throw "Direct read failed; bounded fallback requires an exact StoreId or Store name. $directFailure"
    }
    $stores = @()
    $storeChecks = 0
    foreach ($candidate in $Namespace.Stores) {
        if (++$storeChecks -gt 32) {
            throw 'Store discovery cap reached; lookup remains unresolved.'
        }
        if (($StoreId -and ([string]$candidate.StoreID).Equals($StoreId, [StringComparison]::OrdinalIgnoreCase)) -or (-not $StoreId -and [string]$candidate.DisplayName -eq $Store)) {
            $stores += $candidate
        }
    }
    if ($stores.Count -ne 1) {
        throw 'Fallback requires exactly one verified store.'
    }
    $storeItem = $stores[0]
    if ($Store -and [string]$storeItem.DisplayName -ne $Store) {
        throw 'Store name and StoreId disagree.'
    }
    $queue = New-Object System.Collections.Queue
    $queue.Enqueue($storeItem.GetRootFolder())
    $folders = @()
    $visited = 0
    while ($queue.Count -gt 0 -and $visited -lt $MaxFolders) {
        $f = $queue.Dequeue()
        $visited++
        if ([string]$f.Name -in @($Folder, 'Inbox', 'Sent Mail', 'Sent Items', 'Drafts') -or [string]$f.FolderPath -eq $Folder) {
            $folders += $f
        }
        foreach ($child in $f.Folders) {
            if ($queue.Count + $visited -ge $MaxFolders) {
                break
            }
            $queue.Enqueue($child)
        }
    }
    $folders = @($folders | Sort-Object @{Expression = { if ([string]$_.FolderPath -eq $Folder -or [string]$_.Name -eq $Folder) {
                    0
                }
                else {
                    1
                } }
        }, @{Expression = { if (([string]$_.FolderPath).Contains('[Gmail]')) {
                    0
                }
                else {
                    1
                } }
        })
    $scanned = 0
    $folderCount = 0
    foreach ($f in $folders) {
        $folderCount++
        $items = $f.Items
        $items.Sort('[ReceivedTime]', $true)
        for ($i = 1; $i -le $items.Count -and $scanned -lt $MaxScan; $i++) {
            $candidate = $items.Item($i)
            $scanned++
            if ($candidate -and ([string]$candidate.EntryID).Equals($EntryId, [StringComparison]::OrdinalIgnoreCase)) {
                return [pscustomobject]@{Message = $candidate
                    ReadPath = 'bounded-folder-exact-id'
                    ActualStoreId = [string]$storeItem.StoreID
                    FolderPath = [string]$f.FolderPath
                    Scanned = $scanned
                    FoldersScanned = $folderCount
                    DirectFailure = $directFailure
                }
            }
        }
        if ($scanned -ge $MaxScan) {
            break
        }
    }
    throw "Exact EntryId not found in bounded fallback ($scanned items, $folderCount selected folders, $visited folder metadata records). Obtain a verified current locator if the item moved; no subject/sender substitution was made. Direct failure: $directFailure"
}
