# Single-refresh maintenance package — NOT AUTHORIZED FOR LIVE EXECUTION

Scope: refresh `ea48b845-e2a3-453b-8225-80a2a362d995`, correlation
`425aebf9-e576-4489-999a-87e52fe1a67d`, database `investment`, schema `research`,
context `k3d-ai-investment-dev`, namespace `ai-investment`.

Local preparation/tests do not authorize any live command below. Execute each
phase only after its recorded approval. No live preimage, termination certificate,
approval record or production connection configuration is supplied by this package.
The current refresh's original worker termination is still unproven.

## What is preserved

`reconcile_single_refresh.sql` changes exactly five fields of exactly this row:
`status`, `completed_at`, `updated_at`, `safe_error_code`, `safe_error_message`.
It records FAILED / ADMIN_WORKER_TERMINATION_VERIFIED and the reconciliation
clock, explicitly leaving the original failure time/cause unknown. Identity,
correlation, start time, counters, other refreshes, source evidence, checkpoints,
cycle history and published results are preserved. The original six-record
`reconcile_refresh_runs.sql` and `capture_refresh_preimages.sql` are unchanged.

The SQL verifies the approved SHA-256 against the **original CSV bytes**, not a
reformatted JSON document or a caller-provided claim about their hash. Hex is a
transport encoding of those exact bytes; it is not a second source of truth.
The server uses built-in PostgreSQL SHA-256, without an extension or server-file
access. Input is bounded to 64 KiB, one quoted CSV field/record, UTF-8, LF or CRLF.

## Required evidence and approvals

Maintain a new access-controlled evidence directory. Never overwrite old evidence.
Approval records must be immutable review records, separately retained from the
working files. Hashes computed during execution must be compared with those
records; they must not replace them.

- **A — freeze/isolate/terminate:** named operator, approved outage/abort window,
  exact executor/controller/host inventory, external-caller and automation freeze,
  and explicit authorization to interrupt uncommitted in-memory work. Capture all
  container IDs, pod UIDs, nodes, process generations/PIDs and cgroup paths before
  stopping anything. Exclude secret values from evidence. Keep research-service
  available. No forced deletion or automatic rollback.
- **Termination certificate:** either bind this run to its original execution and
  positively verify its irreversible termination, or close the inventory of every
  possible executor/location and verify all are terminated with recreation
  prevented. Include node runtime/task/PID/cgroup evidence, external-worker
  evidence, controller freeze and ended research DB sessions. Current pod start
  time, old row age, zero counters, missing pods, missing sessions and quiet logs
  are insufficient alone. An unknown/unreachable location means STOP.
- **B — backup:** a fresh frozen backup and isolated restore validation under
  [README section 4](README.md#4-current-recoverable-backup-approval-b), with exact
  backup ID, manifest/dump hashes, table/Flyway checks and retained verification
  output. Do not reuse the historical six-row backup as evidence for this row.
- **C — reconcile one row:** reviewer identity/time, exact UUID/correlation,
  termination certificate ID and SHA-256, backup evidence, reviewed capture/SQL
  hashes, approved connection service/server identity, and the separately reviewed
  full 17-column CSV's SHA-256. Authorize only the five fields above. This approval
  must explicitly use `APPROVED_SINGLE_REFRESH_TERMINATION_AND_RECONCILIATION`;
  the old six-row approval token is rejected.
- **D/E — observer and paused startup:** separate authorization for the existing
  idle observer and controlled startup/deployment, approved immutable image(s),
  startup pause, isolation, migration verification and the 600-second quiet window.
  Reconciliation approval alone does not authorize startup, deployment, image
  building, baseline advancement, policy removal or traffic/recovery restoration.

No calendar migration, ETF environment, new source acquisition or image build is
needed to reconcile this row. Any later new artifact remains a separate approval.

## 1. Load reviewed helpers and collect the current inventory (read-only)

Use an approved `psql` client and an approved libpq connection service/passfile.
Do not place a password in a command, approval record or evidence file. The local
test runner uses the private PostgreSQL image's psql and requires no live service.

When Windows has no client/service, an explicitly reviewed alternative is the
existing `postgres-0` container's psql using `-X -w -h /var/run/postgresql
-U investment -d investment` through pinned-context kubectl exec. This requires
approval of the exact pod UID/container/Unix-socket endpoint and temporary
protected-file staging; no credential is passed or displayed. The prepared
[postgres-pod handoff](../../../diagnostics/maintenance-20261018-022509/POSTGRES_POD_HANDOFF.prepared.md)
contains the command and guards for this evidence set. It is not authorization.
This changes only client transport, not SQL, hashes, termination or transaction
requirements. Do not create a fake libpq service or rerun capture merely to change
transport. The service-specific examples below apply only to the local-client path.

```powershell
$ProjectRoot = 'C:\workspace\ai-investment-platform'
Set-Location $ProjectRoot
$ErrorActionPreference = 'Stop'
$HelmValues = Join-Path $ProjectRoot 'infrastructure/helm/ai-investment-platform/values-dev.yaml'
$RuntimeDir = Join-Path $ProjectRoot '.tmp'
. ./scripts/database/DeploymentSafety.ps1
# The compatibility loader intentionally loads only some definitions. Load the
# two pure dependencies required for registry verification and patch creation.
foreach ($functionName in @('Get-VerifiedRegistryImage', 'Get-LocalFullEtfExecutionFlag')) {
    $functionNode = $deploymentAst.Find({ param($n)
        $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $functionName
    }, $true)
    if (-not $functionNode) { throw "Missing reviewed helper: $functionName" }
    . ([scriptblock]::Create($functionNode.Extent.Text))
}
function K {
    & kubectl --context k3d-ai-investment-dev -n ai-investment @args
    if ($LASTEXITCODE -ne 0) { throw 'kubectl failed; STOP' }
}
```

Set `$Evidence` to the approved **new** restricted directory, which must already
exist, and save the inventory there under approval A. Do not dot-source platform.ps1
or invoke its entry point. Inspect all namespaces, execution hosts, HPA/jobs,
GitOps/operators, external callers and API-server tunnels before freezing them.

```powershell
$Inventory = Get-DeploymentInventory
$Deployment = K get deployment research-engine -o json | ConvertFrom-Json
$ResearchPods = @($Inventory.items | Where-Object { $_.metadata.labels.'app.kubernetes.io/component' -eq 'research-engine' })
if ($ResearchPods.Count -ne 1 -or $ResearchPods[0].status.phase -ne 'Running') { throw 'Ambiguous research owner; STOP' }
$ResearchPod = $ResearchPods[0]
$CallerNames = @()
foreach ($component in @('api-gateway', 'portfolio-service', 'mcp-gateway')) {
    $matches = @($Inventory.items | Where-Object { $_.metadata.labels.'app.kubernetes.io/component' -eq $component })
    if ($matches.Count -ne 1) { throw "Ambiguous caller: $component" }
    $CallerNames += $matches[0].metadata.name
}
$ResearchService = K get service research-engine -o json | ConvertFrom-Json
$Gate = @{
    ResearchPod=$ResearchPod.metadata.name; ResearchUID=$ResearchPod.metadata.uid
    PodIP=$ResearchPod.status.podIP; ServiceIP=$ResearchService.spec.clusterIP
    Selector=$Deployment.spec.selector; Callers=$CallerNames; AuditPath=$Evidence
    Names=@('research-engine')
}
# Require the full three-label Deployment selector used by current platform.ps1.
$labels = $Gate.Selector.matchLabels
if ($labels.'app.kubernetes.io/component' -ne 'research-engine' -or
    $labels.'app.kubernetes.io/instance' -ne 'ai-investment-platform' -or
    $labels.'app.kubernetes.io/name' -ne 'ai-investment-platform' -or
    @($labels.PSObject.Properties).Count -ne 3 -or $Gate.Selector.matchExpressions) { throw 'Unexpected selector' }
```

Freeze/recheck the reviewed caller/controller set. Reconcile unexpected existing
policies through separate review; do not remove them. After approval A, the existing
`Start-DeploymentIsolation -Gate $Gate` creates **research-bootstrap-isolation**
with that exact selector and calls `Assert-DeploymentIsolation -Probe`. Its current
kube-router proof requires the correct firewall chain/ICMP REJECT rule, native
refusal/exit 1 and an increasing reject counter. Refusal/timeout alone is not proof.
Do not use the older one-label YAML or the former timeout-only instructions.

## 2. Stop and positively verify every executor (approval A)

Follow [README section 3](README.md#3-stop-and-verify-all-writers-approval-a-continued):
capture identities, scale Research Engine to zero using the observed replica
precondition, wait for deletion, and verify no recreation. Stop any separately
identified external executor by its approved procedure. This is not authorized now.

Read-only runtime commands, on every inventoried node:

```powershell
docker exec $NodeContainer crictl ps -a --name research-engine
if ($LASTEXITCODE -ne 0) { throw 'CRI inventory failed' }
$ContainerInspection = docker exec $NodeContainer crictl inspect $CapturedContainerID | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw 'Container inspection failed' }
# Retain only reviewed status, identity, PID and cgroup fields. Runtime config
# can contain resolved environment secrets; do not print/save it in full.
$ContainerInspection.status | Select-Object id,state,startedAt,finishedAt,exitCode
$ContainerInspection.info | Select-Object pid
$ContainerInspection.info.runtimeSpec.linux | Select-Object cgroupsPath
docker exec $NodeContainer ctr -n k8s.io tasks list
if ($LASTEXITCODE -ne 0) { throw 'Task inventory failed' }
```

Corroborate captured PID/process-generation and cgroup termination on each host;
reused PIDs are not the original process. Also verify ended application DB sessions
using approved read-only pg_stat_activity inspection. Missing/garbage-collected
runtime metadata alone is not proof. Unknown historical ownership requires the
closed inventory/all-executors-terminated route, not a timestamp inference.

Only after the termination certificate is reviewed proceed to approval B's fresh
backup/isolated restore. Keep isolation, controller/caller freeze and schema freeze.

## 3. Capture, review and bind the original preimage (read-only, then approval C)

Configure the approved libpq service and independently verify database `investment`,
server identity, role and UTF-8 client encoding. Store that service name in the
reviewed approval record; do not infer it from environment defaults. Copy the
reviewed SQL/capture files to the evidence directory and record their SHA-256s.
Do not overwrite an existing capture or use PowerShell JSON/CSV reserialization.

```powershell
# $ConnectionService must be the reviewed libpq service name, not a password/DSN.
if ($ConnectionService -notmatch '^[A-Za-z0-9_-]{1,80}$') { throw 'Unreviewed connection service' }
$Connection = "service=$ConnectionService"
psql -X --dbname $Connection -v ON_ERROR_STOP=1 -c 'BEGIN READ ONLY; SELECT current_database(),current_user,inet_server_addr(),inet_server_port(); SHOW client_encoding; ROLLBACK;'
if ($LASTEXITCODE -ne 0) { throw 'Database identity check failed' }
$CsvPath = Join-Path $Evidence 'reviewed-preimages.csv'
if (Test-Path -LiteralPath $CsvPath) { throw 'Do not overwrite prior evidence' }
psql -X --dbname $Connection -q -v ON_ERROR_STOP=1 -f (Join-Path $Evidence 'capture_refresh_preimages.sql') -o $CsvPath
if ($LASTEXITCODE -ne 0) { throw 'Preimage capture failed' }
Get-FileHash -LiteralPath $CsvPath -Algorithm SHA256
```

Review the entire single JSON row, all 17 keys/values/nulls/UTC microseconds,
the exact UUID/correlation, RUNNING/TARGETED_LIVE, NULL completion, six zero
counters and no other incomplete row. Review the result against the fresh backup
and termination certificate. Preserve the original bytes in the controlled evidence
store. A reviewer records the approved lowercase SHA-256; capture alone is not approval.

The separately approved `single-refresh-approval.json` must contain:
`refresh_id`, `correlation_id`, `maintenance_approval`, `evidence_id`,
`termination_verified=true`, `termination_evidence_sha256`, `preimage_sha256`,
`reconciliation_sql_sha256`, `capture_sql_sha256`, `connection_service`,
`backup_id`, `approved_by`, `approved_at_utc`. The evidence ID must resolve to the
reviewed termination certificate (retain its file as `termination-evidence.json`).
No such approval/certificate is provided in this repository.

For the postgres-pod alternative, replace `connection_service` with an independently
approved `execution_connection` object binding context, namespace, pod UID, container,
Unix socket directory, database and role; explicitly authorize temporary pod-file
staging. Use that handoff's endpoint and approval checks instead of the service-only
PowerShell example. All other approval/hash fields remain mandatory. Prepared files
with null approval/reviewer/certificate hash or `termination_verified=false` cannot
authorize either connection path.

After that review, derive transport hex from the **unchanged** CSV:

```powershell
$Approval = Get-Content -LiteralPath (Join-Path $Evidence 'single-refresh-approval.json') -Raw | ConvertFrom-Json
if ($Approval.refresh_id -ne 'ea48b845-e2a3-453b-8225-80a2a362d995' -or
    $Approval.correlation_id -ne '425aebf9-e576-4489-999a-87e52fe1a67d' -or
    $Approval.maintenance_approval -cne 'APPROVED_SINGLE_REFRESH_TERMINATION_AND_RECONCILIATION' -or
    $Approval.termination_verified -ne $true -or $Approval.evidence_id -cnotmatch '^[A-Za-z0-9_-]{8,100}$' -or
    -not $Approval.approved_by -or -not $Approval.approved_at_utc -or -not $Approval.backup_id -or
    $Approval.connection_service -cne $ConnectionService) { throw 'Missing exact reviewed authorization' }
function Assert-ApprovedHash([string]$Path, [string]$Expected) {
    if ($Expected -cnotmatch '^[a-f0-9]{64}$' -or
        (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() -cne $Expected) { throw "Approved hash mismatch: $Path" }
}
Assert-ApprovedHash $CsvPath $Approval.preimage_sha256
Assert-ApprovedHash (Join-Path $Evidence 'termination-evidence.json') $Approval.termination_evidence_sha256
Assert-ApprovedHash (Join-Path $Evidence 'reconcile_single_refresh.sql') $Approval.reconciliation_sql_sha256
Assert-ApprovedHash (Join-Path $Evidence 'capture_refresh_preimages.sql') $Approval.capture_sql_sha256
$OriginalBytes = [IO.File]::ReadAllBytes($CsvPath)
if ($OriginalBytes.Length -gt 65536) { throw 'Unexpected preimage size' }
$HexPath = Join-Path $Evidence 'reviewed-preimages.hex'
if (Test-Path -LiteralPath $HexPath) { throw 'Do not overwrite prior evidence' }
$Hex = ([BitConverter]::ToString($OriginalBytes)).Replace('-', '').ToLowerInvariant()
[IO.File]::WriteAllText($HexPath, $Hex + "`n", [Text.Encoding]::ASCII)
```

The SQL independently hashes the decoded original bytes. A changed hex file,
CSV, newline or hash must fail; do not regenerate an approval to bypass drift.

## 4. Exact single-row transaction (approval C only)

Immediately recheck isolation, zero app replicas/executors, recreation prevention,
DB sessions and the approved frozen preimage. Require a new unused log path.

```powershell
$LogPath = Join-Path $Evidence 'single-refresh-reconciliation.log'
if (Test-Path -LiteralPath $LogPath) { throw 'Do not overwrite prior execution output' }
Push-Location $Evidence
try {
    psql -X --dbname $Connection -v ON_ERROR_STOP=1 `
      -v "maintenance_approval=$($Approval.maintenance_approval)" `
      -v "evidence_id=$($Approval.evidence_id)" `
      -v "preimage_sha256=$($Approval.preimage_sha256)" `
      -f (Join-Path $Evidence 'reconcile_single_refresh.sql') -L $LogPath
    if ($LASTEXITCODE -ne 0) { throw 'Reconciliation stopped; inspect outcome before any retry' }
} finally { Pop-Location }
```

Require BEFORE, AFTER_PENDING_COMMIT, COMMIT and COMMITTED receipt with the approved
hash/evidence. Independently read back the target and global incomplete count in a
read-only transaction. Require FAILED/ADMIN_WORKER_TERMINATION_VERIFIED, equal
reconciliation completed_at/updated_at, preserved other 12 fields, zero incomplete
rows and preserved unrelated data/history. The lock/full-preimage/trigger/schema
guards and tests are not permission to skip independent operator verification.

Disconnect or missing commit receipt means UNKNOWN; independently inspect the
database before considering a retry. Never restore RUNNING or claim success to
make the observer pass. All SQL errors stop psql and roll back the transaction.

## 5. Observer and paused-startup handoff (D/E only)

Follow [README sections 6–7](README.md#6-observation-while-research-is-stopped-approval-d).
Preserve `$Gate` as the isolation/caller gate; use a separate `$ObservationGate`
with `ResearchPod='research-maintenance-observer'; Migrations=$null` for DB reads.
The observer must be the reviewed idle process, never application startup. Inspect
actual admission, labels, selectors, command, security, env references and deadline.
Require QUIET_SNAPSHOT before any application start; null migrations do not verify
Flyway. Refresh the isolation gate's pod UID/IP/identity for the new app before
probing it. Require the existing 600-second window with 30-second samples and
unchanged DB fingerprint, pod identity, workloads, policies and Helm revision.

Do not invoke normal deployment preflight with zero app replicas; it requires a
running app pod. Do not call Complete-DeploymentSafety or remove isolation, restore
traffic, enable recovery/scheduling, roll back images, advance a baseline or run
Radar automatically. Each needs its own reviewed authorization. Existing startup
image/init digest and resource-version safeguards remain unchanged.

## STOP / GO conditions and local validation

Stop on any unknown executor/controller/location, unreachable node, missing
termination or backup evidence, unapproved connection, drift, new incomplete work,
schema/trigger/rule change, lock/statement timeout, failed SQL/receipt, BLOCKED or
UNKNOWN observation, unverified migration/image, startup activity, missed cadence
or observer expiry (four hours). There is no automatic cleanup/retry/rollback.

Local test command (only a uniquely named, network-none, tmpfs PostgreSQL container;
already-local image pinned to its inspected ID, no ports/mounts/live credentials):

```powershell
$env:AIP_MAINTENANCE_ISOLATED_PG = '1'
./ai/research-engine/.venv/Scripts/python.exe -B -m unittest discover -s scripts/database/maintenance -p test_maintenance.py -v
```

Passing tests permit package review only. Live execution remains NO-GO until all
phase-specific approvals, termination proof, backup/restore and immutable preimage
binding exist. Local preparation must stop before any live operation.

Local validation on 2026-10-18: **38 tests passed, 0 failed, 0 skipped** (26
isolated PostgreSQL tests, including 21 single-refresh tests; 12 existing
observer/SQL-contract tests). The complete run took 154.749 seconds. PostgreSQL
was the already-local image ID
`sha256:1a6ab3f5345eb6dbe04a1349529caabdb0ab09293a09590fad07b2246bfa4b54`,
in disposable network-none containers with tmpfs data and no ports or mounts.
All seven PowerShell runbook blocks parsed; Python syntax and whitespace checks
passed. The original six-record SQL, capture SQL and rollback-baseline files
retained their pre-task SHA-256s. No live maintenance was executed.
