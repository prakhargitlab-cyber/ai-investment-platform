# .\platform.ps1 backup
#
# Creates exactly one on-demand PostgreSQL backup of the "investment"
# database. Never invoked automatically; only ever runs when the operator
# explicitly runs the "backup" command. Never performs a Helm upgrade,
# image build or deployment.

function Invoke-PlatformBackup {
    param(
        [Parameter(Mandatory = $true)][string]$ProjectRoot,
        [Parameter(Mandatory = $true)][string]$Namespace,
        [Parameter(Mandatory = $true)][string]$ClusterName,
        [Parameter(Mandatory = $true)][hashtable]$DatabaseConfig
    )

    Write-Step "Creating on-demand PostgreSQL backup"

    $podName = Get-PostgresPodName -Namespace $Namespace
    $dbName = $DatabaseConfig.Name
    $dbUser = $DatabaseConfig.Username

    $backupsRoot = Get-BackupsRoot -ProjectRoot $ProjectRoot
    $backupId = New-BackupId
    $tempDir = Join-Path $backupsRoot ".tmp-$backupId"
    $finalDir = Join-Path $backupsRoot $backupId
    New-Item -ItemType Directory -Path $tempDir -Force | Out-Null

    $remoteDumpPath = "/tmp/platform-backup-$backupId.dump"
    $localDumpFileName = "database.dump"
    $localDumpPath = Join-Path $tempDir $localDumpFileName

    try {
        Write-Step "Running pg_dump inside the PostgreSQL pod (custom format)"
        $dumpResult = Invoke-PodExec -Namespace $Namespace -PodName $podName -Arguments @(
            "pg_dump", "-Fc", "-U", $dbUser, "-d", $dbName, "-f", $remoteDumpPath
        )
        if ($dumpResult.ExitCode -ne 0) {
            throw "pg_dump failed inside the PostgreSQL pod: $($dumpResult.Output)"
        }

        Write-Step "Copying the dump out of the cluster (binary-safe)"
        Copy-FromPostgresPod -Namespace $Namespace -PodName $podName -RemotePath $remoteDumpPath -LocalPath $localDumpPath

        if (-not (Test-Path $localDumpPath) -or (Get-Item $localDumpPath).Length -eq 0) {
            throw "Dump file was not produced correctly (missing or zero bytes). Discarding this backup attempt."
        }

        Write-Step "Verifying archive integrity before publishing (pg_restore --list)"
        $remoteVerifyPath = "/tmp/platform-backup-verify-$backupId.dump"
        Copy-ToPostgresPod -Namespace $Namespace -PodName $podName -LocalPath $localDumpPath -RemotePath $remoteVerifyPath
        $listResult = Invoke-PodExec -Namespace $Namespace -PodName $podName -Arguments @("pg_restore", "--list", $remoteVerifyPath)
        Invoke-PodExec -Namespace $Namespace -PodName $podName -Arguments @("rm", "-f", $remoteVerifyPath) | Out-Null
        if ($listResult.ExitCode -ne 0) {
            throw "Archive integrity check failed (pg_restore --list): $($listResult.Output)"
        }

        $checksum = Get-FileSha256 -Path $localDumpPath
        $sizeBytes = (Get-Item $localDumpPath).Length

        Write-Step "Collecting backup metadata"
        $serverVersion = Get-PostgresServerVersion -Namespace $Namespace -PodName $podName -DatabaseUser $dbUser
        $flywayVersions = Get-FlywaySchemaVersions -Namespace $Namespace -PodName $podName -DatabaseName $dbName -DatabaseUser $dbUser
        $gitCommit = Get-CurrentGitCommit -ProjectRoot $ProjectRoot
        $kubeContext = (kubectl config current-context 2>$null)

        $manifest = [ordered]@{
            backupId             = $backupId
            createdAtUtc         = (Get-Date).ToUniversalTime().ToString("o")
            sourceKubeContext    = if ($kubeContext) { $kubeContext.Trim() } else { $null }
            sourceNamespace      = $Namespace
            sourceClusterName    = $ClusterName
            postgresServerVersion = $serverVersion
            applicationGitCommit = $gitCommit
            flywaySchemaVersions = $flywayVersions
            databases            = @(
                [ordered]@{
                    name          = $dbName
                    dumpFormat    = "custom"
                    fileName      = $localDumpFileName
                    sizeBytes     = $sizeBytes
                    sha256        = $checksum
                }
            )
            verificationStatus   = "ARCHIVE_INTEGRITY_VERIFIED"
            verificationNote     = "pg_restore --list succeeded at backup time. This confirms the archive is structurally readable; it is NOT a full test restore. Run '.\platform.ps1 backup-verify -Backup $backupId' for a repeatable integrity re-check, or perform a real restore into a disposable database to confirm restore-tested status."
        }

        $manifest | ConvertTo-Json -Depth 10 | Set-Content -Path (Join-Path $tempDir "manifest.json") -Encoding UTF8
        "$checksum  $localDumpFileName" | Set-Content -Path (Join-Path $tempDir "checksums.sha256") -Encoding ASCII

        # Publish atomically: only rename the fully-written, verified temp
        # directory into its final timestamped name. A backup that fails at
        # any point above leaves only the .tmp-* directory, never something
        # that looks like a completed backup under backups/postgres/.
        Rename-Item -Path $tempDir -NewName $backupId -Force
        $tempDir = $null

        Write-Host ""
        Write-Host "Backup completed: $finalDir" -ForegroundColor Green
        Write-Host "  Database       : $dbName"
        Write-Host "  Size           : $([Math]::Round($sizeBytes / 1MB, 2)) MiB"
        Write-Host "  SHA-256        : $checksum"
        Write-Host "  Verification   : ARCHIVE_INTEGRITY_VERIFIED (not a full test restore)"
        Write-Host ""
        return $finalDir
    }
    catch {
        Invoke-PodExec -Namespace $Namespace -PodName $podName -Arguments @("rm", "-f", $remoteDumpPath) | Out-Null
        if ($tempDir -and (Test-Path $tempDir)) {
            Remove-Item -Path $tempDir -Recurse -Force -ErrorAction SilentlyContinue
        }
        throw
    }
    finally {
        Invoke-PodExec -Namespace $Namespace -PodName $podName -Arguments @("rm", "-f", $remoteDumpPath) 2>$null | Out-Null
    }
}
