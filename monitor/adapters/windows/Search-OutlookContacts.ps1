param(
    [Parameter(Mandatory = $true)]
    [string]$Query,

    [string]$Store = '*',
    [int]$Limit = 25,
    [int]$MaxItemsPerFolder = 200,
    [switch]$IncludeRecipientCache
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\OutlookAutomation.Common.ps1"

$namespace = Get-OutlookAutomationNamespace
$rows = @()
$errors = @()

foreach ($store in (Get-OutlookMatchingStores $namespace $Store)) {
    try {
        foreach ($folder in (Get-OutlookFolderMap $store)) {
            if ($rows.Count -ge $Limit) {
                break
            }
            if (-not $IncludeRecipientCache -and $folder.Name -eq 'Recipient Cache') {
                continue
            }

            if ($folder.DefaultItemType -eq 2) {
                $items = $folder.Items
                $max = [Math]::Min([int]$items.Count, $MaxItemsPerFolder)

                for ($i = 1; $i -le $max -and $rows.Count -lt $Limit; $i++) {
                    $item = $items.Item($i)
                    if ($null -eq $item) {
                        continue
                    }

                    $haystack = @(
                        [string]$item.FullName,
                        [string]$item.Email1Address,
                        [string]$item.Email2Address,
                        [string]$item.Email3Address
                    ) -join "`n"

                    if ($haystack -like "*$Query*") {
                        $rows += [pscustomobject]@{
                            Store = [string]$store.DisplayName
                            Folder = [string]$folder.FolderPath
                            FullName = [string]$item.FullName
                            Email1 = [string]$item.Email1Address
                            Email2 = [string]$item.Email2Address
                            Email3 = [string]$item.Email3Address
                        }
                    }
                }
            }
        }
    }
    catch {
        $errors += [pscustomobject]@{Store = [string]$store.DisplayName
            Error = $_.Exception.Message
        }
    }
}

[pscustomobject]@{Complete = ($errors.Count -eq 0)
    Rows = $rows
    Errors = $errors
} | ConvertTo-Json -Depth 5
if ($errors.Count) {
    throw 'Contact search incomplete; unavailable stores are recorded above.'
}
