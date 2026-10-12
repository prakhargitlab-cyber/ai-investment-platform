# Real PostgreSQL validation, independent of Pester and Kubernetes.
# Creates only a uniquely named, network-isolated container with temporary PGDATA.
param([string]$Image = 'postgres:16')
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot '..\PostgresBackupCommon.psm1') -Force
$container = 'stage2c-snapshot-' + [guid]::NewGuid().ToString('N')
$scratch = Join-Path ([IO.Path]::GetTempPath()) $container
New-Item -ItemType Directory $scratch | Out-Null
$initSql = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..\infrastructure\helm\ai-investment-platform\files\fresh-provisioning.sql')).Path
function Invoke-Container {
    param([string[]]$Arguments, [switch]$ExpectFailure)
    # PS5 treats native stderr as ErrorRecord; preserve the actual process code.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { $output = & docker exec $container @Arguments 2>&1; $code = $LASTEXITCODE }
    finally { $ErrorActionPreference = $previous }
    if ($ExpectFailure) {
        if ($code -eq 0) { throw "Expected failure: $Arguments" }
        return $code
    }
    if ($code -ne 0) { throw "Container command failed ($code): $output" }
    return ($output | Out-String).Trim().Replace("`r`n", "`n")
}
function Send-File {
    param([string]$Name, [string]$Content)
    $local = Join-Path $scratch $Name
    [IO.File]::WriteAllText($local, $Content.Replace("`r`n", "`n"), (New-Object Text.UTF8Encoding $false))
    & docker cp $local "${container}:/tmp/$Name"
    if ($LASTEXITCODE -ne 0) { throw 'docker cp failed' }
}
function Assert-Equal($Actual, $Expected, [string]$Message) {
    if ($Actual -cne $Expected) { throw "$Message : expected [$Expected], got [$Actual]" }
}
try {
    & docker run -d --name $container --network none --tmpfs /var/lib/postgresql/data `
        --mount "type=bind,source=$initSql,target=/docker-entrypoint-initdb.d/10-fresh-provisioning.sql,readonly" `
        -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_USER=tester -e POSTGRES_DB=fixture $Image | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Unable to create disposable PostgreSQL container' }
    $ready = $false
    for ($i = 0; $i -lt 60; $i++) {
        & docker exec $container pg_isready -h 127.0.0.1 -U tester -d fixture *> $null
        if ($LASTEXITCODE -eq 0) {
            # Require final server, not the temporary initdb server.
            $ready = (Invoke-Container @('psql','-XqAt','-U','tester','-d','postgres','-c', "select count(*) from platform_bootstrap.provisioning;")) -eq '1'
            if ($ready) { break }
        }
        Start-Sleep -Seconds 1
    }
    if (-not $ready) { throw 'Disposable PostgreSQL did not initialize' }
    Invoke-Container @('psql','-XqAt','-v','ON_ERROR_STOP=1','-U','tester','-d','fixture','-c',
        'CREATE TABLE accounts(id int); INSERT INTO accounts VALUES (1),(2); CREATE TABLE empty_table(id int); CREATE TABLE flyway_schema_history(version text); INSERT INTO flyway_schema_history VALUES (''1'');') | Out-Null
    Invoke-Container @('mkdir','-p','/tmp/bin') | Out-Null
    # The real pg_dump is wrapped only to introduce a committed concurrent write
    # AFTER export and record its actual snapshot argument. No SQL is mocked.
    Send-File 'pg_dump' @'
#!/bin/sh
set -eu
printf '%s\n' "$@" > /tmp/dump-arguments
printf '%s' "$PLATFORM_SNAPSHOT" > /tmp/exported-snapshot
psql -XqAt -v ON_ERROR_STOP=1 -U tester -d fixture -c 'INSERT INTO accounts VALUES (3);'
exec /usr/bin/pg_dump "$@"
'@
    Invoke-Container @('cp','/tmp/pg_dump','/tmp/bin/pg_dump') | Out-Null
    Invoke-Container @('chmod','+x','/tmp/bin/pg_dump') | Out-Null
    $script = New-SynchronizedSnapshotScript -DatabaseUser tester -DatabaseName fixture -RemoteDumpPath /tmp/fixture.dump
    Send-File 'snapshot.sh' $script
    $counts = Invoke-Container @('env','PATH=/tmp/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin','sh','/tmp/snapshot.sh')
    Assert-Equal $counts "public.accounts|2`npublic.empty_table|0`npublic.flyway_schema_history|1" 'Snapshot counts'
    Assert-Equal (Invoke-Container @('psql','-XqAt','-U','tester','-d','fixture','-c','select count(*) from accounts;')) '3' 'Concurrent live count'
    $snapshot = Invoke-Container @('cat','/tmp/exported-snapshot')
    if ($snapshot -notmatch '^[0-9A-Fa-f]+-[0-9A-Fa-f]+-[0-9]+$') { throw "Invalid exported snapshot: $snapshot" }
    $arguments = Invoke-Container @('cat','/tmp/dump-arguments')
    if ($arguments -notmatch [regex]::Escape("--snapshot=$snapshot")) { throw 'pg_dump did not receive the exported snapshot' }
    Invoke-Container @('createdb','-U','tester','restored') | Out-Null
    Invoke-Container @('pg_restore','--exit-on-error','-U','tester','-d','restored','/tmp/fixture.dump') | Out-Null
    Assert-Equal (Invoke-Container @('psql','-XqAt','-U','tester','-d','restored','-c','select count(*) from accounts;')) '2' 'Restored snapshot count'
    Invoke-Container @('/usr/bin/pg_dump',"--snapshot=$snapshot",'-U','tester','-d','fixture','-f','/tmp/expired.dump') -ExpectFailure | Out-Null
    # A SQL error after successful pg_dump must propagate psql exit 3, even
    # though the dump success flag now exists.
    Send-File 'sql-failure.sh' ($script.Replace('COMMIT;', 'SELECT 1/0;'))
    Assert-Equal (Invoke-Container @('sh','/tmp/sql-failure.sh') -ExpectFailure) 3 'SQL exit-code propagation'
    Send-File 'pg_dump' "#!/bin/sh`nexit 47`n"
    Invoke-Container @('cp','/tmp/pg_dump','/tmp/bin/pg_dump') | Out-Null
    Assert-Equal (Invoke-Container @('env','PATH=/tmp/bin:/usr/bin:/bin','sh','/tmp/snapshot.sh') -ExpectFailure) 47 'pg_dump exit-code propagation'
    Assert-Equal (Invoke-Container @('psql','-XqAt','-U','tester','-d','fixture','-c',"select count(*) from pg_stat_activity where datname='fixture' and state='idle in transaction';")) '0' 'Exporter transaction cleanup'
    Assert-Equal (Invoke-Container @('sh','-c','find /tmp -maxdepth 1 -name "platform-snapshot.*" | wc -l')) '0' 'Temporary flag cleanup'
    # Exercise the production helpers with real PostgreSQL. Only the transport
    # is adapted from kubectl exec to this disposable container's docker exec.
    $module = Get-Module PostgresBackupCommon
    & $module {
        param($name)
        $script:IntegrationContainer = $name
        function script:Invoke-PodExec {
            param($Namespace, $PodName, $Arguments)
            if ($Namespace -ne 'isolated' -or $PodName -ne $script:IntegrationContainer) { throw 'Integration transport target mismatch' }
            $ErrorActionPreference = 'Continue'
            $output = & docker exec $script:IntegrationContainer @Arguments 2>&1
            return [pscustomobject]@{ ExitCode = $LASTEXITCODE; Output = ($output | Out-String) }
        }
    } $container
    $connection = @{Namespace='isolated'; PodName=$container; DatabaseUser='tester'; DatabaseName='fixture'}
    $actualCounts = Invoke-SynchronizedDumpWithRowCounts @connection -RemoteDumpPath /tmp/production.dump -BackupId '2026-10-10_120000_abcdef'
    Assert-Equal $actualCounts['public.accounts'] 3L 'Production helper row counts'
    Assert-Equal (Use-FreshProvisioningMarker @connection) $true 'First provisioning claim'
    Assert-Equal (Use-FreshProvisioningMarker @connection) $false 'Repeated provisioning claim'
    Invoke-Container @('psql','-XqAt','-U','tester','-d','postgres','-c',"UPDATE platform_bootstrap.provisioning SET consumed_at=NULL, created_at=clock_timestamp()-interval '2 hours';") | Out-Null
    Assert-Equal (Use-FreshProvisioningMarker @connection) $false 'Expired provisioning claim'
    Invoke-Container @('psql','-XqAt','-U','tester','-d','postgres','-c','UPDATE platform_bootstrap.provisioning SET consumed_at=NULL, created_at=clock_timestamp();') | Out-Null
    Revoke-FreshProvisioningMarker @connection
    Assert-Equal (Use-FreshProvisioningMarker @connection) $false 'Restore revocation'
    Invoke-Container @('psql','-XqAt','-U','tester','-d','postgres','-c','DROP SCHEMA platform_bootstrap CASCADE;') | Out-Null
    Assert-Equal (Use-FreshProvisioningMarker @connection) $false 'Missing provisioning marker'
    Revoke-FreshProvisioningMarker @connection
    Write-Host 'PASS: real snapshot substitution, concurrent writes, dump/restore counts, snapshot lifetime, SQL and pg_dump failures, cleanup.'
}
finally {
    & docker rm -f -v $container | Out-Null
    # Delete only the exact temporary directory created by this script.
    $resolved = [IO.Path]::GetFullPath($scratch)
    $expected = [IO.Path]::GetFullPath((Join-Path ([IO.Path]::GetTempPath()) $container))
    if ($resolved -eq $expected -and (Split-Path $resolved -Leaf) -eq $container) {
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}
