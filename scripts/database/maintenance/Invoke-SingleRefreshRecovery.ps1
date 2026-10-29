[CmdletBinding()]
param([switch]$ExecuteReconciliation, [switch]$ContinueFailedPreflight)

# Exact single-refresh wrapper. The reviewed Markdown supplies the three execution
# blocks; the original SQL, certificate, templates and evidence are never rewritten.
$ErrorActionPreference = 'Stop'

function Assert-RecoveryHash {
    param([string]$Path, [string]$Expected)
    if ($Expected -cnotmatch '^[a-f0-9]{64}$' -or
        (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() -cne $Expected) {
        throw "Approved hash mismatch: $Path"
    }
}

function Read-RecoveryPackage {
    param([string]$Root)
    $evidence = Join-Path $Root 'diagnostics/maintenance-20261018-022509'
    $hashes = @{
        'termination-evidence.json' = 'a764f89eab4af72578719fa3285a9d7fdf4563647d28aa1be82fa48ab77f0b59'
        'phase-a-certificate-approval.prepared.json' = '362f818eb3529b7f56d431fa811dc9e43bf99631b1a59aa100852872dd327067'
        'phase-c-approval.prepared.json' = 'bffcc67ffaf86eecc01e3854522188c5a86a52cd4d5c788f377e49d0fb8360f1'
        'POSTGRES_POD_HANDOFF.prepared.md' = 'e95e6eeb7f9a7ead4db4a2f9889b087f0a96bd2fbd2c983ba57c6b0844726f48'
        'reviewed-preimages.csv' = '93afa309641a57584e45158059648d40d72b631cbbe5a92bd258ec3ee850d31b'
        'reviewed-preimages.hex' = '362f0389fae0ca0aaa92f59875fb59dddded07917fc9f72a27b5129d30c7375c'
        'capture_refresh_preimages.sql' = 'bb65dbf6e2c42bfcb8bea820e1347823ea406f1cf03ee87f2b8e97b7f337de0f'
        'reconcile_single_refresh.sql' = '81403fb5bb0f760075833cf2d36fdd42b10d6978833f62164e8dd39e0ec700a7'
    }
    foreach ($name in $hashes.Keys) { Assert-RecoveryHash (Join-Path $evidence $name) $hashes[$name] }
    $certificate = Get-Content -LiteralPath (Join-Path $evidence 'termination-evidence.json') -Raw | ConvertFrom-Json
    foreach ($binding in $certificate.reused_source_evidence_sha256.PSObject.Properties) {
        if ($binding.Name -cnotmatch '^[A-Za-z0-9_.-]+$') { throw 'Unexpected evidence filename' }
        Assert-RecoveryHash (Join-Path $evidence $binding.Name) $binding.Value
    }
    $checksPath = Join-Path $evidence 'phase-a-final-checks-20261018-014344.json'
    Assert-RecoveryHash $checksPath $certificate.final_checks_artifact.sha256
    $checks = Get-Content -LiteralPath $checksPath -Raw | ConvertFrom-Json
    $a = Get-Content -LiteralPath (Join-Path $evidence 'phase-a-certificate-approval.prepared.json') -Raw | ConvertFrom-Json
    $c = Get-Content -LiteralPath (Join-Path $evidence 'phase-c-approval.prepared.json') -Raw | ConvertFrom-Json
    if ($certificate.termination_verified -ne $true -or $certificate.refresh_id -cne $c.refresh_id -or
        $a.certificate_sha256 -cne $hashes['termination-evidence.json']) { throw 'Certificate/template binding mismatch' }
    $backup = Join-Path $Root "backups/postgres/$($c.backup_id)"
    Assert-RecoveryHash (Join-Path $backup 'database.dump') $c.backup_dump_sha256
    Assert-RecoveryHash (Join-Path $backup 'manifest.json') $c.backup_manifest_sha256
    $text = Get-Content -LiteralPath (Join-Path $evidence 'POSTGRES_POD_HANDOFF.prepared.md') -Raw
    # A bare -- is consumed by PowerShell when K is called. Quote this literal
    # argument in memory; the reviewed handoff file and its hash stay unchanged.
    $blocks = @([regex]::Matches($text, '(?ms)^```powershell\r?\n(.*?)^```') | ForEach-Object {
        [scriptblock]::Create($_.Groups[1].Value.Replace(' -- ', " '--' "))
    })
    if ($blocks.Count -ne 3) { throw 'Reviewed handoff block layout changed' }
    [pscustomobject]@{Root=$Root; Evidence=$evidence; Hashes=$hashes; Certificate=$certificate; Checks=$checks; A=$a; C=$c; Blocks=$blocks}
}

function Read-RecoveryConsent {
    param($Package)
    Write-Host "Refresh: $($Package.C.refresh_id); certificate SHA-256: $($Package.Hashes['termination-evidence.json'])"
    $name = (Read-Host 'Your real full name, explicitly acting as operator and reviewer').Trim()
    if ($name.Length -lt 2 -or $name.Length -gt 160 -or $name -match '[<>\x00-\x1f]' -or
        $name -match '^(operator|reviewer|admin|test|example|your name)$') { throw 'A real named operator/reviewer is required' }
    Write-Host 'Phase A: approve the exact certificate and closed local executor inventory; retain the maintenance freeze and isolation until separately authorized controlled recovery.'
    if ((Read-Host 'Type APPROVE_PHASE_A_TERMINATION_CERTIFICATE') -cne 'APPROVE_PHASE_A_TERMINATION_CERTIFICATE') { throw 'Phase A approval missing' }
    $aTime = [DateTime]::UtcNow.ToString('o')
    $endpoint = $Package.C.execution_connection
    Write-Host "Phase C: FAILED/ADMIN_WORKER_TERMINATION_VERIFIED for only $($Package.C.refresh_id); only status/completed_at/updated_at/safe_error_code/safe_error_message."
    Write-Host "Connection: $($endpoint.kube_context)/$($endpoint.namespace), $($endpoint.pod), UID $($endpoint.pod_uid), $($endpoint.container), $($endpoint.container_id), socket $($endpoint.socket_directory), role/database investment."
    Write-Host "Authorize the existing protected SQL and private temporary file staging at $($endpoint.remote_staging_directory); retain already reviewed backup/preimage/SQL bindings."
    if ((Read-Host 'Type APPROVED_SINGLE_REFRESH_TERMINATION_AND_RECONCILIATION') -cne 'APPROVED_SINGLE_REFRESH_TERMINATION_AND_RECONCILIATION') { throw 'Phase C authorization missing' }
    $cTime = [DateTime]::UtcNow.ToString('o')
    if ((Read-Host 'Confirm the local-only maintenance freeze remains unbroken: type CONFIRM_UNBROKEN_MAINTENANCE_FREEZE') -cne 'CONFIRM_UNBROKEN_MAINTENANCE_FREEZE') { throw 'Maintenance freeze not confirmed' }
    [pscustomobject]@{Name=$name; PhaseAApprovedAtUtc=$aTime; PhaseCApprovedAtUtc=$cTime; FreezeConfirmedAtUtc=[DateTime]::UtcNow.ToString('o')}
}

function Write-RecoveryRecord {
    param([string]$Path, $Value)
    $bytes = [Text.UTF8Encoding]::new($false).GetBytes(($Value | ConvertTo-Json -Depth 24) + [char]10)
    $stream = [IO.File]::Open($Path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try { $stream.Write($bytes, 0, $bytes.Length); $stream.Flush($true) } finally { $stream.Dispose() }
    [IO.File]::SetAttributes($Path, ([IO.File]::GetAttributes($Path) -bor [IO.FileAttributes]::ReadOnly))
    (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Invoke-RecoveryNative {
    param([string]$Command, [string[]]$Arguments)
    $result = & $Command @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Command failed; STOP, no automatic retry" }
    $result
}

function Read-RecoveryDatabase {
    param([string]$Select)
    $sql = 'BEGIN READ ONLY; SET LOCAL statement_timeout = ''10s''; SET LOCAL TIME ZONE ''UTC''; ' + $Select + '; ROLLBACK;'
    $text = Invoke-DeploymentKubectl @('exec','postgres-0','-c','postgres','--','psql','-X','-w','-qAt','-h','/var/run/postgresql','-U','investment','-d','investment','-v','ON_ERROR_STOP=1','-c',$sql)
    ($text -join [Environment]::NewLine) | ConvertFrom-Json
}

function Assert-RecoveryRuntime {
    param($Package)
    # Reuse the existing deployment safeguards, without a pod startup or probe.
    $inventory = Get-DeploymentInventory
    if (@($inventory.items | Where-Object { $_.metadata.name -match 'research-engine' -or
        $_.metadata.labels.'app.kubernetes.io/component' -eq 'research-engine' -or
        @(@($_.spec.containers)+@($_.spec.initContainers) | Where-Object image -match 'research-engine').Count }).Count) { throw 'Research Engine executor present' }
    $dep = Invoke-DeploymentKubectl @('get','deployment','research-engine','-o','json') | ConvertFrom-Json
    $approved = $Package.Certificate.recreation_prevention
    if ($dep.metadata.uid -cne $approved.deployment.uid -or $dep.metadata.resourceVersion -cne $approved.deployment.resourceVersion -or
        $dep.spec.replicas -ne 0 -or $dep.status.replicas -gt 0 -or $dep.status.readyReplicas -gt 0 -or
        $dep.status.observedGeneration -ne $dep.metadata.generation) { throw 'Research Engine deployment drift' }
    $sets = Invoke-DeploymentKubectl @('get','replicasets','-o','json') | ConvertFrom-Json
    $engineSets = @($sets.items | Where-Object { @($_.metadata.ownerReferences | Where-Object uid -eq $dep.metadata.uid).Count })
    if ($engineSets.Count -ne $approved.replica_sets.Count) { throw 'Research ReplicaSet inventory drift' }
    foreach ($set in $engineSets) {
        if ($set.metadata.uid -cnotin @($approved.replica_sets.uid) -or $set.spec.replicas -ne 0 -or $set.status.replicas -gt 0) { throw 'Research ReplicaSet drift' }
    }
    $hpa = Invoke-DeploymentKubectl @('get','hpa','--all-namespaces','-o','json') | ConvertFrom-Json
    if (@($hpa.items).Count) { throw 'Unreviewed autoscaling controller' }
    Assert-DeploymentIsolation @{Selector=$dep.spec.selector}
    $policy = Invoke-DeploymentKubectl @('get','networkpolicy','research-bootstrap-isolation','-o','json') | ConvertFrom-Json
    if ($policy.metadata.uid -cne $approved.isolation.uid -or $policy.metadata.resourceVersion -cne $approved.isolation.resourceVersion) { throw 'Isolation identity drift' }
    $pod = Invoke-DeploymentKubectl @('get','pod','postgres-0','-o','json') | ConvertFrom-Json
    $postgres = @($pod.status.containerStatuses | Where-Object name -eq 'postgres')
    if ($pod.metadata.uid -cne $Package.C.execution_connection.pod_uid -or $pod.metadata.deletionTimestamp -or
        $postgres.Count -ne 1 -or $postgres[0].containerID -cne $Package.C.execution_connection.container_id -or
        -not $postgres[0].ready -or -not $postgres[0].state.running) { throw 'Postgres pod/container identity drift' }
    $processes = @(Get-CimInstance Win32_Process)
    if (@($processes | Where-Object { $_.Name -match '^(python(w)?([0-9]+(\.[0-9]+)*)?|pypy[0-9]*|uvicorn|gunicorn|hypercorn)(\.exe)?$' -or
        ($_.Name -in @('kubectl.exe','oc.exe') -and (-not $_.CommandLine -or $_.CommandLine -match '\b(port-forward|proxy)\b')) }).Count) { throw 'Windows executor/tunnel present or unknown' }
    $wsl = @(Invoke-RecoveryNative wsl.exe @('--list','--running','--quiet') | ForEach-Object { ($_ -replace '\x00','').Trim() } | Where-Object { $_ })
    if ($wsl.Count -ne 1 -or $wsl[0] -cne 'docker-desktop') { throw 'WSL execution scope drift' }
    $docker = @(Invoke-RecoveryNative docker @('ps','-a','--format','{{json .}}') | ForEach-Object { $_ | ConvertFrom-Json })
    $previous = $Package.Checks.windows_and_docker.dockerInventory
    if ($docker.Count -ne $previous.Count) { throw 'Docker executor inventory drift' }
    foreach ($container in $docker) {
        $old = @($previous | Where-Object ID -eq $container.ID)
        if ($old.Count -ne 1 -or $container.Image -cne $old[0].Image -or $container.State -cne $old[0].State) { throw 'Docker execution state drift' }
    }
    foreach ($old in $Package.Checks.windows_and_docker.engineContainers) {
        $current = @(Invoke-RecoveryNative docker @('inspect',$old.id) | ConvertFrom-Json)[0]
        if ($current.State.Running -or $current.State.Pid -ne 0 -or $current.State.Status -cne 'created' -or $current.HostConfig.RestartPolicy.Name -cne 'no') { throw 'Standalone Research Engine execution/recreation possible' }
    }
    $liveContainerIds = @()
    foreach ($node in $Package.Checks.node_runtimes) {
        $cri = Invoke-RecoveryNative docker @('exec',$node.node,'crictl','ps','-a','-o','json') | ConvertFrom-Json
        $sandboxes = Invoke-RecoveryNative docker @('exec',$node.node,'crictl','pods','-o','json') | ConvertFrom-Json
        $known = @($cri.containers.id)+@($sandboxes.items.id)
        $liveContainerIds += @($cri.containers | Where-Object state -eq 'CONTAINER_RUNNING' | ForEach-Object id)
        if (@($cri.containers | Where-Object { $_.metadata.name -match 'research-engine' -or $_.image.image -match 'research-engine' }).Count) { throw 'Node Research Engine container present' }
        $tasks = @(Invoke-RecoveryNative docker @('exec',$node.node,'ctr','-n','k8s.io','tasks','list') | Select-Object -Skip 1 | Where-Object { $_.Trim() })
        foreach ($task in $tasks) { if (($task.Trim() -split '\s+')[0] -cnotin $known) { throw 'Unattributed containerd task' } }
    }
    # Inspect all Linux interpreter executables, including renamed worker processes.
    $scan = @'
set -eu
for entry in /proc/[0-9]*; do
  test -r "$entry/comm" || continue
  comm=$(cat "$entry/comm" 2>/dev/null) || continue
  exe=$(readlink "$entry/exe" 2>/dev/null) || exe=UNKNOWN
  case "$comm:$exe" in
    python*:*|pypy*:*|uvicorn*:*|gunicorn*:*|hypercorn*:*|*:/usr/bin/python*|*:/usr/local/bin/python*|*:/usr/bin/pypy*|*:/usr/local/bin/pypy*)
      printf 'WORKER|%s|' "$exe"; tr '\n' ' ' < "$entry/cgroup"; printf '\n' ;;
    kubectl:*|oc:*)
      args=$(tr '\000' ' ' < "$entry/cmdline")
      case " $args " in *" port-forward "*|*" proxy "*) printf 'TUNNEL\n' ;; esac ;;
  esac
done
'@
    $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($scan))
    $linux = @(Invoke-RecoveryNative wsl.exe @('-d','docker-desktop','-u','root','--','sh','-c',"printf '%s' '$encoded' | base64 -d | sh"))
    foreach ($line in $linux) {
        if ($line -cnotmatch '^WORKER\|[^|]+\|.*\/([a-f0-9]{64})\s*$' -or
            $Matches[1] -cnotin @($Package.Checks.linux_process_attribution.container_id) -or $Matches[1] -cnotin $liveContainerIds) { throw 'Unattributed Linux executor or API tunnel' }
    }
    $db = Read-RecoveryDatabase "SELECT json_build_object('database',current_database(),'role',current_user,'session_user',session_user,'version',current_setting('server_version'),'encoding',current_setting('client_encoding'),'address',inet_server_addr(),'clients',COALESCE((SELECT json_agg(host(client_addr)) FROM pg_stat_activity WHERE datname=current_database() AND backend_type='client backend' AND pid<>pg_backend_pid()),'[]'::json))"
    if ($db.database -cne 'investment' -or $db.role -cne 'investment' -or $db.session_user -cne 'investment' -or
        $db.version -cne '16.15' -or $db.encoding -cne 'UTF8' -or $null -ne $db.address) { throw 'Database execution identity drift' }
    foreach ($client in $db.clients) { if (-not $client -or $client -cnotin @($inventory.items.status.podIP)) { throw 'Unattributed database client session' } }
}

function Assert-RecoveryReceipt {
    param([string]$Receipt)
    $markers = @('(?m)^\s*BEFORE\s*\|','(?m)^\s*AFTER_PENDING_COMMIT\s*\|','(?m)^COMMIT\s*\r?$','(?m)^COMMITTED:')
    $position = -1
    foreach ($marker in $markers) {
        $match = [regex]::Match($Receipt, $marker)
        if (-not $match.Success -or $match.Index -le $position) { throw 'UNKNOWN: missing/out-of-order commit receipt; independently inspect, never automatically retry' }
        $position = $match.Index
    }
}

function Assert-RecoveryReadback {
    param($Result, $Before, [string]$EvidenceId)
    $row = $Result.row
    if ($Result.incomplete_count -ne 0 -or -not $row -or -not $Before -or @($Before.PSObject.Properties).Count -ne 17 -or @($row.PSObject.Properties).Count -ne 17 -or
        $row.status -cne 'FAILED' -or -not $row.completed_at -or $row.completed_at -cne $row.updated_at -or
        $row.safe_error_code -cne 'ADMIN_WORKER_TERMINATION_VERIFIED' -or
        $row.safe_error_message -cne ('Administrative reconciliation after verified worker termination; original failure time and cause unknown. Evidence: ' + $EvidenceId)) { throw 'UNKNOWN: reconciliation readback mismatch' }
    foreach ($property in $Before.PSObject.Properties) {
        if ($property.Name -in @('status','completed_at','updated_at','safe_error_code','safe_error_message')) { continue }
        if (($property.Value | ConvertTo-Json -Compress) -cne ($row.($property.Name) | ConvertTo-Json -Compress)) { throw "UNKNOWN: preserved field changed: $($property.Name)" }
    }
}

function Read-RecoveryPreimage {
    param([string]$Evidence)
    $csv = @(Get-Content -LiteralPath (Join-Path $Evidence 'reviewed-preimages.csv') -Raw | ConvertFrom-Csv -Header payload)
    if ($csv.Count -ne 1) { throw 'Expected one retained preimage record' }
    # Assign the decoded JSON array before indexing it: @(<pipeline>)[0] leaves
    # a nested array under Windows PowerShell 5.1 instead of its 17-column row.
    $rows = $csv[0].payload | ConvertFrom-Json
    if (@($rows).Count -ne 1 -or @($rows[0].PSObject.Properties).Count -ne 17) { throw 'Expected one 17-column preimage row' }
    $rows[0]
}

function Read-RecoveryContinuation {
    param($Package)
    # These identify only the already-investigated separator/preflight failure.
    # A generic STOP, missing receipt, or a different failed attempt is insufficient.
    $aHash = 'c91a17091208bbe114a5a0a643d70dbe4a35175011803f5903158a94a98d6ac6'
    $cHash = '732f04f08b7d7b5004e0cea029f30cd75350ba47557ba8ea193087fb256d9eac'
    $priorHash = 'e2bfae6acfbb27fb6a6af6e91dad09501183d1820c80c711f5cc9abb6a3eea30'
    Assert-RecoveryHash (Join-Path $Package.Evidence 'phase-a-certificate-approval.json') $aHash
    Assert-RecoveryHash (Join-Path $Package.Evidence 'single-refresh-approval.json') $cHash
    Assert-RecoveryHash (Join-Path $Package.Evidence 'single-refresh-workflow.log') $priorHash
    if (@(Get-ChildItem -LiteralPath $Package.Evidence -Filter 'single-refresh-continuation-*.log' -File).Count) { throw 'Prior continuation exists; outcome requires independent review, never replay' }
    $a = Get-Content -LiteralPath (Join-Path $Package.Evidence 'phase-a-certificate-approval.json') -Raw | ConvertFrom-Json
    $c = Get-Content -LiteralPath (Join-Path $Package.Evidence 'single-refresh-approval.json') -Raw | ConvertFrom-Json
    $prior = Get-Content -LiteralPath (Join-Path $Package.Evidence 'single-refresh-workflow.log') -Raw
    $expectedPrior = "Phase A approval SHA-256: $aHash; Phase C approval SHA-256: $cHash" + [Environment]::NewLine + 'STOP: no automatic retry or cleanup; inspect retained artifacts'
    if ($prior.TrimEnd() -cne $expectedPrior) { throw 'Unrelated or ambiguous previous failure; continuation refused' }
    foreach ($record in @($a,$c)) {
        if ($record.refresh_id -cne 'ea48b845-e2a3-453b-8225-80a2a362d995' -or
            $record.correlation_id -cne '425aebf9-e576-4489-999a-87e52fe1a67d' -or
            $record.evidence_id -cne $Package.Certificate.evidence_id -or -not $record.approved_by -or
            $record.approved_at_utc -cnotmatch '^\d{4}-\d\d-\d\dT.*Z$') { throw 'Existing authorization identity/decision is incomplete' }
    }
    if ($a.independently_approved -isnot [bool] -or -not $a.independently_approved -or
        $a.approval_decision -cne 'APPROVE_PHASE_A_TERMINATION_CERTIFICATE' -or
        $a.certificate_sha256 -cne $Package.Hashes['termination-evidence.json'] -or
        $c.termination_evidence_sha256 -cne $a.certificate_sha256 -or $c.phase_a_approval_sha256 -cne $aHash -or
        $c.maintenance_approval -cne 'APPROVED_SINGLE_REFRESH_TERMINATION_AND_RECONCILIATION' -or
        $c.termination_verified -isnot [bool] -or -not $c.termination_verified -or
        $c.live_execution_authorized -isnot [bool] -or -not $c.live_execution_authorized -or
        $c.temporary_postgres_pod_file_staging_authorized -ne $true -or $c.connection_method_independently_approved -ne $true) { throw 'Existing Phase A/C authorization is not valid for continuation' }
    foreach ($binding in @('preimage_sha256','preimage_hex_sha256','capture_sql_sha256','reconciliation_sql_sha256','backup_id','backup_dump_sha256','backup_manifest_sha256')) {
        if ($c.$binding -cne $Package.C.$binding) { throw "Approved continuation binding changed: $binding" }
    }
    if (($c.execution_connection | ConvertTo-Json -Depth 8 -Compress) -cne ($Package.C.execution_connection | ConvertTo-Json -Depth 8 -Compress) -or
        ($c.authorized_fields -join '|') -cne ($Package.C.authorized_fields -join '|')) { throw 'Approved connection or five-field scope changed' }
    [pscustomobject]@{A=$a; C=$c; AHash=$aHash; CHash=$cHash; PriorLogHash=$priorHash}
}

function Assert-RecoveryUnchangedRefresh {
    param($Package)
    $before = Read-RecoveryPreimage $Package.Evidence
    $current = Read-RecoveryDatabase "SELECT json_build_object('row',(SELECT to_jsonb(r) FROM research.research_refresh_runs r WHERE refresh_run_id='ea48b845-e2a3-453b-8225-80a2a362d995'::uuid),'incomplete_count',(SELECT count(*) FROM research.research_refresh_runs WHERE completed_at IS NULL))"
    if ($before.refresh_run_id -cne $Package.C.refresh_id -or $before.status -cne 'RUNNING' -or $before.mode -cne 'TARGETED_LIVE' -or
        $null -ne $before.completed_at -or $current.incomplete_count -ne 1 -or -not $current.row -or
        (@($before.PSObject.Properties.Name | Sort-Object) -join '|') -cne (@($current.row.PSObject.Properties.Name | Sort-Object) -join '|')) { throw 'UNKNOWN or changed refresh state; continuation refused' }
    foreach ($property in $before.PSObject.Properties) {
        if (($property.Value | ConvertTo-Json -Compress) -cne ($current.row.($property.Name) | ConvertTo-Json -Compress)) { throw "Refresh no longer matches original preimage: $($property.Name)" }
    }
}

function Assert-RecoveryContinuationReady {
    param($Package)
    # Exact approved path, no files created; a symlink also counts as occupied.
    $check = 'if [ -e /tmp/single-refresh-ea48b845-20261018-022509 ] || [ -L /tmp/single-refresh-ea48b845-20261018-022509 ]; then printf STAGING_PRESENT; else printf STAGING_ABSENT; fi'
    $state = Invoke-DeploymentKubectl @('exec','postgres-0','-c','postgres','--','sh','-c',$check)
    if (($state -join '').Trim() -cne 'STAGING_ABSENT') { throw 'Previous staging exists or is unverified; continuation refused' }
    Assert-RecoveryUnchangedRefresh $Package
}

function Invoke-SingleRefreshRecovery {
    param([string]$Root, [switch]$ExecuteReconciliation, [switch]$ContinueFailedPreflight)
    if (-not $ExecuteReconciliation) { throw 'Execution switch missing; no approvals or external actions performed' }
    $package = Read-RecoveryPackage $Root
    if ($ContinueFailedPreflight) {
        $existing = Read-RecoveryContinuation $package
        if ((Read-Host 'Confirm the existing maintenance freeze remains unbroken: type CONFIRM_UNBROKEN_MAINTENANCE_FREEZE') -cne 'CONFIRM_UNBROKEN_MAINTENANCE_FREEZE') { throw 'Maintenance freeze not confirmed' }
        $ProjectRoot = $Root
        . (Join-Path $Root 'scripts/database/DeploymentSafety.ps1')
        Assert-RecoveryRuntime $package
        $package = Read-RecoveryPackage $Root
        $existing = Read-RecoveryContinuation $package
        $package.C = $existing.C
        Assert-RecoveryContinuationReady $package
        $logPath = Join-Path $package.Evidence ('single-refresh-continuation-' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ') + '-' + [guid]::NewGuid().ToString('N') + '.log')
        Invoke-RecoveryHandoff $package $existing.AHash $existing.CHash $logPath -PriorLogHash $existing.PriorLogHash
        return
    }
    # Never replay approval creation, staging or an ambiguously completed transaction.
    foreach ($name in @('phase-a-certificate-approval.json','single-refresh-approval.json','single-refresh-workflow.log')) {
        if (Test-Path -LiteralPath (Join-Path $package.Evidence $name)) { throw "Prior approval/execution artifact exists: $name; STOP, review without retry" }
    }
    $consent = Read-RecoveryConsent $package
    $ProjectRoot = $Root
    . (Join-Path $Root 'scripts/database/DeploymentSafety.ps1')
    Assert-RecoveryRuntime $package
    # Re-read the fixed bindings after interactive review, before making records.
    $package = Read-RecoveryPackage $Root
    $a = $package.A
    $a.artifact_type = 'SINGLE_REFRESH_PHASE_A_CERTIFICATE_APPROVAL'
    $a.review_status = 'EXPLICITLY_APPROVED_BY_NAMED_OPERATOR_REVIEWER'
    $a.operator_name = $consent.Name; $a.reviewer_name = $consent.Name; $a.reviewer_is_operator = $true
    $a.approved_by = $consent.Name; $a.approved_at_utc = $consent.PhaseAApprovedAtUtc
    $a.approval_decision = 'APPROVE_PHASE_A_TERMINATION_CERTIFICATE'; $a.independently_approved = $true
    $a.approved_statement = "I, $($consent.Name), approve the exact bound certificate and continuing maintenance freeze as operator and reviewer."
    $a.approval_record_handling = 'Approved immutable Phase A review record, separately retained from the unchanged certificate and prepared template.'
    $a.minimum_remaining_action = 'Phase C requires its separately retained explicit authorization; Phase A alone grants no live execution permission.'
    $a | Add-Member -NotePropertyName freeze_confirmed_at_utc -NotePropertyValue $consent.FreezeConfirmedAtUtc
    $aHash = Write-RecoveryRecord (Join-Path $package.Evidence 'phase-a-certificate-approval.json') $a
    $c = $package.C
    $c.artifact_type = 'SINGLE_REFRESH_PHASE_C_APPROVAL'; $c.review_status = 'EXPLICITLY_AUTHORIZED'
    $c.maintenance_approval = 'APPROVED_SINGLE_REFRESH_TERMINATION_AND_RECONCILIATION'
    $c.evidence_id_status = 'BOUND_TO_SEPARATELY_APPROVED_CERTIFICATE'
    $c.termination_verified = $true; $c.termination_evidence_sha256 = $package.Hashes['termination-evidence.json']
    $c.prepared_certificate_file = 'termination-evidence.json'
    $c.temporary_postgres_pod_file_staging_authorized = $true; $c.connection_method_independently_approved = $true
    $c.approved_by = $consent.Name; $c.approved_at_utc = $consent.PhaseCApprovedAtUtc; $c.live_execution_authorized = $true
    $c | Add-Member -NotePropertyName phase_a_approval_sha256 -NotePropertyValue $aHash
    $c | Add-Member -NotePropertyName freeze_confirmed_at_utc -NotePropertyValue $consent.FreezeConfirmedAtUtc
    $cHash = Write-RecoveryRecord (Join-Path $package.Evidence 'single-refresh-approval.json') $c
    $logPath = Join-Path $package.Evidence 'single-refresh-workflow.log'
    Invoke-RecoveryHandoff $package $aHash $cHash $logPath
}

function Invoke-RecoveryHandoff {
    param($Package, [string]$aHash, [string]$cHash, [string]$logPath, [string]$PriorLogHash)
    $stream = [IO.File]::Open($logPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::Read)
    $audit = [IO.StreamWriter]::new($stream, [Text.UTF8Encoding]::new($false)); $audit.AutoFlush = $true
    $reconciliationInvoked = $false
    Push-Location $package.Evidence
    try {
        $audit.WriteLine("Phase A approval SHA-256: $aHash; Phase C approval SHA-256: $cHash")
        if ($PriorLogHash) { $audit.WriteLine("CONTINUATION of the verified failed preflight; original audit SHA-256: $PriorLogHash; started UTC: $([DateTime]::UtcNow.ToString('o'))") }
        # Dot-source the hash-pinned handoff blocks to retain K/Expected/Endpoint.
        $preflight = . $package.Blocks[0]; $audit.WriteLine(($preflight -join [Environment]::NewLine))
        Assert-RecoveryRuntime $package
        if ($PriorLogHash) { Assert-RecoveryContinuationReady $package }
        $staging = . $package.Blocks[1]; $audit.WriteLine(($staging -join [Environment]::NewLine))
        $audit.WriteLine(($RemoteHashes -join [Environment]::NewLine))
        Assert-RecoveryRuntime $package
        if ($PriorLogHash) {
            Assert-RecoveryHash (Join-Path $package.Evidence 'single-refresh-workflow.log') $PriorLogHash
            Assert-RecoveryUnchangedRefresh $package
        }
        Assert-RecoveryHash (Join-Path $package.Evidence 'phase-a-certificate-approval.json') $aHash
        Assert-RecoveryHash (Join-Path $package.Evidence 'single-refresh-approval.json') $cHash
        foreach ($name in $package.Hashes.Keys) { Assert-RecoveryHash (Join-Path $package.Evidence $name) $package.Hashes[$name] }
        $reconciliationInvoked = $true
        $receipt = @(. $package.Blocks[2]) -join [Environment]::NewLine
        $audit.WriteLine($receipt); Assert-RecoveryReceipt $receipt
        $readback = Read-RecoveryDatabase "SELECT json_build_object('row',(SELECT to_jsonb(r) FROM research.research_refresh_runs r WHERE refresh_run_id='ea48b845-e2a3-453b-8225-80a2a362d995'::uuid),'incomplete_count',(SELECT count(*) FROM research.research_refresh_runs WHERE completed_at IS NULL))"
        # Parse only for comparison; original CSV/hex bytes are never serialized back.
        $before = Read-RecoveryPreimage $package.Evidence
        Assert-RecoveryReadback $readback $before $package.C.evidence_id
        $audit.WriteLine('COMMITTED_AND_INDEPENDENT_READBACK_VERIFIED')
        Write-Host 'Reconciliation committed and read back. Research Engine remains stopped/isolated. D/E observer and paused deployment need separate authorization; see SINGLE_REFRESH_RUNBOOK.md section 5.'
    } catch {
        $state = if ($reconciliationInvoked) { 'UNKNOWN: independently inspect database before any retry' } else { 'STOP: no automatic retry or cleanup; inspect retained artifacts' }
        $audit.WriteLine($state); throw "$state. $($_.Exception.Message)"
    } finally { Pop-Location; $audit.Dispose() }
}

if ($MyInvocation.InvocationName -ne '.') {
    $root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../..'))
    Invoke-SingleRefreshRecovery -Root $root -ExecuteReconciliation:$ExecuteReconciliation -ContinueFailedPreflight:$ContinueFailedPreflight
}
