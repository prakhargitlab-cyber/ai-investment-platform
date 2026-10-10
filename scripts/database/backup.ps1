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
    Test-SafeIdentifier -Value $dbName -FieldName "Database name"
    Test-SafeIdentifier -Value $dbUser -FieldName "Database user"

    $backupsRoot = Get-BackupsRoot -ProjectRoot $ProjectRoot
    $backupId = New-BackupId
    Test-SafeBackupId -BackupId $backupId
    $tempDir = Join-Path $backupsRoot ".tmp-$backupId"
    $finalDir = Join-Path $backupsRoot $backupId
    New-Item -ItemType Directory -Path $tempDir -Force | Out-Null

    $remoteDumpPath = "/tmp/platform-backup-$backupId.dump"
    $remoteVerifyPath = "/tmp/platform-backup-verify-$backupId.dump"
    $localDumpFileName = "database.dump"
    $localDumpPath = Join-Path $tempDir $localDumpFileName

    try {
        # Confirmed from the application's own configuration (not just this
        # runtime check): infrastructure/helm/ai-investment-platform's
        # java-services.yaml (JDBC URL) and ai-services.yaml
        # (AIP_RESEARCH_DATABASE_NAME) both derive every service's database
        # name from the single $.Values.database.name value ("investment"
        # in both values.yaml and values-dev.yaml). No service is
        # configured against a different database. This runtime check
        # remains as a defense-in-depth safety net for any database created
        # outside that configuration (e.g. manually, or by a future change
        # not yet reflected here).
        Write-Step "Checking for additional PostgreSQL databases beyond '$dbName'"
        $otherDatabases = @()
        $otherDbResult = Invoke-PodExec -Namespace $Namespace -PodName $podName -Arguments @(
            "psql", "-U", $dbUser, "-d", "postgres", "-At", "-c",
            "select datname from pg_database where not datistemplate and datname not in ('postgres', '$dbName');"
        )
        if ($otherDbResult.ExitCode -eq 0) {
            $otherDatabases = @($otherDbResult.Output -split "`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ })
            if ($otherDatabases.Count -gt 0) {
                Write-Warning "Additional PostgreSQL database(s) exist and are NOT covered by this backup: $($otherDatabases -join ', '). Only '$dbName' is backed up by '.\platform.ps1 backup'."
            }
        }
        else {
            Write-Warning "Unable to confirm whether additional PostgreSQL databases exist beyond '$dbName' (psql exit $($otherDbResult.ExitCode)): $($otherDbResult.Output)"
        }

        Write-Step "Running pg_dump and capturing a synchronized per-table row-count snapshot (same PostgreSQL snapshot, so counts never drift from what pg_dump actually wrote)"
        $tableRowCounts = Invoke-SynchronizedDumpWithRowCounts -Namespace $Namespace -PodName $podName `
            -DatabaseUser $dbUser -DatabaseName $dbName -RemoteDumpPath $remoteDumpPath -BackupId $backupId

        Write-Step "Copying the dump out of the cluster (binary-safe)"
        Copy-FromPostgresPod -Namespace $Namespace -PodName $podName -RemotePath $remoteDumpPath -LocalPath $localDumpPath

        if (-not (Test-Path $localDumpPath) -or (Get-Item $localDumpPath).Length -eq 0) {
            throw "Dump file was not produced correctly (missing or zero bytes). Discarding this backup attempt."
        }

        Write-Step "Verifying archive integrity before publishing (pg_restore --list)"
        try {
            Copy-ToPostgresPod -Namespace $Namespace -PodName $podName -LocalPath $localDumpPath -RemotePath $remoteVerifyPath
            $listResult = Invoke-PodExec -Namespace $Namespace -PodName $podName -Arguments @("pg_restore", "--list", $remoteVerifyPath)
            if ($listResult.ExitCode -ne 0) {
                throw "Archive integrity check failed (pg_restore --list): $($listResult.Output)"
            }
        }
        finally {
            $cleanupVerify = Invoke-PodExec -Namespace $Namespace -PodName $podName -Arguments @("rm", "-f", $remoteVerifyPath)
            if ($cleanupVerify.ExitCode -ne 0) {
                Write-Warning "Unable to remove temporary verification file '$remoteVerifyPath' from the PostgreSQL pod; it may need manual cleanup."
            }
        }

        $checksum = Get-FileSha256 -Path $localDumpPath
        $sizeBytes = (Get-Item $localDumpPath).Length

        Write-Step "Collecting remaining backup metadata"
        $serverVersion = Get-PostgresServerVersion -Namespace $Namespace -PodName $podName -DatabaseUser $dbUser
        $flywayVersions = Get-FlywaySchemaVersions -Namespace $Namespace -PodName $podName -DatabaseName $dbName -DatabaseUser $dbUser
        $gitCommit = Get-CurrentGitCommit -ProjectRoot $ProjectRoot
        $kubeContext = (kubectl config current-context 2>$null)

        $manifest = [ordered]@{
            backupId                      = $backupId
            createdAtUtc                  = (Get-Date).ToUniversalTime().ToString("o")
            sourceKubeContext             = if ($kubeContext) { $kubeContext.Trim() } else { $null }
            sourceNamespace               = $Namespace
            sourceClusterName             = $ClusterName
            postgresServerVersion         = $serverVersion
            applicationGitCommit          = $gitCommit
            flywaySchemaVersions          = $flywayVersions
            otherDatabasesDetectedNotBackedUp = $otherDatabases
            databases                     = @(
                [ordered]@{
                    name          = $dbName
                    dumpFormat    = "custom"
                    fileName      = $localDumpFileName
                    sizeBytes     = $sizeBytes
                    sha256        = $checksum
                    tableRowCounts = $tableRowCounts
                    tableRowCountsCaptureMethod = "SYNCHRONIZED_PG_EXPORT_SNAPSHOT"
                }
            )
            verificationStatus   = "ARCHIVE_INTEGRITY_VERIFIED"
            verificationNote     = "pg_restore --list succeeded at backup time. This confirms the archive is structurally readable; it is NOT a full test restore. Run '.\platform.ps1 backup-verify -Backup $backupId' for a repeatable integrity re-check, or '.\platform.ps1 restore -Backup $backupId -TargetDatabase <disposable-name>' to confirm full restore-tested status with real row-count comparison."
        }

        Assert-ValidManifest -Manifest ([PSCustomObject]$manifest)

        $manifest | ConvertTo-Json -Depth 12 | Set-Content -Path (Join-Path $tempDir "manifest.json") -Encoding UTF8
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
        if ($tempDir -and (Test-Path $tempDir)) {
            Remove-Item -Path $tempDir -Recurse -Force -ErrorAction SilentlyContinue
        }
        throw
    }
    finally {
        # Belt-and-suspenders: remove the primary remote dump on every exit
        # path, success or failure (the verify-copy cleans up separately,
        # right after its own use, above).
        Invoke-PodExec -Namespace $Namespace -PodName $podName -Arguments @("rm", "-f", $remoteDumpPath) | Out-Null
    }
}
