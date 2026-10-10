# .\platform.ps1 restore -Backup latest
# .\platform.ps1 restore -Backup 2026-10-10_153000_ab12cd
#
# On-demand, operator-confirmed restore. Never invoked automatically.
# Never runs Helm upgrades, image builds, deployments, Flyway migrations,
# first-ADMIN bootstrap or Radar cycles as a side effect of restoring --
# those only ever happen through the normal application startup path that
# already runs independently of this script.

function Invoke-PlatformRestore {
    param(
        [Parameter(Mandatory = $true)][string]$ProjectRoot,
        [Parameter(Mandatory = $true)][string]$Namespace,
        [Parameter(Mandatory = $true)][string]$ClusterName,
        [Parameter(Mandatory = $true)][hashtable]$DatabaseConfig,
        [Parameter(Mandatory = $true)][string]$Backup,
        [Parameter(Mandatory = $true)][string[]]$DatabaseJavaServices,
        [string]$TargetDatabase,
        [switch]$AuthorizeDestructiveRestore,
        [switch]$SkipSafetyBackup
    )

    Write-Step "Resolving and validating backup '$Backup'"
    $backupDir = Resolve-BackupDirectory -ProjectRoot $ProjectRoot -Backup $Backup
    $backupId = Split-Path -Leaf $backupDir
    $verified = Invoke-PlatformBackupVerify -ProjectRoot $ProjectRoot -Namespace $Namespace -Backup $backupId
    if (-not $verified) {
        throw "Refusing to restore backup '$backupId': it did not pass verification. See the failures reported above."
    }

    $manifest = Read-BackupManifest -BackupDir $backupDir
    $podName = Get-PostgresPodName -Namespace $Namespace
    $dbUser = $DatabaseConfig.Username
    $effectiveTargetDb = if ($TargetDatabase) { $TargetDatabase } else { $DatabaseConfig.Name }
    $isDisposableTarget = ($effectiveTargetDb -ne $DatabaseConfig.Name)
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
    Write-Host "Target database     : $effectiveTargetDb $(if ($isDisposableTarget) { '(disposable target)' } else { '(THE LIVE APPLICATION DATABASE)' })"
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host ""

    $identityMismatch = ($manifest.sourceNamespace -ne $Namespace) -or
                         ($manifest.sourceKubeContext -and $kubeContext -and $manifest.sourceKubeContext -ne $kubeContext.Trim())
    if ($identityMismatch -and -not $AuthorizeDestructiveRestore) {
        throw "This backup's source environment differs from the current target environment. Re-run with -AuthorizeDestructiveRestore to explicitly approve restoring across environments."
    }

    Write-Step "Checking PostgreSQL version compatibility"
    $targetServerVersion = Get-PostgresServerVersion -Namespace $Namespace -PodName $podName -DatabaseUser $dbUser
    $sourceMajor = ($manifest.postgresServerVersion -split '\.')[0]
    $targetMajor = ($targetServerVersion -split '\.')[0]
    if ($sourceMajor -ne $targetMajor) {
        Write-Warning "PostgreSQL major version differs: backup was taken from $($manifest.postgresServerVersion), target server is $targetServerVersion. pg_restore across major versions is not guaranteed to be compatible."
        if (-not $AuthorizeDestructiveRestore) {
            throw "Refusing to continue across a PostgreSQL major-version difference without -AuthorizeDestructiveRestore."
        }
    }
    else {
        Write-Host "PostgreSQL version compatible ($targetServerVersion)." -ForegroundColor Green
    }

    Write-Step "Checking Flyway/application schema compatibility against this checkout"
    foreach ($schema in $manifest.flywaySchemaVersions.PSObject.Properties.Name) {
        $backupVersion = $manifest.flywaySchemaVersions.$schema
        $migrationDir = Get-ChildItem -Path (Join-Path $ProjectRoot "services") -Recurse -Filter "V*__*.sql" -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -match "\\db\\migration\\" }
        $knownVersions = $migrationDir | ForEach-Object {
            if ($_.Name -match '^V(\d+(?:_\d+)*)__') { $matches[1] -replace '_', '.' }
        } | Where-Object { $_ }
        if ($knownVersions -and ($knownVersions -notcontains $backupVersion) -and ([version]($backupVersion -replace '_','.') -gt ($knownVersions | ForEach-Object { [version]($_ -replace '_','.') } | Sort-Object -Descending | Select-Object -First 1))) {
            Write-Warning "Schema '$schema': backup records Flyway version $backupVersion, which is newer than any migration present in this checkout. The currently checked-out application code may not understand the restored schema."
            if (-not $AuthorizeDestructiveRestore) {
                throw "Refusing to continue: backup schema version for '$schema' is ahead of this checkout. Re-run with -AuthorizeDestructiveRestore once you have confirmed this is intentional."
            }
        }
    }
    Write-Host "Flyway schema compatibility check completed." -ForegroundColor Green

    $existingDataDetected = $false
    if (-not $isDisposableTarget) {
        Write-Step "Detecting existing data in target database '$effectiveTargetDb'"
        $countResult = Invoke-PodExec -Namespace $Namespace -PodName $podName -Arguments @(
            "psql", "-U", $dbUser, "-d", $effectiveTargetDb, "-At", "-c",
            "select coalesce(sum(n), 0) from (select count(*) as n from information_schema.tables where table_schema not in ('pg_catalog','information_schema')) t;"
        )
        if ($countResult.ExitCode -eq 0 -and [int]($countResult.Output.Trim()) -gt 0) {
            $existingDataDetected = $true
        }

        if ($existingDataDetected -and -not $AuthorizeDestructiveRestore) {
            throw "Target database '$effectiveTargetDb' already contains application schema objects. Refusing to overwrite without -AuthorizeDestructiveRestore."
        }
    }

    $originalReplicas = @{}

    if (-not (Read-Host "Type RESTORE to confirm overwriting '$effectiveTargetDb' in namespace '$Namespace' with backup '$backupId'").Equals("RESTORE")) {
        Write-Host "Restore cancelled; no changes were made." -ForegroundColor Yellow
        return
    }

    if ($existingDataDetected) {
        Write-Step "Stopping database writers before a destructive restore (maintenance mode)"
        foreach ($service in $DatabaseJavaServices) {
            $replicas = kubectl get deployment $service -n $Namespace -o jsonpath='{.spec.replicas}' 2>$null
            if ($LASTEXITCODE -eq 0 -and $replicas) {
                $originalReplicas[$service] = [int]$replicas
                kubectl scale deployment $service -n $Namespace --replicas=0 | Out-Host
            }
        }
        kubectl scale deployment research-engine -n $Namespace --replicas=0 2>$null | Out-Null
        foreach ($service in $originalReplicas.Keys) {
            kubectl wait --for=delete pod -l "app=$service" -n $Namespace --timeout=60s 2>$null | Out-Null
        }

        if (-not $SkipSafetyBackup) {
            Write-Step "Taking an explicit safety backup of the current (about-to-be-overwritten) database"
            Invoke-PlatformBackup -ProjectRoot $ProjectRoot -Namespace $Namespace -ClusterName $ClusterName -DatabaseConfig $DatabaseConfig | Out-Null
        }
        else {
            Write-Warning "Safety backup skipped by request (-SkipSafetyBackup). Proceeding without one."
        }
    }

    try {
        Write-Step "Restoring '$($manifest.databases[0].fileName)' into '$effectiveTargetDb'"
        $dumpFile = Join-Path $backupDir $manifest.databases[0].fileName
        $remoteRestorePath = "/tmp/platform-restore-$backupId.dump"
        Copy-ToPostgresPod -Namespace $Namespace -PodName $podName -LocalPath $dumpFile -RemotePath $remoteRestorePath

        if ($isDisposableTarget) {
            Invoke-PodExec -Namespace $Namespace -PodName $podName -Arguments @(
                "psql", "-U", $dbUser, "-d", "postgres", "-c", "create database $effectiveTargetDb owner $dbUser;"
            ) | Out-Null
        }

        # --clean --if-exists drops existing objects before recreating them;
        # --no-owner --no-privileges prevents blindly copying source-cluster
        # role/ownership/privilege assignments onto the target cluster.
        $restoreResult = Invoke-PodExec -Namespace $Namespace -PodName $podName -Arguments @(
            "pg_restore", "--clean", "--if-exists", "--no-owner", "--no-privileges",
            "-U", $dbUser, "-d", $effectiveTargetDb, $remoteRestorePath
        )
        Invoke-PodExec -Namespace $Namespace -PodName $podName -Arguments @("rm", "-f", $remoteRestorePath) | Out-Null

        if ($restoreResult.ExitCode -ne 0) {
            throw "pg_restore reported a failure: $($restoreResult.Output)"
        }

        Write-Step "Validating restored data and Flyway history"
        $restoredVersions = Get-FlywaySchemaVersions -Namespace $Namespace -PodName $podName -DatabaseName $effectiveTargetDb -DatabaseUser $dbUser
        if ($restoredVersions.Count -eq 0) {
            throw "Restore completed but no Flyway schema history could be read back from '$effectiveTargetDb'. Treating this as a failed restore."
        }

        Write-Host ""
        Write-Host "Restore completed successfully into '$effectiveTargetDb'." -ForegroundColor Green
        Write-Host "Restored Flyway versions:" -ForegroundColor Green
        foreach ($schema in $restoredVersions.Keys) { Write-Host "  $schema -> $($restoredVersions[$schema])" }
        Write-Host ""
        Write-Host "Note: this script never triggers migrations, first-ADMIN bootstrap, or an initial Radar cycle. Those only run through normal application startup ('.\platform.ps1 up') -- restoring data here does not, by itself, start application pods." -ForegroundColor Yellow
    }
    finally {
        if ($existingDataDetected) {
            Resume-ScaledServices -Namespace $Namespace -OriginalReplicas $originalReplicas
        }
    }
}

function Resume-ScaledServices {
    param(
        [Parameter(Mandatory = $true)][string]$Namespace,
        [Parameter(Mandatory = $true)][hashtable]$OriginalReplicas
    )
    Write-Step "Restoring original replica counts for database-connected services"
    foreach ($service in $OriginalReplicas.Keys) {
        kubectl scale deployment $service -n $Namespace --replicas=$($OriginalReplicas[$service]) | Out-Host
    }
    kubectl scale deployment research-engine -n $Namespace --replicas=1 2>$null | Out-Null
}
