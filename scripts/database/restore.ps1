Import-Module (Join-Path $PSScriptRoot "PostgresBackupCommon.psm1")

# .\platform.ps1 restore -Backup latest
# .\platform.ps1 restore -Backup 2026-10-10_153000_ab12cd
# .\platform.ps1 restore -Backup latest -TargetDatabase investment_test_restore -AuthorizeDestructiveRestore
#
# On-demand, operator-confirmed restore. Never invoked automatically.
# Never runs Helm upgrades, image builds, deployments, Flyway migrations,
# first-ADMIN bootstrap or Radar cycles as a side effect of restoring --
# those only ever happen through the normal application startup path that
# already runs independently of this script.
#
# Existing-data detection and the -AuthorizeDestructiveRestore gate apply
# uniformly to ANY target database, regardless of its name -- a database
# is never treated as "disposable" merely because it is not named
# "investment". The only thing that depends on the target's name is
# whether application services (which only ever point at the configured
# production database) are scaled down first.

function Invoke-PlatformRestore {
    param(
        [Parameter(Mandatory = $true)][string]$ProjectRoot,
        [Parameter(Mandatory = $true)][string]$Namespace,
        [Parameter(Mandatory = $true)][string]$ClusterName,
        [Parameter(Mandatory = $true)][hashtable]$DatabaseConfig,
        [Parameter(Mandatory = $true)][string]$Backup,
        [Parameter(Mandatory = $true)][string[]]$DatabaseWriterServices,
        [string]$TargetDatabase,
        [switch]$AuthorizeDestructiveRestore,
        [switch]$SkipSafetyBackup,
        [switch]$ConfirmSafetyBackup
    )

    if ($SkipSafetyBackup -and $ConfirmSafetyBackup) {
        throw "Specify only one of -SkipSafetyBackup or -ConfirmSafetyBackup, not both -- this decision must be unambiguous."
    }

    Write-Step "Resolving and validating backup '$Backup'"
    $backupDir = Resolve-BackupDirectory -ProjectRoot $ProjectRoot -Backup $Backup
    $backupId = Split-Path -Leaf $backupDir
    $manifest = Read-BackupManifest -BackupDir $backupDir
    Assert-ValidManifest -Manifest $manifest

    # Archive-integrity verification (pg_restore --list against the dump
    # file) only proves the archive is structurally readable. It is kept
    # distinct from, and is not a substitute for, the full-restore
    # verification (Flyway history + per-table row-count comparison)
    # performed later in this function after the data has actually been
    # restored.
    $verified = Invoke-PlatformBackupVerify -ProjectRoot $ProjectRoot -Namespace $Namespace -Backup $backupId
    if (-not $verified) {
        throw "Refusing to restore backup '$backupId': it did not pass archive-integrity verification. See the failures reported above."
    }

    $podName = Get-PostgresPodName -Namespace $Namespace
    $dbUser = $DatabaseConfig.Username
    $effectiveTargetDb = if ($TargetDatabase) { $TargetDatabase } else { $DatabaseConfig.Name }
    Test-SafeIdentifier -Value $effectiveTargetDb -FieldName "Target database"
    Test-SafeIdentifier -Value $dbUser -FieldName "Database user"

    # Maintenance mode (scaling application writers to 0) only ever
    # applies when the target IS the configured production database --
    # that is the only database any live application pod is pointed at.
    $isProductionTarget = ($effectiveTargetDb -eq $DatabaseConfig.Name)

    $kubeContext = (kubectl config current-context 2>$null)

    Write-Host ""
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host " RESTORE PLAN" -ForegroundColor Cyan
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host "Backup              : $backupId"
    Write-Host "Source context      : $($manifest.sourceKubeContext)"
    Write-Host "Source namespace    : $($manifest.sourceNamespace)"
    Write-Host "Source PG version   : $($manifest.postgresServerVersion)"
    Write-Host "Target context      : $kubeContext"
    Write-Host "Target namespace    : $Namespace"
    Write-Host "Target database     : $effectiveTargetDb $(if ($isProductionTarget) { '(THE LIVE APPLICATION DATABASE)' } else { '(explicit, non-production target)' })"
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host ""

    $identityMismatch = ($manifest.sourceNamespace -ne $Namespace) -or
                         ($manifest.sourceKubeContext -and $kubeContext -and $manifest.sourceKubeContext -ne $kubeContext.Trim())
    if ($identityMismatch -and -not $AuthorizeDestructiveRestore) {
        throw "This backup's source environment differs from the current target environment. Re-run with -AuthorizeDestructiveRestore to explicitly approve restoring across environments."
    }

    Write-Step "Checking PostgreSQL version compatibility (heuristic -- not proof of full compatibility)"
    $targetServerVersion = Get-PostgresServerVersion -Namespace $Namespace -PodName $podName -DatabaseUser $dbUser
    $sourceMajor = ($manifest.postgresServerVersion -split '\.')[0]
    $targetMajor = ($targetServerVersion -split '\.')[0]
    if ($sourceMajor -ne $targetMajor) {
        Write-Warning "PostgreSQL major version differs: backup was taken from $($manifest.postgresServerVersion), target server is $targetServerVersion. A matching major version is not by itself proof that pg_restore will succeed cleanly across versions, and a non-matching one is not by itself proof that it will fail; this is a heuristic warning, not a guarantee either way."
        if (-not $AuthorizeDestructiveRestore) {
            throw "Refusing to continue across a PostgreSQL major-version difference without -AuthorizeDestructiveRestore."
        }
    }
    else {
        Write-Host "PostgreSQL major version matches ($targetServerVersion). This is a heuristic check, not proof the restore will succeed." -ForegroundColor Green
    }

    Write-Step "Checking Flyway/application schema compatibility against this checkout (heuristic -- not proof of compatibility)"
    foreach ($schema in $manifest.flywaySchemaVersions.PSObject.Properties.Name) {
        $backupVersion = $manifest.flywaySchemaVersions.$schema
        $migrationDir = Get-ChildItem -Path (Join-Path $ProjectRoot "services") -Recurse -Filter "V*__*.sql" -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -match "\\db\\migration\\" }
        $knownVersions = $migrationDir | ForEach-Object {
            if ($_.Name -match '^V(\d+(?:_\d+)*)__') { $matches[1] -replace '_', '.' }
        } | Where-Object { $_ }
        if ($knownVersions -and ($knownVersions -notcontains $backupVersion) -and ([version]($backupVersion -replace '_','.') -gt ($knownVersions | ForEach-Object { [version]($_ -replace '_','.') } | Sort-Object -Descending | Select-Object -First 1))) {
            Write-Warning "Schema '$schema': backup records Flyway version $backupVersion, which is newer than any migration present in this checkout. The currently checked-out application code may not understand the restored schema. This file-based check is a heuristic, not proof of compatibility."
            if (-not $AuthorizeDestructiveRestore) {
                throw "Refusing to continue: backup schema version for '$schema' is ahead of this checkout. Re-run with -AuthorizeDestructiveRestore once you have confirmed this is intentional."
            }
        }
    }
    Write-Host "Flyway schema compatibility check completed (heuristic)." -ForegroundColor Green

    Write-Step "Reliably determining existing state of target database '$effectiveTargetDb'"
    # Get-DatabaseExistence throws rather than guessing if psql itself
    # cannot be queried reliably, so by this point $existingState is
    # known to genuinely be one of MISSING / EMPTY / POPULATED.
    $existingState = Get-DatabaseExistence -Namespace $Namespace -PodName $podName -DatabaseUser $dbUser -DatabaseName $effectiveTargetDb
    $existingDataDetected = ($existingState -eq "POPULATED")

    if ($existingDataDetected -and -not $AuthorizeDestructiveRestore) {
        throw "Target database '$effectiveTargetDb' already contains application schema objects (reliably detected; this applies regardless of the database's name). Refusing to overwrite without -AuthorizeDestructiveRestore."
    }

    if (-not (Read-Host "Type RESTORE to confirm overwriting '$effectiveTargetDb' in namespace '$Namespace' with backup '$backupId'").Equals("RESTORE")) {
        Write-Host "Restore cancelled; no changes were made." -ForegroundColor Yellow
        return
    }

    $maintenanceStateFile = Join-Path (Join-Path $ProjectRoot ".tmp") "restore-maintenance-state.json"
    $originalReplicas = @{}
    $maintenanceModeEntered = $false

    try {
        if ($isProductionTarget) {
            Write-Step "Stopping database writers before a destructive restore (maintenance mode)"
            foreach ($service in $DatabaseWriterServices) {
                $replicas = kubectl get deployment $service -n $Namespace -o jsonpath='{.spec.replicas}' 2>$null
                if ($LASTEXITCODE -ne 0 -or -not $replicas) {
                    Write-Warning "Deployment '$service' not found or has no replica count; assuming it is not running and skipping it."
                    continue
                }
                $originalReplicas[$service] = [int]$replicas
            }

            $maintenanceModeEntered = $true
            Write-MaintenanceState -Path $maintenanceStateFile -BackupId $backupId -TargetDatabase $effectiveTargetDb `
                -Namespace $Namespace -OriginalReplicas $originalReplicas -Phase "SCALING_DOWN"

            foreach ($service in $originalReplicas.Keys) {
                kubectl scale deployment $service -n $Namespace --replicas=0 | Out-Host
                if ($LASTEXITCODE -ne 0) {
                    throw "Failed to scale deployment '$service' to 0 replicas (kubectl exit $LASTEXITCODE). Aborting before taking any destructive action."
                }
            }
            foreach ($service in $originalReplicas.Keys) {
                kubectl wait --for=delete pod -l "app=$service" -n $Namespace --timeout=120s 2>$null | Out-Null
                if ($LASTEXITCODE -ne 0) {
                    throw "Timed out waiting for all pod(s) of service '$service' to terminate after scaling to 0 replicas. Database-writing pods may still be running; refusing to proceed with a destructive restore."
                }
            }

            # Explicit re-check, independent of kubectl wait's own exit
            # code: confirm no non-terminated pod for any of these
            # services remains, rather than trusting a single signal.
            foreach ($service in $originalReplicas.Keys) {
                $remaining = kubectl get pod -n $Namespace -l "app=$service" -o jsonpath='{.items[?(@.status.phase!="Succeeded" && @.status.phase!="Failed")].metadata.name}' 2>$null
                if ($LASTEXITCODE -ne 0) {
                    throw "Unable to reliably confirm that service '$service' has no running pods (kubectl exit $LASTEXITCODE). Refusing to proceed with a destructive restore."
                }
                if ($remaining -and $remaining.Trim()) {
                    throw "Service '$service' still has running pod(s) ($($remaining.Trim())) after scale-down. Refusing to proceed with a destructive restore."
                }
            }

            Write-MaintenanceState -Path $maintenanceStateFile -BackupId $backupId -TargetDatabase $effectiveTargetDb `
                -Namespace $Namespace -OriginalReplicas $originalReplicas -Phase "WRITERS_STOPPED"

            if ($existingDataDetected) {
                $takeSafetyBackup = $false
                if ($SkipSafetyBackup) {
                    Write-Warning "Safety backup explicitly skipped (-SkipSafetyBackup)."
                }
                elseif ($ConfirmSafetyBackup) {
                    $takeSafetyBackup = $true
                }
                else {
                    $takeSafetyBackup = Read-ExplicitSafetyBackupApproval -TargetDatabase $effectiveTargetDb
                    if (-not $takeSafetyBackup) {
                        Write-Warning "Safety backup declined interactively. Proceeding without one."
                    }
                }

                if ($takeSafetyBackup) {
                    Write-Step "Taking an explicitly approved safety backup of '$effectiveTargetDb' before overwriting it"
                    $safetyDbConfig = @{ Name = $effectiveTargetDb; Username = $dbUser }
                    Invoke-PlatformBackup -ProjectRoot $ProjectRoot -Namespace $Namespace -ClusterName $ClusterName -DatabaseConfig $safetyDbConfig | Out-Null
                    Write-MaintenanceState -Path $maintenanceStateFile -BackupId $backupId -TargetDatabase $effectiveTargetDb `
                        -Namespace $Namespace -OriginalReplicas $originalReplicas -Phase "SAFETY_BACKUP_TAKEN"
                }
            }
        }
        else {
            Write-Host "Target '$effectiveTargetDb' is not the configured production database ('$($DatabaseConfig.Name)'); no application services will be scaled down." -ForegroundColor Yellow
        }

        if ($existingState -eq "MISSING") {
            Write-Step "Target database '$effectiveTargetDb' does not exist; creating it"
            $createResult = Invoke-PodExec -Namespace $Namespace -PodName $podName -Arguments @(
                "psql", "-U", $dbUser, "-d", "postgres", "-c", "create database `"$effectiveTargetDb`" owner `"$dbUser`";"
            )
            if ($createResult.ExitCode -ne 0) {
                throw "Failed to create target database '$effectiveTargetDb' (psql/CREATE DATABASE exit $($createResult.ExitCode)): $($createResult.Output)"
            }
        }

        Write-Step "Restoring '$($manifest.databases[0].fileName)' into '$effectiveTargetDb'"
        $dumpFile = Join-Path $backupDir $manifest.databases[0].fileName
        $remoteRestorePath = "/tmp/platform-restore-$backupId.dump"
        Copy-ToPostgresPod -Namespace $Namespace -PodName $podName -LocalPath $dumpFile -RemotePath $remoteRestorePath

        try {
            # --clean --if-exists drops existing objects before recreating
            # them; --no-owner --no-privileges prevents blindly copying
            # source-cluster role/ownership/privilege assignments onto the
            # target cluster.
            Revoke-FreshProvisioningMarker -Namespace $Namespace -PodName $podName -DatabaseUser $dbUser -DatabaseName $effectiveTargetDb
            $restoreResult = Invoke-PodExec -Namespace $Namespace -PodName $podName -Arguments @(
                "pg_restore", "--clean", "--if-exists", "--no-owner", "--no-privileges",
                "-U", $dbUser, "-d", $effectiveTargetDb, $remoteRestorePath
            )
            if ($restoreResult.ExitCode -ne 0) {
                throw "pg_restore reported a failure (exit $($restoreResult.ExitCode)): $($restoreResult.Output)"
            }
        }
        finally {
            $cleanupRestore = Invoke-PodExec -Namespace $Namespace -PodName $podName -Arguments @("rm", "-f", $remoteRestorePath)
            if ($cleanupRestore.ExitCode -ne 0) {
                Write-Warning "Unable to remove temporary restore file '$remoteRestorePath' from the PostgreSQL pod; it may need manual cleanup."
            }
        }

        Write-Step "Validating restored Flyway schema history"
        $restoredVersions = Get-FlywaySchemaVersions -Namespace $Namespace -PodName $podName -DatabaseName $effectiveTargetDb -DatabaseUser $dbUser
        if ($restoredVersions.Count -eq 0) {
            throw "Restore completed but no Flyway schema history could be read back from '$effectiveTargetDb'. Treating this as a failed restore."
        }

        Write-Step "Verifying restored data via per-table row-count comparison (full-restore verification -- distinct from, and in addition to, the archive-integrity check performed earlier)"
        $restoredRowCounts = Get-TableRowCounts -Namespace $Namespace -PodName $podName -DatabaseName $effectiveTargetDb -DatabaseUser $dbUser
        $expectedRowCounts = $manifest.databases[0].tableRowCounts
        $captureMethod = $manifest.databases[0].tableRowCountsCaptureMethod
        # Only a row-count snapshot captured under the synchronized
        # pg_export_snapshot() method (Invoke-SynchronizedDumpWithRowCounts)
        # is trustworthy for a strict comparison -- it is guaranteed
        # consistent with exactly what pg_dump wrote. A backup whose
        # manifest predates that method, or whose tableRowCounts was
        # recorded by an earlier, independently-sampled live query (not
        # guaranteed to match the dump's snapshot), is never compared: a
        # coincidental match there would be false confidence, not proof.
        $rowCountsTrustworthy = (Resolve-RowCountsTrustworthy -ManifestDatabase $manifest.databases[0])
        $rowCountMismatches = @()

        if ($rowCountsTrustworthy) {
            foreach ($tableKey in $restoredRowCounts.Keys) {
                if ($tableKey -notin $expectedRowCounts.PSObject.Properties.Name) {
                    $rowCountMismatches += "  Unexpected restored table: $tableKey"
                }
            }
            foreach ($tableKey in $expectedRowCounts.PSObject.Properties.Name) {
                $expected = [int64]$expectedRowCounts.$tableKey
                $actual = if ($restoredRowCounts.Contains($tableKey)) { [int64]$restoredRowCounts[$tableKey] } else { $null }
                if ($null -eq $actual -or $actual -ne $expected) {
                    $rowCountMismatches += "  $tableKey : expected $expected, got $(if ($null -eq $actual) { 'MISSING' } else { $actual })"
                }
            }
        }
        elseif ($expectedRowCounts) {
            Write-Warning "This backup's manifest has a recorded per-table row-count snapshot, but it was not captured under a synchronized PostgreSQL snapshot (capture method: '$captureMethod'). It is not guaranteed to match what pg_dump actually wrote, so it is not used for comparison. Verification status below reflects this."
        }
        else {
            Write-Warning "This backup's manifest has no recorded per-table row counts (it predates that feature); skipping row-count comparison. Verification status below reflects this."
        }

        if ($rowCountMismatches.Count -gt 0) {
            throw "Full-restore verification failed: restored row counts do not match the backup manifest for $($rowCountMismatches.Count) table(s):`n$($rowCountMismatches -join "`n")"
        }

        $fullRestoreVerified = $rowCountsTrustworthy

        Write-Host ""
        Write-Host "Restore completed successfully into '$effectiveTargetDb'." -ForegroundColor Green
        if ($fullRestoreVerified) {
            Write-Host "Verification        : FULL_RESTORE_VERIFIED (Flyway history present + all synchronized-snapshot table row counts match the backup manifest)" -ForegroundColor Green
        }
        elseif ($expectedRowCounts) {
            Write-Host "Verification        : FLYWAY_HISTORY_VERIFIED_ONLY (this backup's row-count snapshot was not captured under a synchronized PostgreSQL snapshot and was not compared)" -ForegroundColor Yellow
        }
        else {
            Write-Host "Verification        : FLYWAY_HISTORY_VERIFIED_ONLY (this backup's manifest predates row-count snapshots; row counts were not compared)" -ForegroundColor Yellow
        }
        Write-Host "Restored Flyway versions:" -ForegroundColor Green
        foreach ($schema in $restoredVersions.Keys) { Write-Host "  $schema -> $($restoredVersions[$schema])" }
        Write-Host ""
        Write-Host "Note: this script never triggers migrations, first-ADMIN bootstrap, or an initial Radar cycle. Those only run through normal application startup ('.\platform.ps1 up') -- restoring data here does not, by itself, start application pods." -ForegroundColor Yellow

        if ($maintenanceModeEntered) {
            Write-Step "Restore succeeded; restoring original replica counts for database-connected services"
            $resumedCleanly = Resume-ScaledServices -Namespace $Namespace -OriginalReplicas $originalReplicas
            if ($resumedCleanly) {
                Remove-MaintenanceState -Path $maintenanceStateFile
            }
            else {
                Write-Warning "Restore itself succeeded, but one or more services could not be resumed automatically. Maintenance state left at '$maintenanceStateFile' -- resume the listed service(s) manually (see warnings above), then delete that file."
            }
        }
    }
    catch {
        if ($maintenanceModeEntered) {
            Write-Host ""
            Write-Host "============================================================" -ForegroundColor Red
            Write-Host " RESTORE FAILED -- SYSTEM LEFT IN A DOCUMENTED MAINTENANCE STATE" -ForegroundColor Red
            Write-Host "============================================================" -ForegroundColor Red
            Write-Host "Database-connected service(s) ($($originalReplicas.Keys -join ', ')) were scaled to 0 replicas" -ForegroundColor Red
            Write-Host "and were NOT automatically resumed, because this restore failed or only partially" -ForegroundColor Red
            Write-Host "completed. Resuming writers against a possibly inconsistent database could cause" -ForegroundColor Red
            Write-Host "further damage." -ForegroundColor Red
            Write-Host ""
            Write-Host "Maintenance state recorded at: $maintenanceStateFile"
            Write-Host ""
            Write-Host "To recover:"
            Write-Host "  1. Inspect the target database '$effectiveTargetDb' manually and decide whether it is"
            Write-Host "     safe to resume service as-is, needs this restore repeated, or needs to be restored"
            Write-Host "     from the safety backup taken just before this attempt (if one was taken -- see the"
            Write-Host "     warnings above)."
            Write-Host "  2. Once you have confirmed the database is in a known-good state, resume services"
            Write-Host "     manually, for example:"
            foreach ($service in $originalReplicas.Keys) {
                Write-Host "       kubectl scale deployment $service -n $Namespace --replicas=$($originalReplicas[$service])"
            }
            Write-Host "  3. Delete the maintenance-state file once you have confirmed recovery:"
            Write-Host "       Remove-Item '$maintenanceStateFile'"
            Write-Host ""
        }
        throw
    }
}

function Write-MaintenanceState {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$BackupId,
        [Parameter(Mandatory = $true)][string]$TargetDatabase,
        [Parameter(Mandatory = $true)][string]$Namespace,
        [Parameter(Mandatory = $true)][hashtable]$OriginalReplicas,
        [Parameter(Mandatory = $true)][string]$Phase
    )
    $dir = Split-Path -Parent $Path
    if ($dir -and -not (Test-Path $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
    $state = [ordered]@{
        backupId         = $BackupId
        targetDatabase   = $TargetDatabase
        namespace        = $Namespace
        originalReplicas = $OriginalReplicas
        phase            = $Phase
        updatedAtUtc     = (Get-Date).ToUniversalTime().ToString("o")
    }
    $state | ConvertTo-Json -Depth 6 | Set-Content -Path $Path -Encoding UTF8
}

function Remove-MaintenanceState {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (Test-Path $Path) {
        Remove-Item -Path $Path -Force -ErrorAction SilentlyContinue
    }
}

function Read-ExplicitSafetyBackupApproval {
    param([Parameter(Mandatory = $true)][string]$TargetDatabase)
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        $answer = Read-Host "Take an explicit safety backup of '$TargetDatabase' before this destructive restore? [Y/n]"
        $trimmed = $answer.Trim()
        if ($trimmed -eq "" -or $trimmed -match '^(?i:y|yes)$') { return $true }
        if ($trimmed -match '^(?i:n|no)$') { return $false }
        Write-Warning "Please answer Y or N."
    }
    throw "No clear answer was given for whether to take a safety backup before this destructive restore. Re-run and answer explicitly, or pass -SkipSafetyBackup / -ConfirmSafetyBackup to decide this non-interactively."
}

function Resume-ScaledServices {
    param(
        [Parameter(Mandatory = $true)][string]$Namespace,
        [Parameter(Mandatory = $true)][hashtable]$OriginalReplicas
    )
    $allSucceeded = $true
    foreach ($service in $OriginalReplicas.Keys) {
        kubectl scale deployment $service -n $Namespace --replicas=$($OriginalReplicas[$service]) | Out-Host
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "Failed to resume deployment '$service' to $($OriginalReplicas[$service]) replica(s) (kubectl exit $LASTEXITCODE). It must be scaled back up manually."
            $allSucceeded = $false
        }
    }
    return $allSucceeded
}
