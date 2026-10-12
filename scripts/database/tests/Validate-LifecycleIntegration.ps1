# Run in a dedicated Windows PowerShell process; never dot-source this harness.
param()
$ErrorActionPreference = 'Stop'
$source = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
$docker = (Get-Command docker.exe -CommandType Application).Source
$commit = (& git -C $source rev-parse HEAD | Out-String).Trim()
$name = 'stage2d-lifecycle-' + [guid]::NewGuid().ToString('N')
$root = Join-Path ([IO.Path]::GetTempPath()) $name
$reportDirectory = Join-Path $source '.tmp'
$state = @{
    Docker=$docker; Container=$name; Namespace='stage2d-isolated'; Root=$root
    Marker=(Join-Path $root '.tmp\restore-maintenance-state.json')
    Replicas=@{writer_one=2; writer_two=1}; Fault=''; Transfers=0; Restores=0
    Events=(New-Object 'Collections.Generic.List[string]')
    MarkerPhases=(New-Object 'Collections.Generic.List[string]')
}
$results = New-Object 'Collections.Generic.List[object]'
$answers = New-Object 'Collections.Generic.Queue[string]'
$savedEnvironment = @{}
foreach ($key in @('PATH','KUBECONFIG','DOCKER_HOST','DOCKER_CONTEXT')) { $savedEnvironment[$key] = [Environment]::GetEnvironmentVariable($key,'Process') }
$created = $false
$failure = $null
New-Item -ItemType Directory $root | Out-Null
New-Item -ItemType Directory (Join-Path $root 'services') | Out-Null
Import-Module (Join-Path $PSScriptRoot 'LifecycleTransport.psm1') -Force -Global
Initialize-LifecycleTransport $state

function Assert-That([bool]$Condition, [string]$Message) { if (-not $Condition) { throw "ASSERTION: $Message" } }
function Expect-Failure([scriptblock]$Action, [string]$Pattern) {
    $caught = $null
    try { & $Action | Out-Null } catch { $caught = $_ }
    if (-not $caught -or $caught.Exception.Message -notlike $Pattern) { throw "Expected [$Pattern], got [$caught]" }
}
function Case([string]$CaseName, [scriptblock]$Action) {
    $watch = [Diagnostics.Stopwatch]::StartNew()
    try { & $Action; $results.Add([pscustomobject]@{Name=$CaseName; Status='PASS'; Seconds=[math]::Round($watch.Elapsed.TotalSeconds,2)}); Write-Host "CASE PASS: $CaseName" -ForegroundColor Green }
    catch { $results.Add([pscustomobject]@{Name=$CaseName; Status='FAIL'; Error=$_.Exception.Message}); throw }
}
function Docker-Checked([string[]]$Arguments) {
    $r = Invoke-LifecycleDocker $Arguments
    if ($r.ExitCode -ne 0) { throw "Disposable Docker operation failed: $($r.Error)" }
    return $r.Output.Trim()
}
function Clean-Remote {
    $r = Invoke-LifecycleExec @('sh','-c','find /tmp -maxdepth 1 -name "platform-*" -print')
    Assert-That ($r.ExitCode -eq 0 -and -not $r.Output.Trim()) 'remote dump/status files must be cleaned'
    Assert-That (@(Get-ChildItem (Join-Path $root 'backups\postgres') -Directory -Filter '.tmp-*').Count -eq 0) 'unpublished backup directories must be cleaned'
}
function Restore-OriginalReplicas {
    # Recovery is deliberate and explicit, after the test has checked DB state.
    $state.Fault = ''
    Assert-That (Resume-ScaledServices -Namespace $state.Namespace -OriginalReplicas @{writer_one=2; writer_two=1}) 'explicit recovery resumes all writers'
    Remove-MaintenanceState $state.Marker
}
function Clone-Backup([string]$SourceDirectory) {
    $id = New-BackupId
    $directory = Join-Path (Get-BackupsRoot $root) $id
    Copy-Item -LiteralPath $SourceDirectory -Destination $directory -Recurse
    $manifest = Read-BackupManifest $directory
    $manifest.backupId = $id
    $manifest | ConvertTo-Json -Depth 12 | Set-Content (Join-Path $directory 'manifest.json') -Encoding UTF8
    return $directory
}
function Save-Manifest($Manifest, [string]$Directory) {
    $Manifest | ConvertTo-Json -Depth 12 | Set-Content (Join-Path $Directory 'manifest.json') -Encoding UTF8
}
function Restore-Fixture([string]$Id, [string]$Target='fixture', [switch]$Safety) {
    $answers.Enqueue('RESTORE')
    $options = @{ProjectRoot=$root; Namespace=$state.Namespace; ClusterName=$name; DatabaseConfig=@{Name='fixture';Username='tester'}; Backup=$Id; DatabaseWriterServices=@('writer_one','writer_two'); TargetDatabase=$Target; AuthorizeDestructiveRestore=$true}
    if ($Safety) { $options.ConfirmSafetyBackup=$true } else { $options.SkipSafetyBackup=$true }
    Invoke-PlatformRestore @options
}

try {
    # Empty PATH and kubeconfig also block accidental fallback to native tools.
    # The adapter never invokes kubectl; Docker always uses an explicit local pipe.
    $env:PATH = ''
    $env:KUBECONFIG = Join-Path $root 'empty-kubeconfig'
    Set-Content $env:KUBECONFIG 'apiVersion: v1'
    $env:DOCKER_HOST = $null; $env:DOCKER_CONTEXT = $null
    function global:kubectl { Invoke-IsolatedKubectl -Arguments $args }
    function global:git {
        if (($args -join ' ') -ne 'rev-parse HEAD') { throw 'ISOLATION: git command denied' }
        $global:LASTEXITCODE=0; return $script:commit
    }
    function global:Read-Host {
        param([string]$Prompt)
        if ($script:answers.Count -eq 0) { throw "Unexpected interactive prompt: $Prompt" }
        $script:state.Events.Add('PROMPT ' + $Prompt)
        return $script:answers.Dequeue()
    }
    function Write-Step([string]$Message) { Write-Host "STEP: $Message" }
    function global:Invoke-RestMethod { throw 'ISOLATION: HTTP/Radar request forbidden' }
    function global:Invoke-WebRequest { throw 'ISOLATION: HTTP request forbidden' }

    Import-Module (Join-Path $PSScriptRoot '..\PostgresBackupCommon.psm1') -Force
    Import-Module (Join-Path $PSScriptRoot '..\RadarBootstrapEligibility.psm1') -Force
    . (Join-Path $PSScriptRoot '..\backup.ps1')
    . (Join-Path $PSScriptRoot '..\verify-backup.ps1')
    . (Join-Path $PSScriptRoot '..\restore.ps1')

    Docker-Checked @('create','--name',$name,'--label',"stage2d.owner=$name",'--network','none','--tmpfs','/var/lib/postgresql/data',
        '-e','POSTGRES_HOST_AUTH_METHOD=trust','-e','POSTGRES_USER=tester','-e','POSTGRES_DB=fixture','postgres:16') | Out-Null
    $created=$true
    $init = Join-Path $source 'infrastructure\helm\ai-investment-platform\files\fresh-provisioning.sql'
    Docker-Checked @('cp',$init,"${name}:/docker-entrypoint-initdb.d/10-fresh-provisioning.sql") | Out-Null
    Docker-Checked @('start',$name) | Out-Null
    $ready=$false
    for ($i=0; $i -lt 60; $i++) {
        $r=Invoke-LifecycleExec @('pg_isready','-h','127.0.0.1','-U','tester','-d','fixture')
        if ($r.ExitCode -eq 0) { $ready=$true; break }
        Start-Sleep -Seconds 1
    }
    Assert-That $ready 'disposable PostgreSQL readiness'
    Case 'Infrastructure and transport isolation' {
        $inspect = @(Docker-Checked @('inspect',$name) | ConvertFrom-Json)[0]
        Assert-That ($inspect.HostConfig.NetworkMode -eq 'none') 'network disabled'
        Assert-That (@($inspect.Mounts | Where-Object Type -in @('volume','bind')).Count -eq 0) 'no persistent volumes or host mounts'
        Assert-That ($inspect.Config.Labels.'stage2d.owner' -eq $name) 'ownership label'
        Assert-That (@($inspect.HostConfig.PortBindings.PSObject.Properties).Count -eq 0) 'no published ports'
        Assert-That (-not (Get-Command kubectl.exe -ErrorAction SilentlyContinue)) 'native kubectl unavailable'
        Expect-Failure { Invoke-IsolatedKubectl @('get','pods','-n','ai-investment') } '*ISOLATION*'
        Expect-Failure { Invoke-IsolatedKubectl @('exec','existing-postgres','-n','stage2d-isolated','--','psql') } '*ISOLATION*'
        Expect-Failure { Invoke-IsolatedKubectl @('apply','-n','stage2d-isolated','-f','job.json') } '*ISOLATION*'
    }
    Invoke-LifecycleSql @'
CREATE SCHEMA auth; CREATE SCHEMA research;
CREATE TABLE auth.flyway_schema_history(installed_rank int, version text, success boolean);
INSERT INTO auth.flyway_schema_history VALUES (1,'1',true);
CREATE TABLE research.flyway_schema_history(installed_rank int, version text, success boolean);
INSERT INTO research.flyway_schema_history VALUES (1,'2',true);
CREATE TABLE auth.app_users(id int PRIMARY KEY);
CREATE TABLE research.global_opportunity_cycle_run(id int PRIMARY KEY);
CREATE TABLE public.payload(id int PRIMARY KEY, raw bytea NOT NULL, note text NOT NULL);
INSERT INTO public.payload SELECT i, decode(repeat('000aff80feff',1024),'hex'), 'Unicode: ' || chr(8364) || chr(28450) FROM generate_series(1,5) i;
CREATE TABLE public.empty_table(id int);
CREATE TABLE public.writer_probe(writer text DEFAULT current_setting('application_name'));
'@ | Out-Null
    Set-LifecycleWriterSessions writer_one 2
    Set-LifecycleWriterSessions writer_two 1
    $config=@{Name='fixture';Username='tester'}
    $backupArgs=@{ProjectRoot=$root; Namespace=$state.Namespace; ClusterName=$name; DatabaseConfig=$config}
    $verifyArgs=@{ProjectRoot=$root; Namespace=$state.Namespace}
    $restoreArgs=$backupArgs.Clone(); $restoreArgs.DatabaseWriterServices=@('writer_one','writer_two')
    $connection=@{Namespace=$state.Namespace;PodName=$name;DatabaseUser='tester';DatabaseName='fixture'}
    $fingerprint=Invoke-LifecycleSql "select md5(string_agg(id::text || encode(raw,'hex') || note,',' order by id)) from payload;"
    $script:original=$null; $script:backupId=$null
    Case 'Production backup: binary transfer, manifest, hashes, Flyway and counts' {
        $script:original=Invoke-PlatformBackup @backupArgs
        $script:backupId=Split-Path $original -Leaf
        $m=Read-BackupManifest $original
        Assert-That ($m.applicationGitCommit -eq $commit) 'source commit'
        Assert-That ($m.databases[0].tableRowCounts.'public.payload' -eq 5) 'synchronized payload count'
        Assert-That ($m.databases[0].tableRowCounts.'public.empty_table' -eq 0) 'empty-table count'
        Assert-That ($m.databases[0].tableRowCounts.'public.writer_probe' -eq 0) 'uncommitted writer rows excluded'
        Assert-That ($m.flywaySchemaVersions.auth -eq '1' -and $m.flywaySchemaVersions.research -eq '2') 'Flyway versions'
        Assert-That ($m.databases[0].sha256 -eq (Get-FileSha256 (Join-Path $original 'database.dump'))) 'manifest SHA-256'
        Assert-That ((Get-Content (Join-Path $original 'checksums.sha256')).Trim() -eq "$($m.databases[0].sha256)  database.dump") 'checksum sidecar'
        $bytes=[IO.File]::ReadAllBytes((Join-Path $original 'database.dump'))
        Assert-That ([Text.Encoding]::ASCII.GetString($bytes,0,5) -eq 'PGDMP') 'custom binary archive header'
        Assert-That ($state.Transfers -eq 2) 'both binary transfer directions hash-checked'
        Clean-Remote
    }
    Case 'Production archive verification' { Assert-That (Invoke-PlatformBackupVerify @verifyArgs -Backup $backupId) 'valid backup verifies'; Clean-Remote }
    Case 'Corrupted binary rejected before mutation' {
        $bad=Clone-Backup $original; $file=Join-Path $bad 'database.dump'; $bytes=[IO.File]::ReadAllBytes($file); $bytes[10]=$bytes[10] -bxor 255; [IO.File]::WriteAllBytes($file,$bytes)
        Assert-That (-not (Invoke-PlatformBackupVerify @verifyArgs -Backup (Split-Path $bad -Leaf))) 'corrupt hash rejected'
        Expect-Failure { Invoke-PlatformRestore @restoreArgs -Backup (Split-Path $bad -Leaf) } '*did not pass archive-integrity*'
        Assert-That ($state.Restores -eq 0) 'no restore attempted'
    }
    Case 'Corrupt archive rejected even with recomputed checksum' {
        $bad=Clone-Backup $original; $file=Join-Path $bad 'database.dump'; [IO.File]::WriteAllBytes($file,[byte[]](1,2,3,4,5))
        $m=Read-BackupManifest $bad; $m.databases[0].sizeBytes=5; $m.databases[0].sha256=Get-FileSha256 $file; Save-Manifest $m $bad
        "$($m.databases[0].sha256)  database.dump" | Set-Content (Join-Path $bad 'checksums.sha256')
        Assert-That (-not (Invoke-PlatformBackupVerify @verifyArgs -Backup (Split-Path $bad -Leaf))) 'pg_restore list rejects invalid archive'
        Clean-Remote
    }
    Case 'Incomplete manifests and inconsistent checksum sidecar rejected' {
        foreach ($field in @('sha256','tableRowCounts')) {
            $bad=Clone-Backup $original; $m=Read-BackupManifest $bad; $m.databases[0].PSObject.Properties.Remove($field); Save-Manifest $m $bad
            Assert-That (-not (Invoke-PlatformBackupVerify @verifyArgs -Backup (Split-Path $bad -Leaf))) "missing $field rejected"
        }
        $bad=Clone-Backup $original; Set-Content (Join-Path $bad 'checksums.sha256') 'wrong checksum'
        Assert-That (-not (Invoke-PlatformBackupVerify @verifyArgs -Backup (Split-Path $bad -Leaf))) 'sidecar mismatch rejected'
    }
    Case 'Authorization, confirmation and mutually exclusive safety choices' {
        Expect-Failure { Invoke-PlatformRestore @restoreArgs -Backup $backupId } '*Refusing to overwrite without*'
        $answers.Enqueue('NO'); Invoke-PlatformRestore @restoreArgs -Backup $backupId -AuthorizeDestructiveRestore
        Expect-Failure { Invoke-PlatformRestore @restoreArgs -Backup $backupId -SkipSafetyBackup -ConfirmSafetyBackup } '*Specify only one*'
        Expect-Failure { Invoke-PlatformRestore @restoreArgs -Backup $backupId -TargetDatabase postgres -AuthorizeDestructiveRestore } '*reserved database*'
        Assert-That ($state.Restores -eq 0 -and -not (Test-Path $state.Marker)) 'decline leaves database and writers unchanged'
        $answers.Enqueue('invalid'); $answers.Enqueue('invalid'); $answers.Enqueue('invalid')
        Expect-Failure { Read-ExplicitSafetyBackupApproval fixture } '*No clear answer*'
    }
    Case 'Successful configured-target restore with writer shutdown and real safety backup' {
        Invoke-LifecycleSql "INSERT INTO payload VALUES (99,decode('abcdef','hex'),'before restore');" | Out-Null
        $before=@(Get-ChildItem (Get-BackupsRoot $root) -Directory).Name
        Restore-Fixture $backupId -Safety
        $safety=@(Get-ChildItem (Get-BackupsRoot $root) -Directory | Where-Object Name -notin $before)
        Assert-That ($safety.Count -eq 1) 'one pre-restore backup published'
        Assert-That ((Read-BackupManifest $safety[0].FullName).databases[0].tableRowCounts.'public.payload' -eq 6) 'safety backup captures pre-restore data'
        $script:safetyId=$safety[0].Name
        Assert-That ($state.MarkerPhases[-1] -eq 'SAFETY_BACKUP_TAKEN') 'maintenance state precedes destructive restore'
        Assert-That ($state.Replicas.writer_one -eq 2 -and $state.Replicas.writer_two -eq 1 -and -not (Test-Path $state.Marker)) 'success resumes exact replicas and clears marker'
        Assert-That ((Invoke-LifecycleSql "select md5(string_agg(id::text || encode(raw,'hex') || note,',' order by id)) from payload;") -eq $fingerprint) 'binary and Unicode contents round-trip'
        Clean-Remote
    }
    Case 'Safety backup restores into a missing disposable target' {
        Restore-Fixture $safetyId 'safety_check'
        Assert-That ((Invoke-LifecycleSql 'select count(*) from payload;' safety_check) -eq '6') 'safety archive contains overwritten row'
        Assert-That (-not (Test-Path $state.Marker)) 'non-configured target never enters writer maintenance'
        Clean-Remote
    }
    foreach ($fault in @('replica-query','scale-down','wait','remaining','backup')) {
        Case "Fail closed before destruction: $fault" {
            $state.Fault=$fault; $before=$state.Restores; $answers.Enqueue('RESTORE')
            $pattern=switch ($fault) { 'replica-query' {'*read writer replicas*'} 'scale-down' {'*Failed to scale*'} 'wait' {'*Timed out waiting*'} 'remaining' {'*still has running*'} 'backup' {'*Synchronized backup capture*failed*'} }
            Expect-Failure { Invoke-PlatformRestore @restoreArgs -Backup $backupId -AuthorizeDestructiveRestore -ConfirmSafetyBackup } $pattern
            Assert-That ($state.Restores -eq $before) 'no destructive restore before writer/safety checks pass'
            if (Test-Path $state.Marker) { Restore-OriginalReplicas } else { $state.Fault='' }
            Clean-Remote
        }
    }
    foreach ($fault in @('copy-in','restore','missing-table','flyway')) {
        Case "Failed restore retains maintenance and supports deliberate recovery: $fault" {
            $state.Fault=$fault
            $pattern=switch ($fault) { 'copy-in' {'*kubectl cp failed*'} 'restore' {'*pg_restore reported a failure*'} 'missing-table' {'*Full-restore verification failed*'} 'flyway' {'*Flyway version mismatch*'} }
            Expect-Failure { Restore-Fixture $backupId } $pattern
            Assert-That (Test-Path $state.Marker) 'failure retains maintenance marker'
            Assert-That ($state.Replicas.writer_one -eq 0 -and $state.Replicas.writer_two -eq 0) 'failure never resumes writers'
            $saved=Get-Content $state.Marker -Raw | ConvertFrom-Json
            Assert-That ($saved.originalReplicas.writer_one -eq 2 -and $saved.originalReplicas.writer_two -eq 1) 'recovery replicas recorded'
            $recordBefore=Get-Content $state.Marker -Raw
            Expect-Failure { Invoke-PlatformRestore @restoreArgs -Backup $backupId -AuthorizeDestructiveRestore } '*Existing maintenance state*'
            Assert-That ((Get-Content $state.Marker -Raw) -eq $recordBefore) 'retry cannot overwrite recovery state'
            Clean-Remote
            $state.Fault=''
            # Repair while stopped using real pg_restore, then use production
            # recovery helpers with the saved (not overwritten) replica counts.
            Restore-Fixture $backupId 'recovery_check'
            Assert-That ((Invoke-LifecycleSql 'select count(*) from payload;' recovery_check) -eq '5') 'archive still restores independently'
            $r=Invoke-LifecycleDocker @('cp',(Join-Path $original 'database.dump'),"${name}:/tmp/recovery.dump")
            Assert-That ($r.ExitCode -eq 0) 'recovery copy'
            $r=Invoke-LifecycleExec @('pg_restore','--clean','--if-exists','--no-owner','--no-privileges','-U','tester','-d','fixture','/tmp/recovery.dump')
            Assert-That ($r.ExitCode -eq 0) 'manual repair succeeds'
            Invoke-LifecycleExec @('rm','-f','/tmp/recovery.dump') | Out-Null
            Restore-OriginalReplicas
        }
    }
    Case 'Resume failure preserves recovery record after successful data restore' {
        $state.Fault='resume'; Restore-Fixture $backupId
        Assert-That (Test-Path $state.Marker) 'resume failure retains maintenance marker'
        Assert-That ((Invoke-LifecycleSql 'select count(*) from payload;') -eq '5') 'data restore succeeded'
        Restore-OriginalReplicas; Clean-Remote
    }
    Case 'Restored data cannot grant fresh-install eligibility or automatic ADMIN/Radar' {
        Assert-That (Test-DatabaseRestoreRecorded @connection) 'durable negative restore record'
        Assert-That (-not (Use-FreshProvisioningMarker @connection)) 'revoked marker cannot be consumed'
        Invoke-LifecycleSql 'DROP SCHEMA platform_bootstrap CASCADE;' postgres | Out-Null
        Restore-Fixture $backupId
        Assert-That ((Invoke-LifecycleSql "select to_regclass('platform_bootstrap.provisioning') is null;" postgres) -eq 't') 'restore never recreates fresh marker'
        Assert-That (-not (Use-FreshProvisioningMarker @connection)) 'missing marker cannot be consumed'
        # Load only the two real functions from platform.ps1, never its dispatch.
        $tokens=$null; $errors=$null
        $ast=[Management.Automation.Language.Parser]::ParseFile((Join-Path $source 'platform.ps1'),[ref]$tokens,[ref]$errors)
        foreach ($functionName in @('Invoke-FirstAdminBootstrapIfNeeded','Invoke-InitialOpportunityCycleAfterCleanDeploy')) {
            $node=$ast.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $functionName},$true)
            . ([scriptblock]::Create($node.Extent.Text))
        }
        $Namespace=$state.Namespace; $DatabaseConfig=$config; $OpportunityBootstrapMarkerFile=Join-Path $root '.tmp\never-created-radar-marker'
        function Resolve-ResearchDatabaseSchemaName { return 'research' }
        $eventStart=$state.Events.Count
        Invoke-FirstAdminBootstrapIfNeeded
        $radarMessages = @(Invoke-InitialOpportunityCycleAfterCleanDeploy 6>&1 | ForEach-Object { "$_" })
        Write-Host ($radarMessages -join "`n")
        Assert-That (($radarMessages -join ' ') -match 'SKIP_NO_FRESH_PROVISIONING_PROOF') 'real Radar gate denied by marker, with successful schema/count queries'
        Assert-That (-not $script:FirstAdminCreatedThisStartup) 'no automatic admin created'
        Assert-That ((Invoke-LifecycleSql 'select count(*) from auth.app_users;') -eq '0') 'restored empty users remain empty'
        Assert-That ((Invoke-LifecycleSql 'select count(*) from research.global_opportunity_cycle_run;') -eq '0') 'no Radar cycle created'
        Assert-That (-not (Test-Path $OpportunityBootstrapMarkerFile)) 'no local Radar success marker fabricated'
        Assert-That (@($state.Events | Select-Object -Skip $eventStart | Where-Object { $_ -match '^(apply|create|scale) ' }).Count -eq 0) 'bootstrap paths do not create Jobs or workloads'
        Clean-Remote
    }
    Assert-That ($answers.Count -eq 0) 'all explicit prompt answers consumed'
}
catch { $failure=$_; Write-Host $_ -ForegroundColor Red }
finally {
    if ($created) {
        # Verify ownership before removing exactly this harness's container.
        $inspect=Invoke-LifecycleDocker @('inspect',$name)
        if ($inspect.ExitCode -eq 0 -and (@($inspect.Output | ConvertFrom-Json)[0].Config.Labels.'stage2d.owner' -eq $name)) {
            $removed=Invoke-LifecycleDocker @('rm','-f','-v',$name)
            if ($removed.ExitCode -ne 0 -and -not $failure) { $failure='Disposable container cleanup failed' }
        } elseif (-not $failure) { $failure='Unable to establish ownership for cleanup' }
    }
    foreach ($key in $savedEnvironment.Keys) { [Environment]::SetEnvironmentVariable($key,$savedEnvironment[$key],'Process') }
    New-Item -ItemType Directory $reportDirectory -Force | Out-Null
    [ordered]@{Commit=$commit; Results=@($results.ToArray()); Transfers=$state.Transfers; RestoreAttempts=$state.Restores; Events=@($state.Events.ToArray()); Failure=[string]$failure; ContainerRemoved=($created -and $removed.ExitCode -eq 0)} |
        ConvertTo-Json -Depth 10 | Set-Content (Join-Path $reportDirectory 'stage2d-lifecycle-results.json') -Encoding UTF8
    $resolved=[IO.Path]::GetFullPath($root)
    if ($resolved -eq [IO.Path]::GetFullPath((Join-Path ([IO.Path]::GetTempPath()) $name)) -and (Split-Path $resolved -Leaf) -eq $name) { Remove-Item -LiteralPath $resolved -Recurse -Force }
}
if ($failure) { throw $failure }
Write-Host "PASS: $($results.Count) lifecycle cases; $($state.Transfers) hash-checked binary transfers; disposable container removed."
