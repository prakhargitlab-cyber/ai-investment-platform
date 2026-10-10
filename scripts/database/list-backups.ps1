# .\platform.ps1 backups

function Get-PlatformBackups {
    param([Parameter(Mandatory = $true)][string]$ProjectRoot)

    $root = Get-BackupsRoot -ProjectRoot $ProjectRoot
    $dirs = Get-ChildItem -Path $root -Directory -ErrorAction SilentlyContinue | Where-Object { $_.Name -notlike ".tmp-*" }

    $rows = foreach ($dir in $dirs) {
        try {
            $manifest = Read-BackupManifest -BackupDir $dir.FullName
            $totalBytes = 0
            foreach ($db in $manifest.databases) { $totalBytes += [int64]$db.sizeBytes }
            [PSCustomObject]@{
                BackupId         = $manifest.backupId
                CreatedAtUtc     = [datetime]$manifest.createdAtUtc
                Databases        = ($manifest.databases | ForEach-Object { $_.name }) -join ","
                TotalSizeMiB     = [Math]::Round($totalBytes / 1MB, 2)
                Verification     = $manifest.verificationStatus
                SourceEnvironment = "$($manifest.sourceClusterName)/$($manifest.sourceNamespace)"
            }
        }
        catch {
            [PSCustomObject]@{
                BackupId = $dir.Name
                CreatedAtUtc = [datetime]::MinValue
                Databases = "(invalid manifest)"
                TotalSizeMiB = $null
                Verification = "INVALID"
                SourceEnvironment = $null
            }
        }
    }

    $sorted = $rows | Sort-Object CreatedAtUtc -Descending
    $latestId = ($sorted | Select-Object -First 1).BackupId

    if (-not $sorted) {
        Write-Host "No backups found under '$root'. Run '.\platform.ps1 backup' to create one." -ForegroundColor Yellow
        return
    }

    foreach ($row in $sorted) {
        $marker = if ($row.BackupId -eq $latestId) { " (latest)" } else { "" }
        Write-Host ""
        Write-Host "Backup       : $($row.BackupId)$marker" -ForegroundColor Cyan
        Write-Host "Created (UTC): $($row.CreatedAtUtc.ToString('o'))"
        Write-Host "Databases    : $($row.Databases)"
        Write-Host "Total size   : $($row.TotalSizeMiB) MiB"
        Write-Host "Verification : $($row.Verification)"
        Write-Host "Source       : $($row.SourceEnvironment)"
    }
    Write-Host ""
}
