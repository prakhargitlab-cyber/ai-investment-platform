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

    $explicit = Join-Path $root $Backup
    if (-not (Test-Path $explicit)) {
        throw "Backup '$Backup' was not found under '$root'."
    }
    return $explicit
}

