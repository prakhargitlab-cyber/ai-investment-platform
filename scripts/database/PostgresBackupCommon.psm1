# Shared helpers for platform.ps1's on-demand PostgreSQL backup/restore
# commands (backup, backups, backup-verify, restore). Windows PowerShell
# 5.1 compatible. No automatic/scheduled invocation lives here or anywhere
# else in this module: every function is only ever called from an explicit
# platform.ps1 command the operator runs by hand.

function Get-PostgresPodName {
    param([Parameter(Mandatory = $true)][string]$Namespace)
    $pod = kubectl get pods -n $Namespace -l "app.kubernetes.io/component=postgres" -o jsonpath='{.items[0].metadata.name}' 2>$null
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($pod)) {
        throw "Unable to find the running PostgreSQL pod in namespace '$Namespace' (label app.kubernetes.io/component=postgres). Is the platform deployed and healthy?"
    }
    return $pod.Trim()
}

function Invoke-PodExec {
    # Runs a command inside the PostgreSQL pod and returns its stdout as text.
    # Only for short, text-only commands (version probes, catalog queries,
    # file removal) -- never for streaming binary dump/restore data, which
    # must go through Copy-FromPostgresPod / Copy-ToPostgresPod instead.
    param(
        [Parameter(Mandatory = $true)][string]$Namespace,
        [Parameter(Mandatory = $true)][string]$PodName,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )
    $output = & kubectl exec $PodName -n $Namespace -- @Arguments 2>&1
    return [PSCustomObject]@{ Output = ($output | Out-String); ExitCode = $LASTEXITCODE }
}

function Copy-FromPostgresPod {
    # Binary-safe pull of a single file out of the pod. kubectl cp streams
    # the file as a tar archive over the exec protocol; it does not pass
    # through a PowerShell text pipeline, so it cannot corrupt a binary
    # pg_dump archive the way `kubectl exec ... | Set-Content` would.
    param(
        [Parameter(Mandatory = $true)][string]$Namespace,
        [Parameter(Mandatory = $true)][string]$PodName,
        [Parameter(Mandatory = $true)][string]$RemotePath,
        [Parameter(Mandatory = $true)][string]$LocalPath
    )
    kubectl cp "${Namespace}/${PodName}:${RemotePath}" $LocalPath | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "kubectl cp failed while pulling '$RemotePath' from pod '$PodName' to '$LocalPath'."
    }
}

function Copy-ToPostgresPod {
    param(
        [Parameter(Mandatory = $true)][string]$Namespace,
        [Parameter(Mandatory = $true)][string]$PodName,
        [Parameter(Mandatory = $true)][string]$LocalPath,
        [Parameter(Mandatory = $true)][string]$RemotePath
    )
    kubectl cp $LocalPath "${Namespace}/${PodName}:${RemotePath}" | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "kubectl cp failed while pushing '$LocalPath' to pod '$PodName' path '$RemotePath'."
    }
}

function Get-FileSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)
    return (Get-FileHash -Path $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-FlywaySchemaVersions {
    # Reads the latest applied Flyway version for every per-service schema
    # history table present in the database, without exposing row contents
    # beyond version/description/success -- no sensitive data.
    param(
        [Parameter(Mandatory = $true)][string]$Namespace,
        [Parameter(Mandatory = $true)][string]$PodName,
        [Parameter(Mandatory = $true)][string]$DatabaseName,
        [Parameter(Mandatory = $true)][string]$DatabaseUser
    )
    $sql = @'
select table_schema, table_name
from information_schema.tables
where table_name like 'flyway_schema_history%';
'@
    $tablesResult = Invoke-PodExec -Namespace $Namespace -PodName $PodName -Arguments @(
        "psql", "-U", $DatabaseUser, "-d", $DatabaseName, "-At", "-c", $sql
    )
    if ($tablesResult.ExitCode -ne 0) {
        throw "Unable to enumerate Flyway schema history tables: $($tablesResult.Output)"
    }

    $versions = [ordered]@{}
    foreach ($line in ($tablesResult.Output -split "`n")) {
        $line = $line.Trim()
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        $parts = $line -split '\|'
        if ($parts.Count -ne 2) { continue }
        $schema = $parts[0].Trim()
        $table = $parts[1].Trim()
        $versionSql = "select version from $schema.$table where success = true order by installed_rank desc limit 1;"
        $versionResult = Invoke-PodExec -Namespace $Namespace -PodName $PodName -Arguments @(
            "psql", "-U", $DatabaseUser, "-d", $DatabaseName, "-At", "-c", $versionSql
        )
        if ($versionResult.ExitCode -eq 0) {
            $versions[$schema] = $versionResult.Output.Trim()
        }
    }
    return $versions
}

function Get-TableRowCounts {
    # Snapshots a per-schema/per-table row count across every
    # application-owned table (anything not a Postgres/information_schema
    # internal). Recorded in the backup manifest so a later restore can
    # compare against these exact counts as a concrete, non-heuristic
    # confirmation of a successful full restore -- distinct from
    # archive-integrity verification, which only proves the dump file
    # itself is structurally readable.
    param(
        [Parameter(Mandatory = $true)][string]$Namespace,
        [Parameter(Mandatory = $true)][string]$PodName,
        [Parameter(Mandatory = $true)][string]$DatabaseName,
        [Parameter(Mandatory = $true)][string]$DatabaseUser
    )
    $sql = @'
select table_schema, table_name
from information_schema.tables
where table_schema not in ('pg_catalog', 'information_schema')
  and table_type = 'BASE TABLE';
'@
    $tablesResult = Invoke-PodExec -Namespace $Namespace -PodName $PodName -Arguments @(
        "psql", "-U", $DatabaseUser, "-d", $DatabaseName, "-At", "-c", $sql
    )
    if ($tablesResult.ExitCode -ne 0) {
        throw "Unable to enumerate application tables for row-count snapshot: $($tablesResult.Output)"
    }

    $rowCounts = [ordered]@{}
    foreach ($line in ($tablesResult.Output -split "`n")) {
        $line = $line.Trim()
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        $parts = $line -split '\|'
        if ($parts.Count -ne 2) { continue }
        $schema = $parts[0].Trim()
        $table = $parts[1].Trim()

        # Belt-and-suspenders: even though these names came from
        # information_schema (not operator/user input), validate them as
        # safe identifiers before interpolating into SQL, in case a future
        # caller reuses this helper against a different, less trusted
        # source of schema/table names.
        if ($schema -notmatch '^[A-Za-z_][A-Za-z0-9_]{0,62}$' -or $table -notmatch '^[A-Za-z_][A-Za-z0-9_]{0,62}$') {
            Write-Warning "Skipping row-count snapshot for unsafe identifier pair ('$schema'.'$table')."
            continue
        }

        $countSql = "select count(*) from `"$schema`".`"$table`";"
        $countResult = Invoke-PodExec -Namespace $Namespace -PodName $PodName -Arguments @(
            "psql", "-U", $DatabaseUser, "-d", $DatabaseName, "-At", "-c", $countSql
        )
        if ($countResult.ExitCode -ne 0) {
            Write-Warning "Unable to count rows in '$schema.$table' for the backup manifest's row-count snapshot: $($countResult.Output)"
            continue
        }
        $count = 0
        if ([int64]::TryParse($countResult.Output.Trim(), [ref]$count)) {
            $rowCounts["$schema.$table"] = $count
        }
    }
    return $rowCounts
}

function Invoke-SynchronizedDumpWithRowCounts {
    # Runs pg_dump and a full per-table row-count snapshot against the
    # EXACT SAME PostgreSQL snapshot, so the row counts recorded in the
    # backup manifest are guaranteed consistent with what pg_dump actually
    # wrote into the archive -- never an independently-sampled live count
    # taken at some other, later point in time (which could race with
    # concurrent writes against a database that is NOT scaled down during
    # an on-demand backup).
    #
    # Snapshot lifetime (fully contained in the generated script below,
    # a single psql session/transaction, run once via one kubectl exec):
    #   1. BEGIN ISOLATION LEVEL REPEATABLE READ opens a transaction whose
    #      snapshot is fixed at its first query.
    #   2. SELECT pg_export_snapshot() (that first query) fixes the
    #      snapshot and exports its identifier for another process to use.
    #   3. \! pg_dump --snapshot=<id> runs pg_dump as a subprocess of the
    #      SAME psql client process, against that exact exported snapshot,
    #      without ending the holding session/transaction. A `\!` shell
    #      command's own exit status is not visible to psql's
    #      ON_ERROR_STOP, so success is confirmed explicitly via a
    #      touch-on-success flag file, checked after psql exits.
    #   4. Still inside the SAME open transaction (same snapshot), one
    #      SELECT per table counts its rows -- guaranteed to see exactly
    #      what pg_dump dumped, not whatever became live afterward.
    #   5. COMMIT releases the snapshot. The holding transaction only ever
    #      reads, so COMMIT vs. ROLLBACK is immaterial.
    # `\set ON_ERROR_STOP on` means any SQL failure (the snapshot export,
    # or any single table's count) aborts the whole script immediately
    # rather than silently continuing on to COMMIT as if it had succeeded.
    # Every enumerated table must report a count, or this throws: no
    # table's failed count, and no unsafe/unsupported identifier, is ever
    # silently skipped.
    param(
        [Parameter(Mandatory = $true)][string]$Namespace,
        [Parameter(Mandatory = $true)][string]$PodName,
        [Parameter(Mandatory = $true)][string]$DatabaseUser,
        [Parameter(Mandatory = $true)][string]$DatabaseName,
        [Parameter(Mandatory = $true)][string]$RemoteDumpPath,
        [Parameter(Mandatory = $true)][string]$BackupId
    )

    Test-SafeIdentifier -Value $DatabaseUser -FieldName "Database user"
    Test-SafeIdentifier -Value $DatabaseName -FieldName "Database name"
    Test-SafeBackupId -BackupId $BackupId

    $tablesResult = Invoke-PodExec -Namespace $Namespace -PodName $PodName -Arguments @(
        "psql", "-U", $DatabaseUser, "-d", $DatabaseName, "-At", "-c",
        "select table_schema, table_name from information_schema.tables where table_schema not in ('pg_catalog','information_schema') and table_type = 'BASE TABLE' order by 1, 2;"
    )
    if ($tablesResult.ExitCode -ne 0) {
        throw "Unable to enumerate application tables before the synchronized backup capture: $($tablesResult.Output)"
    }

    $tableKeys = New-Object System.Collections.Generic.List[string]
    $countSelects = New-Object System.Collections.Generic.List[string]
    foreach ($line in ($tablesResult.Output -split "`n")) {
        $line = $line.Trim()
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        $parts = $line -split '\|'
        if ($parts.Count -ne 2) {
            throw "Unexpected output while enumerating application tables for the synchronized backup capture: '$line'"
        }
        $schema = $parts[0].Trim()
        $table = $parts[1].Trim()
        # Do not silently skip an unsupported/unsafe table identifier --
        # refuse the whole backup instead, per the explicit requirement
        # that failed table counts and unsupported identifiers are never
        # dropped quietly.
        Test-SafeIdentifier -Value $schema -FieldName "Table schema"
        Test-SafeIdentifier -Value $table -FieldName "Table name"
        $tableKeys.Add("$schema.$table")
        $countSelects.Add("SELECT '$schema.$table|' || count(*) FROM `"$schema`".`"$table`";")
    }

    if ($tableKeys.Count -eq 0) {
        throw "No application tables were found in database '$DatabaseName'; refusing to produce a backup with no verifiable row-count snapshot."
    }

    $okFlag = "/tmp/platform-backup-pgdump-ok-$BackupId.flag"
    $sqlScript = @"
\set ON_ERROR_STOP on
BEGIN ISOLATION LEVEL REPEATABLE READ;
SELECT pg_export_snapshot() AS snap
\gset
\! pg_dump --snapshot=':snap' -Fc -U $DatabaseUser -d $DatabaseName -f $RemoteDumpPath && touch $okFlag
$($countSelects -join "`n")
COMMIT;
"@

    $shScript = "psql -U $DatabaseUser -d $DatabaseName -At <<'PLATFORM_SQL_EOF'`n$sqlScript`nPLATFORM_SQL_EOF`ntest -f $okFlag"

    try {
        $result = Invoke-PodExec -Namespace $Namespace -PodName $PodName -Arguments @("sh", "-c", $shScript)
        if ($result.ExitCode -ne 0) {
            throw "Synchronized backup capture (pg_dump + row-count snapshot under one PostgreSQL snapshot) failed: $($result.Output)"
        }

        $outputLines = @($result.Output -split "`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ })
        $tableRowCounts = [ordered]@{}
        foreach ($line in $outputLines) {
            $sep = $line.LastIndexOf('|')
            if ($sep -lt 0) {
                throw "Unexpected row-count output line from the synchronized backup capture (missing '|'): '$line'"
            }
            $key = $line.Substring(0, $sep)
            $valueText = $line.Substring($sep + 1)
            $value = 0L
            if (-not [long]::TryParse($valueText, [ref]$value)) {
                throw "Unexpected row-count output line from the synchronized backup capture (non-numeric count): '$line'"
            }
            $tableRowCounts[$key] = $value
        }

        foreach ($key in $tableKeys) {
            if (-not $tableRowCounts.Contains($key)) {
                throw "The synchronized row-count capture did not report a count for table '$key'; refusing to treat this backup's row-count snapshot as complete."
            }
        }
        if ($tableRowCounts.Count -ne $tableKeys.Count) {
            throw "The synchronized row-count capture reported $($tableRowCounts.Count) table(s) but $($tableKeys.Count) were enumerated; refusing to treat this backup's row-count snapshot as complete."
        }

        return $tableRowCounts
    }
    finally {
        Invoke-PodExec -Namespace $Namespace -PodName $PodName -Arguments @("rm", "-f", $okFlag) | Out-Null
    }
}

function Get-PostgresServerVersion {
    param(
        [Parameter(Mandatory = $true)][string]$Namespace,
        [Parameter(Mandatory = $true)][string]$PodName,
        [Parameter(Mandatory = $true)][string]$DatabaseUser
    )
    $result = Invoke-PodExec -Namespace $Namespace -PodName $PodName -Arguments @(
        "psql", "-U", $DatabaseUser, "-At", "-c", "show server_version;"
    )
    if ($result.ExitCode -ne 0) {
        throw "Unable to read PostgreSQL server version: $($result.Output)"
    }
    return $result.Output.Trim()
}

function Get-CurrentGitCommit {
    param([Parameter(Mandatory = $true)][string]$ProjectRoot)
    $previousLocation = Get-Location
    try {
        Set-Location $ProjectRoot
        $commit = git rev-parse HEAD 2>$null
        if ($LASTEXITCODE -ne 0) { return $null }
        return $commit.Trim()
    }
    finally {
        Set-Location $previousLocation
    }
}

function New-BackupId {
    # UTC timestamp directory name with a short unique suffix to avoid
    # collisions, per the required backups/postgres/<id>/ layout.
    $timestamp = (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd_HHmmss")
    $suffix = ([guid]::NewGuid().ToString("N")).Substring(0, 6)
    return "${timestamp}_$suffix"
}

function Get-BackupsRoot {
    param([Parameter(Mandatory = $true)][string]$ProjectRoot)
    $root = Join-Path $ProjectRoot "backups\postgres"
    if (-not (Test-Path $root)) {
        New-Item -ItemType Directory -Path $root -Force | Out-Null
    }
    return $root
}

function Read-BackupManifest {
    param([Parameter(Mandatory = $true)][string]$BackupDir)
    $manifestPath = Join-Path $BackupDir "manifest.json"
    if (-not (Test-Path $manifestPath)) {
        throw "No manifest.json found in '$BackupDir'. This directory is not a valid backup."
    }
    try {
        return (Get-Content -Path $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json)
    }
    catch {
        throw "manifest.json in '$BackupDir' is not valid JSON: $($_.Exception.Message)"
    }
}

function Resolve-BackupDirectory {
    # Resolves "-Backup latest" or an explicit backup id to a concrete,
    # existing directory under backups/postgres/. Never falls back to raw
    # filesystem listing order; "latest" is decided from validated manifest
    # metadata (createdAtUtc), matching the "do not depend on filesystem
    # creation time alone" requirement.
    param(
        [Parameter(Mandatory = $true)][string]$ProjectRoot,
        [Parameter(Mandatory = $true)][string]$Backup
    )
    $root = Get-BackupsRoot -ProjectRoot $ProjectRoot

    if ($Backup -eq "latest") {
        $candidates = Get-ChildItem -Path $root -Directory -ErrorAction SilentlyContinue
        $withManifests = foreach ($dir in $candidates) {
            try {
                $manifest = Read-BackupManifest -BackupDir $dir.FullName
                [PSCustomObject]@{ Dir = $dir.FullName; CreatedAtUtc = [datetime]$manifest.createdAtUtc }
            }
            catch { }
        }
        $latest = $withManifests | Sort-Object CreatedAtUtc -Descending | Select-Object -First 1
        if (-not $latest) {
            throw "No valid backups found under '$root'. Run '.\platform.ps1 backup' first."
        }
        return $latest.Dir
    }

    # Validate before it is ever joined into a filesystem path: an
    # unvalidated -Backup value could otherwise contain path-traversal
    # sequences (e.g. "..\\..\\Windows") and escape the backups root.
    Test-SafeBackupId -BackupId $Backup

    $explicit = Join-Path $root $Backup
    if (-not (Test-Path $explicit)) {
        throw "Backup '$Backup' was not found under '$root'."
    }
    return $explicit
}

function Test-SafeBackupId {
    # Backup ids are only ever generated by New-BackupId (yyyy-MM-dd_HHmmss
    # plus a 6-hex-character suffix). Reject anything else before it is used
    # in a filesystem path, a remote pod path, or a SQL/shell argument.
    param([Parameter(Mandatory = $true)][string]$BackupId)
    if ($BackupId -notmatch '^[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{6}_[0-9a-f]{6}$') {
        throw "Backup id '$BackupId' does not match the expected format (yyyy-MM-dd_HHmmss_xxxxxx) and will not be used in any path or command."
    }
}

function Test-SafeIdentifier {
    # A conservative, explicit allowlist for anything interpolated into a
    # SQL statement or a remote filesystem path: a PostgreSQL-safe
    # lowercase-start identifier, max 63 characters (PostgreSQL's own
    # NAMEDATALEN limit). Rejects quotes, semicolons, path separators,
    # whitespace, and SQL/shell metacharacters outright.
    param(
        [Parameter(Mandatory = $true)][string]$Value,
        [Parameter(Mandatory = $true)][string]$FieldName
    )
    if ($Value -notmatch '^[A-Za-z_][A-Za-z0-9_]{0,62}$') {
        throw "$FieldName '$Value' is not a safe identifier (expected ^[A-Za-z_][A-Za-z0-9_]{0,62}`$). Refusing to use it in SQL or a remote path."
    }
}

function Get-DatabaseExistence {
    # Reliably determines whether a database exists and, if so, whether it
    # already contains application schema objects. Throws rather than
    # guessing when either question cannot be answered (a failed psql call
    # must never be silently read as "does not exist" or "is empty").
    # Returns one of: "MISSING", "EMPTY", "POPULATED".
    param(
        [Parameter(Mandatory = $true)][string]$Namespace,
        [Parameter(Mandatory = $true)][string]$PodName,
        [Parameter(Mandatory = $true)][string]$DatabaseUser,
        [Parameter(Mandatory = $true)][string]$DatabaseName
    )
    Test-SafeIdentifier -Value $DatabaseName -FieldName "Database name"
    Test-SafeIdentifier -Value $DatabaseUser -FieldName "Database user"

    $existsResult = Invoke-PodExec -Namespace $Namespace -PodName $PodName -Arguments @(
        "psql", "-U", $DatabaseUser, "-d", "postgres", "-At", "-c",
        "select 1 from pg_database where datname = '$DatabaseName';"
    )
    if ($existsResult.ExitCode -ne 0) {
        throw "Unable to reliably determine whether database '$DatabaseName' exists (psql exit $($existsResult.ExitCode)): $($existsResult.Output)"
    }
    if ($existsResult.Output.Trim() -ne "1") {
        return "MISSING"
    }

    $countResult = Invoke-PodExec -Namespace $Namespace -PodName $PodName -Arguments @(
        "psql", "-U", $DatabaseUser, "-d", $DatabaseName, "-At", "-c",
        "select count(*) from information_schema.tables where table_schema not in ('pg_catalog','information_schema');"
    )
    if ($countResult.ExitCode -ne 0) {
        throw "Database '$DatabaseName' exists but its contents could not be reliably inspected (psql exit $($countResult.ExitCode)): $($countResult.Output)"
    }

    $count = 0
    if (-not [int]::TryParse($countResult.Output.Trim(), [ref]$count)) {
        throw "Database '$DatabaseName' returned a non-numeric table count ('$($countResult.Output.Trim())'); refusing to treat its contents as known."
    }
    if ($count -gt 0) { return "POPULATED" } else { return "EMPTY" }
}

function Assert-ValidManifest {
    # Validates the required shape of a backup manifest before it is
    # published (backup.ps1) or relied upon (restore.ps1/verify-backup.ps1).
    param([Parameter(Mandatory = $true)]$Manifest)
    if (-not $Manifest.backupId) { throw "manifest.backupId is missing." }
    Test-SafeBackupId -BackupId $Manifest.backupId
    if (-not $Manifest.createdAtUtc) { throw "manifest.createdAtUtc is missing." }
    try { [datetime]$Manifest.createdAtUtc | Out-Null } catch { throw "manifest.createdAtUtc ('$($Manifest.createdAtUtc)') is not a valid timestamp." }
    if (-not $Manifest.databases -or @($Manifest.databases).Count -eq 0) { throw "manifest.databases is empty." }
    foreach ($db in @($Manifest.databases)) {
        if (-not $db.name) { throw "A database entry in the manifest is missing 'name'." }
        if (-not $db.fileName) { throw "Database '$($db.name)': manifest entry is missing 'fileName'." }
        if ($null -eq $db.sizeBytes -or [int64]$db.sizeBytes -le 0) { throw "Database '$($db.name)': manifest 'sizeBytes' is missing or not positive." }
        if (-not $db.sha256 -or $db.sha256 -notmatch '^[0-9a-f]{64}$') { throw "Database '$($db.name)': manifest 'sha256' is missing or not a 64-character hex SHA-256." }
        if ($db.fileName -match '[\\/]' -or $db.fileName -match '\.\.') { throw "Database '$($db.name)': manifest 'fileName' ('$($db.fileName)') is not a safe bare filename." }
    }
}
