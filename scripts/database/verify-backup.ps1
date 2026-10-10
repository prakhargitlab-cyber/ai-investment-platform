# .\platform.ps1 backup-verify -Backup latest
#
# Performs an archive-integrity check, NOT a full test restore. See
# restore.ps1 for the only path that performs a real, disposable-database
# restore test.

function Invoke-PlatformBackupVerify {
    param(
        [Parameter(Mandatory = $true)][string]$ProjectRoot,
        [Parameter(Mandatory = $true)][string]$Namespace,
        [Parameter(Mandatory = $true)][string]$Backup
    )

    $backupDir = Resolve-BackupDirectory -ProjectRoot $ProjectRoot -Backup $Backup
    $backupId = Split-Path -Leaf $backupDir
    Write-Step "Verifying backup '$backupId'"

    $failures = New-Object System.Collections.Generic.List[string]

    try {
        $manifest = Read-BackupManifest -BackupDir $backupDir
        Assert-ValidManifest -Manifest $manifest
    }
    catch {
        Write-Host "FAIL: $($_.Exception.Message)" -ForegroundColor Red
        return $false
    }

    foreach ($db in $manifest.databases) {
        Test-SafeIdentifier -Value $db.name -FieldName "Manifest database name"
        $filePath = Join-Path $backupDir $db.fileName
        if (-not (Test-Path $filePath)) {
            $failures.Add("Missing dump file for database '$($db.name)': $filePath")
            continue
        }

        $actualSize = (Get-Item $filePath).Length
        if ($actualSize -ne [int64]$db.sizeBytes) {
            $failures.Add("Size mismatch for '$($db.name)': manifest says $($db.sizeBytes) bytes, file is $actualSize bytes")
        }

        $actualChecksum = Get-FileSha256 -Path $filePath
        if ($actualChecksum -ne $db.sha256) {
            $failures.Add("SHA-256 mismatch for '$($db.name)': manifest says $($db.sha256), file hashes to $actualChecksum")
        }
    }

    $checksumsFile = Join-Path $backupDir "checksums.sha256"
    if (-not (Test-Path $checksumsFile)) {
        $failures.Add("checksums.sha256 file is missing")
    }

    if ($failures.Count -eq 0) {
        $podName = Get-PostgresPodName -Namespace $Namespace
        foreach ($db in $manifest.databases) {
            $filePath = Join-Path $backupDir $db.fileName
            $remotePath = "/tmp/platform-verify-$backupId-$($db.name).dump"
            try {
                Copy-ToPostgresPod -Namespace $Namespace -PodName $podName -LocalPath $filePath -RemotePath $remotePath
                $listResult = Invoke-PodExec -Namespace $Namespace -PodName $podName -Arguments @("pg_restore", "--list", $remotePath)
                if ($listResult.ExitCode -ne 0) {
                    $failures.Add("pg_restore --list failed for '$($db.name)': $($listResult.Output)")
                }
            }
            finally {
                Invoke-PodExec -Namespace $Namespace -PodName $podName -Arguments @("rm", "-f", $remotePath) | Out-Null
            }
        }
    }

    # Manifest structure/field validity is already enforced above via
    # Assert-ValidManifest (which throws, and is caught, before reaching
    # this point) -- no separate ad-hoc field check is needed here.

    Write-Host ""
    if ($failures.Count -eq 0) {
        Write-Host "PASS: backup '$backupId' is internally consistent and its archive(s) are readable by pg_restore." -ForegroundColor Green
        Write-Host "This is an archive-integrity check only. It does not confirm the data restores cleanly into a real database -- use '.\platform.ps1 restore' into a disposable target for that." -ForegroundColor Yellow
        return $true
    }
    else {
        Write-Host "FAIL: backup '$backupId' did not pass verification:" -ForegroundColor Red
        foreach ($f in $failures) { Write-Host "  - $f" -ForegroundColor Red }
        return $false
    }
}
