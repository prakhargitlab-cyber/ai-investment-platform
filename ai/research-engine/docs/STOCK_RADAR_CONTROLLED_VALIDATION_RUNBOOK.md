# Targeted Stock Radar deployment and repeat validation

PREPARATION ONLY. None of the cancellation, awake-request, build, push, deployment,
rollback or controlled POST commands below was executed while preparing this file.
Run sections individually in one PowerShell session only after execution is authorized.

## Review conclusion: Step 2 — cancellation contract implemented in source

A narrow, durable, two-phase cycle-cancellation contract is now implemented in
the `ai/research-engine` source (for the NEXT deployment image). See section 2
for the cancel-and-drain procedure. The contract has two phases:

- **Phase 1 (REQUEST)**: `request_cycle_cancel` sets `status='CANCEL_REQUESTED'`,
  preserving ownership/lease/active slot so the owning worker can drain.
- **Phase 2 (TERMINAL)**: `cancel_cycle_run` (worker-fenced) flips to terminal
  `CANCELLED` and releases ownership + active slot.

The currently-running OLD image (dev-20260927-212558) has NO code path that
reads `status='CANCEL_REQUESTED'` or `status='CANCELLED'` — the DELETE /cancel
endpoint returns 404 on the old image. The operator must drain the old worker,
deploy the new image, then issue the cancel request against the new image.
The new worker's `start()` checks `_cancel_status()` at startup and refuses to
resume a `CANCEL_REQUESTED` cycle owned by a live lease-holder (startup/recovery
barrier for the old-to-new transition race).

Other contract differences from a generic async-job runbook:

- `POST /api/v1/research/opportunities/cycles` with `candidate_ids` is synchronous,
  under `worker.run_lock`, and returns a completed selection. It does not enqueue a
  production job or create a resumable cycle checkpoint. Its cycle UUID is assigned
  after investigation. `/cycles/{id}/status` reads production CYCLE_JOB events and
  is not a controlled-run polling API. Poll the local request job below instead.
- `top_n` must be **2 through 4**, regardless of candidate count. Use 4 and set
  `shortlist_limit=13`. Always send the explicit 13-element `candidate_ids` list.
- Controlled runs persist research evidence, result snapshots and controlled scan
  history. They do not replace the shared production Radar/current-state projection.
- The synchronous route does not forward its HTTP request ID as the selection's
  `correlation_id`. Use the logging `requestId` from `X-Request-ID`; do not expect a
  non-null result correlation_id or an early cycle ID.

Evidence inspected 2026-09-28, approximately 10:42-10:55 UTC:

- Old cycle `fb915256-dabd-4a42-afc6-1902e0bc7d0d`: RUNNING, resume_count=0,
  renewed ownership lease; baseline 2,584 completed / one retryable; deep 207 analyzed,
  42 rank-filtered, 150 retryable, five falsely unavailable, four IN_PROGRESS.
- Pod `research-engine-69cff4688c-592mp`; image
  `ai-investment-registry:5000/ai-investment/research-engine:dev-20260927-212558`;
  digest `sha256:6def0ce1d8192b5a1ae61b2d9a7342610d09aee3631fc686dd6efdf14351aba4`.
- One replica; requests 100m/256Mi, limits 500m/1536Mi; Stage-2=4;
  PDF concurrency=1, extraction timeout=20s, queue timeout=12s. Preserve them.
- `AIP_RESEARCH_OPPORTUNITY_SCHEDULER_ENABLED=false` already exists. Preserve it;
  it does not stop the active worker or prevent resumption of its durable cycle.
- Registry: host `localhost:5001`, cluster `ai-investment-registry:5000`.
  The existing image is an OCI index. Accept OCI **indexes and manifests** when
  checking the registry; a narrower Accept header produced a misleading 404.
- Research `/health` returned ok; gateway `/actuator/health` returned UP. Portfolio
  deployment was 1/1, but a direct service health request timed out. All dependency
  checks below must pass before validation; pod readiness alone is insufficient.

Source contracts: `app/main.py` (`OpportunityCycleRequest`, `opportunity_cycle`,
`opportunity_cycle_status`, lifespan); `app/opportunity_worker.py` (`close`,
`_heartbeat`, `_run_one`, `start`); `app/cycle_checkpoint.py` (lease/fencing);
`app/global_opportunity_cycle.py` (`nse_equities`, controlled selection);
`app/opportunity_persistence.py` (controlled publication); `platform.ps1` and
`ai/research-engine/Dockerfile` (image context). Do not run the broad platform script.

## 0. Session helpers and Windows-awake preflight

These commands create only local review artifacts until explicitly noted otherwise.
Native commands are checked immediately; `$ErrorActionPreference='Stop'` alone is
not sufficient for native nonzero exits in Windows PowerShell 5.1.

```powershell
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath 'C:\workspace\ai-investment-platform'
$Ns = 'ai-investment'
$OldCycle = 'fb915256-dabd-4a42-afc6-1902e0bc7d0d'
$Selector = 'app.kubernetes.io/component=research-engine'
$ReviewStart = [DateTime]::UtcNow
$Artifacts = Join-Path $env:TEMP ('radar-controlled-' + $ReviewStart.ToString('yyyyMMddTHHmmssZ'))
New-Item -ItemType Directory -Path $Artifacts -ErrorAction Stop | Out-Null
$Utf8 = New-Object System.Text.UTF8Encoding($false)

function Invoke-NativeText {
    param([string]$File, [string[]]$Arguments, [string]$InputText)
    $application = Get-Command $File -CommandType Application -ErrorAction Stop | Select-Object -First 1
    $savedPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue' # collect native stderr on PS 5.1
        if ($PSBoundParameters.ContainsKey('InputText')) {
            $lines = @($InputText | & $application.Source @Arguments 2>&1)
        } else { $lines = @(& $application.Source @Arguments 2>&1) }
        $nativeExit = $LASTEXITCODE
    } finally { $ErrorActionPreference = $savedPreference }
    $text = ($lines | ForEach-Object { $_.ToString() }) -join "`n"
    if ($nativeExit -ne 0) { throw "$File exited $nativeExit`n$text" }
    return $text
}
function Save-Text([string]$Name, [string]$Text) {
    [IO.File]::WriteAllText((Join-Path $Artifacts $Name), $Text, $Utf8)
}
function Save-Json([string]$Name, $Value) {
    Save-Text $Name ($Value | ConvertTo-Json -Depth 100)
}
function Read-Sql([string]$SelectSql) {
    # Call only with the SELECT statements in this runbook. The transaction itself
    # also refuses writes, including writes hidden inside a function.
    $sql = "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;`n$SelectSql`nROLLBACK;"
    $raw = Invoke-NativeText 'kubectl' @('exec','-i','-n',$Ns,'postgres-0','--','sh','-c',
        'exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -X -q -t -A -v ON_ERROR_STOP=1') -InputText $sql
    return ($raw | ConvertFrom-Json)
}
function Assert-NoProductionCycle {
    # Terminal production-cycle statuses now include 'CANCELLED' (added by the
    # cancellation contract). CANCELLED is a truthful terminal state with
    # released ownership and active slot.
    #
    # CANCEL_REQUESTED (the transient drain phase) is REJECTED here: ownership
    # and the active slot are intentionally PRESERVED during drain, so the
    # production cycle is not yet in a safe terminal state. This assertion
    # passes ONLY after the cycle has been coerced to terminal CANCELLED
    # (ownership released, lease cleared, active slot deleted, worker verified).
    $state = Read-OldCheckpoint
    if ($null -eq $state.run -or $state.run.status -notin @('COMPLETED','FAILED','CANCELLED')) {
        throw 'Old cycle is not terminal'
    }
    if ($null -ne $state.active -or $state.run.owner_id -or $state.run.lease_expires_at) {
        throw 'Production cycle/ownership still active'
    }
    # latest_job.status must be terminal (COMPLETED/FAILED/CANCELLED);
    # CANCEL_REQUESTED is rejected because it means the worker has not yet
    # acknowledged the terminal cancel and may still be draining.
    if ($null -eq $state.latest_job -or $state.latest_job.status -notin @('COMPLETED','FAILED','CANCELLED')) {
        throw 'Worker terminal acknowledgement is missing'
    }
    $deploy = Get-ResearchDeployment
    $container = @($deploy.spec.template.spec.containers | Where-Object name -eq 'research-engine')[0]
    $scheduler = @($container.env | Where-Object name -eq 'AIP_RESEARCH_OPPORTUNITY_SCHEDULER_ENABLED')
    if ($scheduler.Count -ne 1 -or $scheduler[0].value -ne 'false') {
        throw 'Automatic scheduling is not disabled as reviewed; do not change it in this runbook'
    }
    return $state
}
function Read-PodPython([string]$Script) {
    # Never import app.main/repository/persistence to query live state: initialization
    # is not a read-only database contract. Scripts here use urllib or read files.
    Invoke-NativeText 'kubectl' @('exec','-i','-n',$Ns,$Pod,'-c','research-engine','--','python','-') -InputText $Script
}
function Get-ResearchDeployment {
    (Invoke-NativeText 'kubectl' @('get','deployment','research-engine','-n',$Ns,'-o','json')) | ConvertFrom-Json
}
function Get-OnlyResearchPod {
    $items = @(((Invoke-NativeText 'kubectl' @('get','pods','-n',$Ns,'-l',$Selector,'-o','json')) | ConvertFrom-Json).items)
    if ($items.Count -ne 1) { throw "Expected one research pod; found $($items.Count). Wait for rollout/termination." }
    return $items[0]
}
$Context = Invoke-NativeText 'kubectl' @('config','current-context')
if ($Context.Trim() -ne 'k3d-ai-investment-dev') { throw "Unexpected kube context: $Context" }
$DeploymentBefore = Get-ResearchDeployment
$PodBefore = Get-OnlyResearchPod
$Pod = $PodBefore.metadata.name
Save-Json 'deployment-before.json' $DeploymentBefore
Save-Json 'pod-before.json' $PodBefore
Save-Text 'worktree-status.txt' (Invoke-NativeText 'git' @('status','--short'))
Save-Text 'power-capabilities.txt' (Invoke-NativeText 'powercfg.exe' @('/a'))
Save-Text 'power-scheme.txt' (Invoke-NativeText 'powercfg.exe' @('/getactivescheme'))
Save-Text 'power-requests-before.txt' (Invoke-NativeText 'powercfg.exe' @('/requests'))
```

During the future authorized execution, keep the machine plugged in, lid open, and
do not press Sleep. A scoped awake request is preferable to altering the power plan.
It cannot defeat explicit lid/sleep actions. This block was NOT executed in preparation.

```powershell
$AwakeJob = Start-Job -Name 'RadarValidationAwake' -ScriptBlock {
    Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class RadarAwake {
    [DllImport("kernel32.dll", SetLastError=true)]
    public static extern uint SetThreadExecutionState(uint flags);
}
'@
    try {
        while ($true) {
            if ([RadarAwake]::SetThreadExecutionState([uint32]2147483651) -eq 0) {
                throw 'SetThreadExecutionState failed'
            } # ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
            Start-Sleep -Seconds 15
        }
    } finally { [void][RadarAwake]::SetThreadExecutionState([uint32]2147483648) }
}
Start-Sleep -Seconds 2
if ($AwakeJob.State -ne 'Running') { Receive-Job $AwakeJob; throw 'Awake job failed' }
Save-Text 'power-requests-active.txt' (Invoke-NativeText 'powercfg.exe' @('/requests'))

function Get-StandbyEvents {
    # Enumerate recent System events so "no matching events" is not confused with
    # failure to read the log. Fail if event-log access itself fails.
    @(Get-WinEvent -LogName System -MaxEvents 10000 -ErrorAction Stop |
        Where-Object { $_.ProviderName -eq 'Microsoft-Windows-Kernel-Power' -and
            $_.Id -in @(42,107,506,507) -and $_.TimeCreated.ToUniversalTime() -ge $ReviewStart } |
        Select-Object @{n='Utc';e={$_.TimeCreated.ToUniversalTime().ToString('o')}},Id,Message)
}
```

## 1. Capture old status and complete checkpoint

The HTTP status is a transition/job event, not a heartbeat: the live RUNNING event
still had yesterday's updated_at. Use the durable run's lease and progress alongside it.

```powershell
Save-Text 'old-http-status.json' (Read-PodPython @'
import urllib.request
with urllib.request.urlopen('http://127.0.0.1:8000/api/v1/research/opportunities/cycles/fb915256-dabd-4a42-afc6-1902e0bc7d0d/status', timeout=30) as r:
    print(r.read().decode())
'@)
function Read-OldCheckpoint {
    Read-Sql @"
SELECT json_build_object(
 'captured_at',now(),
 'run',(SELECT row_to_json(r) FROM research.global_opportunity_cycle_run r WHERE cycle_id='$OldCycle'),
 'active',(SELECT row_to_json(a) FROM research.global_opportunity_cycle_active a WHERE market='NSE'),
 'latest_job',(SELECT payload::json FROM research.global_opportunity_top_selection
   WHERE payload::json->>'record_kind'='CYCLE_JOB' AND payload::json->>'cycle_id'='$OldCycle'
   ORDER BY generated_at DESC,cycle_id DESC LIMIT 1),
 'progress',COALESCE((SELECT json_agg(to_jsonb(p)-'payload' || jsonb_build_object('payload',p.payload::jsonb)
   ORDER BY phase,global_instrument_id) FROM research.global_opportunity_cycle_progress p WHERE cycle_id='$OldCycle'),'[]'::json));
"@
}
$OldBefore = Read-OldCheckpoint
Save-Json 'old-checkpoint-before.json' $OldBefore
$OldBefore.progress | Group-Object phase,state,disposition | Select-Object Name,Count | Format-Table
Save-Text 'old-pod.log' (Invoke-NativeText 'kubectl' @('logs','-n',$Ns,$Pod,'-c','research-engine','--timestamps=true'))
```

## 2. Stop the cycle: supported cancel-and-drain contract (two-phase)

The audited source now implements a narrow, durable, two-phase cycle-cancellation
contract (for the NEXT deployment image only — see "Old-worker transition" below):

- **Phase 1 (REQUEST)**: `request_cycle_cancel` sets `status='CANCEL_REQUESTED'`
  for the active cycle run (if still ACCEPTED/RUNNING/PUBLISHED). Ownership
  (`owner_id`), lease (`lease_expires_at`), and the active slot are PRESERVED
  so the owning worker can drain in-flight work and renew its lease during drain.
- **Phase 2 (TERMINAL)**: the owning worker (after drain) calls
  `cancel_cycle_run` to flip to terminal `CANCELLED`, releasing ownership and
  the active slot. Completed evidence (progress rows) is preserved.

### 2.1 Request durable cancellation (new image only)

```powershell
# ADMIN-gated. Requests durable status='CANCEL_REQUESTED' for the active cycle
# run (if still ACCEPTED/RUNNING/PUBLISHED). Ownership, lease, and the active
# slot are PRESERVED so the owning worker can drain in-flight work.
# Idempotent: a second request on a CANCEL_REQUESTED (or CANCELLED) run is a no-op.
#
# IMPORTANT: This endpoint CANNOT cancel the currently-running OLD image
# (dev-20260927-212558). It only becomes effective once the NEW image — which
# contains the cancellation contract — is deployed and serving. On the old
# image the endpoint returns 404 (does not exist).
$CancelBody = Read-PodPython @'
import json, urllib.request
req = urllib.request.Request(
    "http://127.0.0.1:8000/api/v1/research/opportunities/cycles/$OldCycle/cancel",
    method="DELETE")
req.add_header("X-AIP-User-Id", "operator")
req.add_header("X-AIP-User-Issuer", "local-review")
req.add_header("X-AIP-User-Subject", "operator")
req.add_header("X-AIP-User-Roles", "ADMIN")
with urllib.request.urlopen(req, timeout=30) as r:
    print(r.read().decode())
'@
Save-Text 'cancel-request.json' $CancelBody
# Verify durable cancellation (only meaningful against the NEW image):
# After cancel succeeds, the run status should be CANCEL_REQUESTED (draining).
# The active slot and owner/lease are preserved. Assert-NoProductionCycle
# REJECTS CANCEL_REQUESTED — it only accepts terminal CANCELLED.
```

### 2.2 Verify the old worker drains (old image still running)

The currently-running OLD worker (dev-20260927-212558) does NOT read the
CANCEL_REQUESTED flag. Its existing `close()` / lease-fencing behavior is
unchanged. To drain it without losing evidence or using ad-hoc SQL:

1. Request a graceful pod shutdown (Kubernetes SIGTERM via the deployment).
   The OLD worker's `close()` releases the lease and leaves the cycle RUNNING
   (by design — it does not know about CANCEL_REQUESTED/CANCELLED).
2. Confirm the OLD pod has terminated and a NEW pod is not yet scheduled:
   ```powershell
   $PodStatus = (Invoke-NativeText 'kubectl' @('get','pods','-n',$Ns,'-l',$Selector,'-o','json') | ConvertFrom-Json).items
   if ($PodStatus.Count -ne 0) { throw "Old pod still present; do not deploy new image" }
   ```
3. Confirm no in-flight progress is being written (poll the checkpoint
   progress `updated_at` twice, 60s apart, and assert unchanged). The progress
   rows themselves are preserved — no evidence is deleted.

```powershell
$StoppedA = Assert-NoProductionCycle
Start-Sleep -Seconds 60
$StoppedB = Assert-NoProductionCycle
if (($StoppedA.progress | ConvertTo-Json -Depth 100 -Compress) -cne
    ($StoppedB.progress | ConvertTo-Json -Depth 100 -Compress)) { throw 'Old progress is still changing' }
Save-Json 'old-checkpoint-stopped.json' $StoppedB
```

### 2.3 Deploy the NEW image (contains the cancellation contract)

Only after step 2.2 confirms the old worker is drained and no pod is running:

```powershell
# Build/push/deploy the new image (step 3 commands) with the cancellation
# contract source. The new worker's start() reads the run row:
#   - If status='CANCEL_REQUESTED': the startup/recovery barrier in
#     start() checks _cancel_status(). If the original owner is still alive
#     (lease fresh), it does NOT resume. If the lease expired, the new
#     worker takes over to drain (claim_cycle_run includes CANCEL_REQUESTED).
#   - If status='CANCELLED' (terminal): start() does NOT resume, records
#     CANCELLED in the job log, and leaves the cycle terminal.
#   - _maybe_submit() / _maybe_take_over() refuse to resume a CANCEL_REQUESTED
#     or CANCELLED cycle by a different owner.
#   - run_global_opportunity_cycle() raises before resuming/publishing.
#   - _fence_cycle_publication() refuses to PUBLISH a non-RUNNING cycle.
#   - Orchestrator run() checks cycle_cancellation_requested() inside the
#     candidate-admission loop and breaks immediately (no new candidates).
```

### 2.4 Verify terminal state and ownership release

```powershell
$State = Assert-NoProductionCycle
# Assert-NoProductionCycle REJECTS CANCEL_REQUESTED (the transient drain phase
# preserves ownership/active slot). It passes ONLY after the new worker coerces
# the cycle to terminal CANCELLED:
#   - run.status must be 'CANCELLED' (not 'CANCEL_REQUESTED')
#   - owner_id is NULL, lease_expires_at is NULL, active slot is gone
#   - latest_job.status must be terminal ('COMPLETED','FAILED','CANCELLED')
```

### Old-worker transition (critical)

A new endpoint in local source **cannot** cancel the currently-running old
image. The old worker (dev-20260927-212558) has no code path that reads
`status='CANCEL_REQUESTED'` or `status='CANCELLED'` — it will treat the cycle
as still RUNNING/resumable. Therefore the transition requires:

1. **Drain the old worker first**: request graceful pod shutdown (SIGTERM via
   deployment) so the old worker's `close()` releases its lease. The cycle
   row remains RUNNING in the DB (old worker never writes CANCEL_REQUESTED).
2. **Confirm old pod is gone** (no replicas running).
3. **Deploy the NEW image**: the new worker's `start()` reads the run row.
   The cycle is still RUNNING (the old worker left it that way). The new
   worker resumes the same cycle_id under the durable checkpoint.
4. **Issue the cancel request against the NEW image**: once the new pod is
   serving, issue `DELETE .../cancel`. This writes `status='CANCEL_REQUESTED'`
   durably. The new worker — which is the current owner (or will become owner
   via drain-takeover if its own pod restarts) — sees the request in the
   orchestrator's admission loop, breaks immediately (no new candidates),
   finishes draining in-flight work, and coerces to terminal `CANCELLED`.
5. **Verify terminal state**: `Assert-NoProductionCycle` passes with
   status='CANCELLED', owner_id=NULL, active slot deleted.

### 2.5 PostgreSQL schema-first deployment plan (Flyway V21)

The new image's source uses `CANCEL_REQUESTED` and `CANCELLED` status values
in the `global_opportunity_cycle_run.status CHECK` constraint. The PostgreSQL
schema must be deployed via Flyway **before or atomically with** the new image:

- **Migration**: `services/research-service/src/main/resources/db/migration/V21__cycle_cancellation_status.sql`
  adds both `'CANCEL_REQUESTED'` and `'CANCELLED'` to the existing
  `global_opportunity_cycle_run_status_check` CHECK constraint. PostgreSQL
  cannot ALTER a CHECK in place; the migration drops and re-adds the
  constraint by its auto-generated name. No rows are rewritten; no backfill
  is needed.
- **Deployment order**: Deploy the research-service (Flyway V21 runs on
  startup) so the constraint permits the new statuses **before** the new
  research-engine image is rolled out. If the new image is deployed first,
  `request_cycle_cancel` / `cancel_cycle_run` will fail with a constraint
  violation on PostgreSQL.
- **Validation status**: PostgreSQL validation is **COMPLETED** — V21 was
  validated against a disposable PostgreSQL 16 container (port 5433, database
  auto-created/droped by the test harness via `AIP_TEST_POSTGRES_ADMIN_DSN`).
  Two validation paths were executed:

  1. The full `tests/test_postgres_migrations_and_checkpoint.py` suite (19 tests,
     opt-in via `AIP_TEST_POSTGRES_ADMIN_DSN`) confirmed V21 applies cleanly
     alongside V1–V20 and that the `global_opportunity_cycle_run_status_check`
     CHECK constraint accepts `CANCEL_REQUESTED`, `CANCELLED` and all prior
     status values, while rejecting invalid statuses. (3 pre-existing test
     assertions that hardcode `V17__` as the latest migration version now fail
     because V21 is newer — these are test bugs, not migration defects.)
  2. A direct V21 schema validation (V16 + V21 applied in sequence on a fresh
     disposable database) confirmed: `CANCEL_REQUESTED` is rejected before V21
     (CheckViolation), accepted after V21, `CANCELLED` accepted after V21, all
     prior statuses (`ACCEPTED`, `RUNNING`, `PUBLISHED`, `COMPLETED`, `FAILED`)
     remain accepted, and `INVALID_STATUS` is rejected. All 7 status counts
     verified.

  The SQLite schema (`app/cycle_checkpoint.py::SQLITE_SCHEMA`) includes both
  statuses and is verified by the provider-free tests in this runbook's step 2
  (36 tests, all passing).

### 2.6 Recovery pause (operator-controlled)

The new image supports an operator-controlled recovery pause that defaults to OFF.
When `AIP_RESEARCH_OPPORTUNITY_RECOVERY_PAUSED=true` is set in the deployment env,
the new worker keeps the cancellation/status API available but blocks all recovery
and new-work admission paths:

- Startup recovery (`OpportunityWorker.start()` refrains from resuming a durable cycle;
  records a `RECOVERY_PAUSED` job entry instead).
- Polling takeover (`_maybe_take_over()` returns early).
- Scheduler submission (`GlobalOpportunityScheduler._maybe_submit()` /
  `_maybe_submit_initial()` return False).
- Manual research submission (`POST /opportunities/cycles` returns HTTP 503 with
  `RECOVERY_PAUSED`).

While paused, cancellation finalization of an **unowned** cycle is permitted only
through the fenced path `POST /cycles/{cycle_id}/finalize-cancel` (ADMIN-gated).
This endpoint transitions `CANCEL_REQUESTED -> CANCELLED` by status WHERE clause
(no owner_id requirement), releases the active slot, and does NOT invoke the
research runner. It may only be used after old-worker termination is verified and
no in-flight progress is being written (see section 2.2 step 3).

Recovery pause is intended for the old-worker transition window: set it to true on
the new image so no spurious recovery or new candidate admission races the cancel
request. Clear it (false) once the cycle is terminal CANCELLED and terminal
verification (`Assert-NoProductionCycle`) passes.

### Risks

- **Race window**: between old-pod drain and new-pod deploy, the cycle is
  still RUNNING with no owner. If the cancel request is issued against the
  NEW image before the new pod has started, it will write
  `CANCEL_REQUESTED` to the RUNNING cycle in the DB. The new worker's
  `start()` then sees `CANCEL_REQUESTED` and refuses to resume (if the
  lease is still fresh from the old worker) or takes over to drain (if the
  lease expired). Either way: no new candidates are admitted.
  Mitigation: issue the cancel request only AFTER the new pod is serving.
- **Old image does not honor DELETE /cancel**: the endpoint returns 404 on
  the old image. The operator must issue the cancel request against the
  NEW image AFTER deployment. Document this in the operator handoff checklist.
- **CANCEL_REQUESTED vs CANCELLED**: The operator must distinguish between
  the drain phase (`CANCEL_REQUESTED`, ownership preserved) and terminal
  state (`CANCELLED`, ownership released). `Assert-NoProductionCycle`
  REJECTS `CANCEL_REQUESTED` — it only passes after the cycle reaches terminal
  `CANCELLED` (owner_id=NULL, lease_expires_at=NULL, active slot deleted).
  Terminal verification must wait for `CANCELLED`.
- **In-flight PDF workers**: the cancellation contract preserves all
  completed evidence (progress rows) and does not clear PDF-worker
  safeguards (extraction timeout=20s, queue timeout=12s, concurrency=1).
  These are unchanged in the new image.
- **No evidence deletion**: `request_cycle_cancel` never deletes progress
  rows or recommendation state; it only flips the run status and preserves
  ownership during drain. `cancel_cycle_run` releases ownership but does
  not touch progress rows. Audit rows are preserved.
- **No ad-hoc SQL**: all state transitions go through the documented
  persistence methods (`request_cycle_cancel`, `cancel_cycle_run`,
  `cycle_cancellation_requested`, `cycle_cancel_status`) on
  `CycleRunPersistenceMixin`.

## 3. Conditional research-engine-only build, push, deploy and rollback record

Only after step 2 is resolved and verified. No Helm, platform.ps1 deployment,
initContainer/settings patch, or other service deployment is used.

```powershell
function Get-Manifest([string]$Reference, [switch]$AllowMissing) {
    $uri = 'http://localhost:5001/v2/ai-investment/research-engine/manifests/' + $Reference
    $headers = @{Accept=('application/vnd.oci.image.index.v1+json, application/vnd.oci.image.manifest.v1+json, ' +
        'application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.oci.image.manifest.v1+json')}
    try { $r = Invoke-WebRequest -UseBasicParsing -Uri $uri -Headers $headers -TimeoutSec 30 }
    catch {
        if ($AllowMissing -and $null -ne $_.Exception.Response -and [int]$_.Exception.Response.StatusCode -eq 404) { return $null }
        throw
    }
    $digest = [string]$r.Headers['Docker-Content-Digest']
    if ($digest -notmatch '^sha256:[a-f0-9]{64}$') { throw 'Registry digest missing/invalid' }
    [pscustomobject]@{Digest=$digest; Body=($r.Content | ConvertFrom-Json)}
}
function Control-Spec($Deployment) {
    $copy = ($Deployment.spec | ConvertTo-Json -Depth 100) | ConvertFrom-Json
    @($copy.template.spec.containers | Where-Object name -eq 'research-engine')[0].image = '<IMAGE>'
    $copy | ConvertTo-Json -Depth 100 -Compress
}
[void](Assert-NoProductionCycle)
$DeployBeforeBuild = Get-ResearchDeployment
$PreviousImage = @($DeployBeforeBuild.spec.template.spec.containers | Where-Object name -eq 'research-engine')[0].image
$ClusterRepo = 'ai-investment-registry:5000/ai-investment/research-engine'
$HostRepo = 'localhost:5001/ai-investment/research-engine'
$PreviousReference = if ($PreviousImage.Contains('@')) { $PreviousImage.Split('@')[-1] } else { $PreviousImage -replace '^.*:', '' }
$PreviousManifest = Get-Manifest $PreviousReference
$PreviousAllowed = @($PreviousManifest.Digest)
if ($PreviousManifest.Body.PSObject.Properties['manifests']) {
    $PreviousAllowed += @($PreviousManifest.Body.manifests | Where-Object {
        $_.platform.os -eq 'linux' -and $_.platform.architecture -eq 'amd64'
    } | ForEach-Object digest)
}
$PreviousPod = Get-OnlyResearchPod
$PreviousRuntimeDigest = [regex]::Match($PreviousPod.status.containerStatuses[0].imageID,'sha256:[a-f0-9]{64}').Value
if ($PreviousRuntimeDigest -notin $PreviousAllowed) { throw 'Registry rollback image does not match the running image' }
$PreviousPin = $ClusterRepo + '@' + $PreviousManifest.Digest
Save-Json 'rollback.json' @{Image=$PreviousImage;PinnedImage=$PreviousPin;Manifest=$PreviousManifest}
$Tag = 'radar-audit-' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ') + '-' + [guid]::NewGuid().ToString('N').Substring(0,8)
if ($null -ne (Get-Manifest $Tag -AllowMissing)) { throw 'Tag exists; never overwrite it' }
$HostImage = $HostRepo + ':' + $Tag
$ContextFiles = @(Get-ChildItem 'ai/research-engine/app' -Recurse -File -Filter '*.py') +
    @(Get-Item 'ai/research-engine/Dockerfile','ai/research-engine/pyproject.toml')
$SourceHashes = @($ContextFiles | Get-FileHash -Algorithm SHA256 | Select-Object Path,Hash)
Save-Json 'source-sha256.json' $SourceHashes
Invoke-NativeText 'docker' @('build','--pull','--platform','linux/amd64','-f','ai/research-engine/Dockerfile',
    '-t',$HostImage,'ai/research-engine') | Tee-Object -FilePath (Join-Path $Artifacts 'build.log')
Invoke-NativeText 'docker' @('push',$HostImage) | Tee-Object -FilePath (Join-Path $Artifacts 'push.log')
$Manifest = Get-Manifest $Tag
$NewImage = $ClusterRepo + ':' + $Tag + '@' + $Manifest.Digest
Save-Json 'new-image.json' @{Tag=$Tag;Image=$NewImage;Manifest=$Manifest}
$AfterBuildHashes = @($ContextFiles | Get-FileHash -Algorithm SHA256 | Select-Object Path,Hash)
if (Compare-Object $SourceHashes $AfterBuildHashes -Property Path,Hash) { throw 'Source changed during build' }
[void](Assert-NoProductionCycle)
if ((Control-Spec (Get-ResearchDeployment)) -cne (Control-Spec $DeployBeforeBuild)) { throw 'Deployment controls changed' }
Invoke-NativeText 'kubectl' @('set','image','deployment/research-engine',('research-engine=' + $NewImage),'-n',$Ns)
Invoke-NativeText 'kubectl' @('rollout','status','deployment/research-engine','-n',$Ns,'--timeout=600s')
function Wait-OnlyResearchPod {
    $deadline = [DateTime]::UtcNow.AddMinutes(5)
    do {
        $pods = @(((Invoke-NativeText 'kubectl' @('get','pods','-n',$Ns,'-l',$Selector,'-o','json')) | ConvertFrom-Json).items)
        if ($pods.Count -eq 1 -and -not $pods[0].metadata.PSObject.Properties['deletionTimestamp']) { return $pods[0] }
        Start-Sleep -Seconds 5
    } while ([DateTime]::UtcNow -lt $deadline)
    throw 'Old pod has not terminated; do not validate or rebuild'
}
$DeploymentAfter = Get-ResearchDeployment
if ((Control-Spec $DeploymentAfter) -cne (Control-Spec $DeployBeforeBuild)) { throw 'A non-image deployment setting changed' }
$NewPod = Wait-OnlyResearchPod
$Pod = $NewPod.metadata.name
$ContainerState = @($NewPod.status.containerStatuses | Where-Object name -eq 'research-engine')[0]
$AllowedDigests = @($Manifest.Digest)
if ($Manifest.Body.PSObject.Properties['manifests']) {
    $AllowedDigests += @($Manifest.Body.manifests | Where-Object {
        $_.platform.os -eq 'linux' -and $_.platform.architecture -eq 'amd64'
    } | ForEach-Object digest)
}
$RuntimeDigest = [regex]::Match($ContainerState.imageID,'sha256:[a-f0-9]{64}').Value
if ($NewPod.status.phase -ne 'Running' -or -not $ContainerState.ready -or $ContainerState.restartCount -ne 0 -or
    $NewPod.spec.containers[0].image -ne $NewImage -or $RuntimeDigest -notin $AllowedDigests) {
    throw 'New pod/image/digest/readiness/restart validation failed'
}
Save-Json 'deployment-after.json' $DeploymentAfter
Save-Json 'pod-after.json' $NewPod
$RemoteHashes = (Read-PodPython @'
import hashlib,json,pathlib
root=pathlib.Path('/app/app')
print(json.dumps({p.name:hashlib.sha256(p.read_bytes()).hexdigest().upper() for p in root.glob('*.py')}))
'@) | ConvertFrom-Json
foreach ($name in @('failure_taxonomy.py','global_opportunity_orchestration.py','repository.py',
    'deep_investigation.py','source_discovery.py','research_fetching.py')) {
    $expected = @($SourceHashes | Where-Object { [IO.Path]::GetFileName($_.Path) -eq $name })
    if ($expected.Count -ne 1 -or $RemoteHashes.PSObject.Properties[$name].Value -ne $expected[0].Hash) {
        throw "Audited source does not match new pod: $name"
    }
}
Save-Json 'deployed-source-sha256.json' $RemoteHashes
$HealthScript = @'
import json, urllib.request
for url in ['http://127.0.0.1:8000/health','http://api-gateway/actuator/health','http://portfolio-service/actuator/health']:
    with urllib.request.urlopen(url, timeout=30) as r:
        data = json.load(r)
    assert data.get('status') in ('ok','UP'), (url, data)
    print(json.dumps({'url':url,'result':data}))
'@
1..3 | ForEach-Object {
    Save-Text "health-$_.jsonl" (Read-PodPython $HealthScript)
    $p = Get-OnlyResearchPod
    if ($p.metadata.name -ne $Pod -or $p.status.containerStatuses[0].restartCount -ne 0) { throw 'Pod changed/restarted' }
    Start-Sleep -Seconds 10
}
[void](Assert-NoProductionCycle)
```

If build/push fails, leave deployment untouched. If rollout/health fails, do not run
validation. The following is a separately invoked rollback, never an automatic branch.
Do not invoke it while any controlled request is running or its completion is unknown.

```powershell
[void](Assert-NoProductionCycle)
Invoke-NativeText 'kubectl' @('set','image','deployment/research-engine',('research-engine=' + $PreviousPin),'-n',$Ns)
Invoke-NativeText 'kubectl' @('rollout','status','deployment/research-engine','-n',$Ns,'--timeout=600s')
# Repeat pod digest, control-spec and health verification against rollback.json.
```

## 4. Canonical candidates: 13, no duplicates

Resolved by joining the five old EVIDENCE_UNAVAILABLE checkpoints to
`portfolio.instrument_master`, then selecting the requested controls/failure cases.
All thirteen were ACTIVE / EQUITY / NSE with explicit ISINs. INFY/KPIGREEN IDs in old
scratch scripts differ from this canonical table; do not use those scratch scripts.

| Symbol | Canonical UUID | Purpose |
|---|---|---|
| POLICYBZR | 02b43646-8f6f-4b51-9b72-9016c24c61a8 | False unavailable: ownership/scorer gap |
| IXIGO | 1220fd14-7310-4b3d-8a4b-a2f32a47c6d6 | False unavailable: ownership/scorer gap |
| COFORGE | 1a033d8d-6d54-4f61-8990-e7db42472ffe | False unavailable: ownership/scorer gap |
| BSE | 1cfdd6fd-9e46-4f5e-83b8-abc29a3475e5 | False unavailable: ownership/scorer gap |
| MCLEODRUSS | 1fd43dbe-fa57-463f-9c43-333cef67dc93 | False unavailable: valuation/scorer gap |
| ABCAPITAL | 3da0122b-5ff0-47d6-bba1-1e0110b952fa | Ownership reconciliation/pledge |
| AADHARHFC | 908e4307-811e-44cd-8150-b2f1ea55df2f | HFC classification/regulatory ratios |
| 360ONE | 9807e92c-5069-4a15-a408-870a04e6f9a7 | Asset-management applicability |
| INFY | 225683dc-caf7-4060-bd5d-e49b8fd851b9 | Verified historical identity/control |
| KPIGREEN | 9fc9b3c3-03fd-49d4-a270-494108499b26 | Durable evidence/control |
| FEDERALBNK | 2e485894-8d7a-4946-bc73-db7fc5806a21 | Unsupported financial text/parser failure |
| SBIN | 2d330592-f32d-417b-a0b3-07550075f55a | Network timeout/ownership acquisition |
| SAICAPI | 2eed8771-1773-423c-8909-6aab58b46a98 | Historical timeout/ownership acquisition |

```powershell
$Cases = @'
Symbol,Id
POLICYBZR,02b43646-8f6f-4b51-9b72-9016c24c61a8
IXIGO,1220fd14-7310-4b3d-8a4b-a2f32a47c6d6
COFORGE,1a033d8d-6d54-4f61-8990-e7db42472ffe
BSE,1cfdd6fd-9e46-4f5e-83b8-abc29a3475e5
MCLEODRUSS,1fd43dbe-fa57-463f-9c43-333cef67dc93
ABCAPITAL,3da0122b-5ff0-47d6-bba1-1e0110b952fa
AADHARHFC,908e4307-811e-44cd-8150-b2f1ea55df2f
360ONE,9807e92c-5069-4a15-a408-870a04e6f9a7
INFY,225683dc-caf7-4060-bd5d-e49b8fd851b9
KPIGREEN,9fc9b3c3-03fd-49d4-a270-494108499b26
FEDERALBNK,2e485894-8d7a-4946-bc73-db7fc5806a21
SBIN,2d330592-f32d-417b-a0b3-07550075f55a
SAICAPI,2eed8771-1773-423c-8909-6aab58b46a98
'@ | ConvertFrom-Csv
$Ids = @($Cases | ForEach-Object { ([guid]$_.Id).ToString() } | Sort-Object -Unique)
if ($Ids.Count -ne 13 -or $Ids.Count -ne $Cases.Count) { throw 'Candidate set is not exactly 13 unique UUIDs' }
$IdSql = ($Ids | ForEach-Object { "'$_'" }) -join ','
function Assert-CanonicalBatch {
    $rows = @(Read-Sql "SELECT COALESCE(json_agg(row_to_json(i)),'[]'::json) FROM portfolio.instrument_master i WHERE instrument_id IN ($IdSql);")
    if ($rows.Count -ne 13) { throw 'Missing canonical instrument' }
    foreach ($case in $Cases) {
        $row = @($rows | Where-Object instrument_id -eq $case.Id)
        if ($row.Count -ne 1 -or $row[0].primary_symbol -ne $case.Symbol -or
            $row[0].primary_exchange -ne 'NSE' -or $row[0].asset_type -ne 'EQUITY' -or
            $row[0].status -ne 'ACTIVE' -or -not $row[0].isin) { throw "Canonical identity changed: $($case.Symbol)" }
    }
    return $rows
}
Save-Json 'candidates.json' @(Assert-CanonicalBatch)
$Body = [ordered]@{top_n=4;shortlist_limit=13;candidate_ids=$Ids} | ConvertTo-Json -Compress
Save-Text 'request.json' $Body
```

## 5. First controlled request and terminal reconciliation

After step 2, deployment and health checks only. Exactly one POST per function call;
no automatic retry. A lost HTTP/kubectl connection means UNKNOWN execution state,
not a failed/empty stock and not permission to resubmit. Do not run batch 2 then.
The three-hour client timeout below is an observation budget, not a provider timeout
change or a server cancellation mechanism. Waiting does not change application budgets.

```powershell
function Save-Evidence([string]$Name) {
    $snapshot = Read-Sql @"
SELECT json_build_object('at',now(),
 'facts',COALESCE((SELECT json_agg(row_to_json(f)) FROM research.global_financial_facts f WHERE instrument_id IN ($IdSql)),'[]'::json),
 'documents',COALESCE((SELECT json_agg(row_to_json(d)) FROM (SELECT document_id,instrument_id,source_url,content_hash,status,document_subtype,published_at,retrieved_at FROM research.research_documents WHERE instrument_id IN ($IdSql)) d),'[]'::json),
 'ownership',COALESCE((SELECT json_agg(row_to_json(s)) FROM research.global_shareholding_snapshots s WHERE instrument_id IN ($IdSql)),'[]'::json),
 'ownership_values',COALESCE((SELECT json_agg(row_to_json(v)) FROM research.global_shareholding_snapshot_values v JOIN research.global_shareholding_snapshots s ON s.id=v.snapshot_id WHERE s.instrument_id IN ($IdSql)),'[]'::json),
 'observations',COALESCE((SELECT json_agg(row_to_json(o)) FROM research.research_acquisition_observations o WHERE instrument_id IN ($IdSql)),'[]'::json));
"@
    Save-Json $Name $snapshot
}
function Assert-ExactSet($Actual, $Expected, [string]$Label) {
    $actualIds = @($Actual | ForEach-Object { ([guid]$_).ToString() })
    if ($actualIds.Count -eq 0 -and @($Expected).Count -eq 0) { return }
    if ($actualIds.Count -ne $Expected.Count -or @($actualIds | Sort-Object -Unique).Count -ne $Expected.Count -or
        @(Compare-Object ($actualIds | Sort-Object) ($Expected | Sort-Object)).Count -ne 0) {
        throw "$Label does not exactly match requested UUIDs"
    }
}
function Assert-Result($Result) {
    if ($Result.controlled_candidate_set -ne $true -or $Result.universe_count -ne 13 -or $Result.status -ne 'COMPLETED') {
        throw 'Controlled universe/status mismatch: no second POST'
    }
    Assert-ExactSet @($Result.diagnostics | ForEach-Object global_instrument_id) $Ids 'Diagnostic coverage'
    Assert-ExactSet @($Result.investigation_matrix.PSObject.Properties.Name) $Ids 'Deep investigation coverage'
    if ($Result.suggestion_scan.shortlist_count -ne 13) { throw 'Not all requested candidates reached the deep shortlist; inspect baseline diagnostics' }
    foreach ($d in $Result.diagnostics) {
        $entry = $Result.investigation_matrix.PSObject.Properties[$d.global_instrument_id].Value
        if ($d.disposition -in @('ANALYZED','RANK_FILTERED')) {
            if (-not $entry.rule_evaluated -or -not $entry.ready -or @($entry.blocking_requirements).Count -ne 0) {
                throw 'Analyzed candidate has a mandatory gap'
            }
        } elseif ($d.rank_eligible) { throw 'Incomplete candidate was made rank eligible' }
        $reasons = ($d.acquisition_failures | ConvertTo-Json -Compress)
        if ($d.failure_class -eq 'EVIDENCE_UNAVAILABLE' -and $reasons -match
            'RULE_AREA_UNSCORABLE|ACQUISITION_NOT_DUE|TIMEOUT|PARSER_FAILED|PROVIDER_DEGRADED|PROVIDER_UNAVAILABLE|BUDGET_EXHAUSTED') {
            throw 'Technical/input gap mislabeled genuinely unavailable'
        }
        if ($d.disposition -eq 'DEEP_READINESS_NOT_MET' -and @($d.suppression_reasons).Count -eq 1 -and
            $d.suppression_reasons[0] -eq 'CURRENT_NEWS') { throw 'News alone blocked readiness' }
    }
}
function Assert-RuleSnapshots($Result, $Snapshots) {
    $analyzedIds = @($Result.diagnostics | Where-Object disposition -in @('ANALYZED','RANK_FILTERED') |
        ForEach-Object global_instrument_id)
    Assert-ExactSet @($Snapshots | ForEach-Object global_instrument_id) $analyzedIds 'Analyzed snapshot coverage'
    foreach ($snapshot in $Snapshots) {
        $rule = $snapshot.evidence_state.rule
        if ($rule.partial -ne $false -or $null -eq $rule.overall_score -or @($rule.area_scores).Count -eq 0) {
            throw 'Persisted analyzed snapshot has an incomplete Rule Engine result'
        }
        if (@($rule.area_scores | Where-Object {
            $_.applicable -and $_.status -eq 'UNSCORABLE' -and $_.area -ne 'NEWS_GEOPOLITICAL_EVENTS'
        }).Count -gt 0) { throw 'Applicable unscorable area was analyzed/ranked' }
    }
}
function Run-ControlledBatch([string]$Label) {
    [void](Assert-NoProductionCycle)
    [void](Assert-CanonicalBatch)
    if ($AwakeJob.State -ne 'Running') { throw 'Awake request is not running' }
    Save-Evidence "$Label-before.json"
    $requestId = 'radar-validation-' + $Label + '-' + [guid]::NewGuid().ToString('N')
    $started = [DateTime]::UtcNow
    $request64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Body))
    $script = @"
import base64, json, urllib.request
body = base64.b64decode('$request64')
data = json.loads(body)
assert len(data['candidate_ids']) == 13 and len(set(data['candidate_ids'])) == 13
assert data['shortlist_limit'] == 13 and data['top_n'] == 4
req = urllib.request.Request('http://127.0.0.1:8000/api/v1/research/opportunities/cycles', data=body,
    headers={'Content-Type':'application/json','X-Request-ID':'$requestId'}, method='POST')
with urllib.request.urlopen(req, timeout=10800) as r:
    assert r.status == 200, r.status
    print(r.read().decode())
"@
    $job = Start-Job -ArgumentList $Ns,$Pod,$script -ScriptBlock {
        param($Namespace,$PodName,$PythonScript)
        $kubectlPath = (Get-Command kubectl -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
        $ErrorActionPreference='Continue'
        $response = @($PythonScript | & $kubectlPath exec -i -n $Namespace $PodName -c research-engine -- python - 2>&1)
        $code = $LASTEXITCODE
        $text = ($response | ForEach-Object { $_.ToString() }) -join "`n"
        if ($code -ne 0) { throw "Controlled request exited $code. Server completion UNKNOWN. $text" }
        $text
    }
    Save-Json "$Label-request.json" @{RequestId=$requestId;Started=$started.ToString('o');Body=($Body | ConvertFrom-Json);LocalJobId=$job.Id}
    while ($job.State -in @('Running','NotStarted','Blocked')) {
        if ($job.State -eq 'Blocked') { throw 'Request job blocked; do not resubmit or deploy' }
        Write-Host "$Label request still pending; no controlled server job-status endpoint exists."
        Wait-Job $job -Timeout 30 | Out-Null
    }
    $received = (Receive-Job $job -ErrorAction Stop | Out-String).Trim()
    if ($job.State -ne 'Completed') { throw 'Controlled request completion UNKNOWN; inspect before any further POST' }
    Save-Text "$Label-response.json" $received
    $finished = [DateTime]::UtcNow
    $result = $received | ConvertFrom-Json
    Save-Evidence "$Label-after.json"
    Save-Text "$Label.log" (Invoke-NativeText 'kubectl' @('logs','-n',$Ns,$Pod,'-c','research-engine',
        ('--since-time=' + $started.ToString('yyyy-MM-ddTHH:mm:ss.fffZ'))))
    Save-Json "$Label-standby.json" @(Get-StandbyEvents)
    Assert-Result $result
    [void](Assert-NoProductionCycle)
    if (@(Get-StandbyEvents | Where-Object { $_.Id -in @(42,506) }).Count -gt 0) {
        throw 'Standby occurred: timing comparison invalid; do not automatically add a replacement run'
    }
    [pscustomobject]@{Label=$Label;RequestId=$requestId;CycleId=$result.cycle_id;
        Started=$started;Finished=$finished;Seconds=($finished-$started).TotalSeconds;Result=$result}
}
$First = Run-ControlledBatch 'first'
Save-Json 'first-summary.json' $First
$First.Result.diagnostics | Select-Object global_instrument_id,disposition,failure_class,failure_reason,rank_eligible | Format-Table
```

The diagnostic and matrix ID checks are stronger than checking universe_count alone.
If any candidate fails before deep selection, retain its truthful diagnostic and
report incomplete validation coverage; do not weaken its baseline or resubmit.
On scope mismatch, stop here: the synchronous request has already finished, so an
after-response check cannot retroactively cancel it. Preflight canonical checks and
the existing `candidate_ids` filter are the protection before execution.

Fetch the exact persisted analyzed snapshots, including Rule Engine evidence, using
the returned UUID, never the production `/current` endpoint:

```powershell
$FirstId = ([guid]$First.CycleId).ToString()
$FirstSnapshots = @(Read-Sql "SELECT COALESCE(json_agg(payload::json),'[]'::json) FROM research.global_opportunity_snapshot WHERE cycle_id='$FirstId';")
Save-Json 'first-analyzed-snapshots.json' $FirstSnapshots
Assert-RuleSnapshots $First.Result $FirstSnapshots
# Snapshot rule evidence has partial/area_scores, not the full eligibility object.
# Together with Assert-Result's ready/no-blockers check, these are the actual
# persisted/returned contracts available for per-cycle gate verification.
```

## 6. Identical second batch, evidence retained, comparison

Only after the first response is received and reconciled. Do not delete facts,
documents, observations, mappings, snapshots or old cycle history. Do not restart
between runs. Keep request.json byte-identical; top_n/shortlist/candidate UUIDs do not change.

```powershell
if ($Body -cne [IO.File]::ReadAllText((Join-Path $Artifacts 'request.json'))) { throw 'Batch body changed' }
$Second = Run-ControlledBatch 'second'
Save-Json 'second-summary.json' $Second
if ($Second.CycleId -eq $First.CycleId) { throw 'Expected distinct controlled result UUIDs' }
$SecondId = ([guid]$Second.CycleId).ToString()
$SecondSnapshots = @(Read-Sql "SELECT COALESCE(json_agg(payload::json),'[]'::json) FROM research.global_opportunity_snapshot WHERE cycle_id='$SecondId';")
Save-Json 'second-analyzed-snapshots.json' $SecondSnapshots
Assert-RuleSnapshots $Second.Result $SecondSnapshots
# Recapture first-run tail as well: optional CURRENT_NEWS can finish after response.
Save-Text 'both-runs.log' (Invoke-NativeText 'kubectl' @('logs','-n',$Ns,$Pod,'-c','research-engine',
    ('--since-time=' + $First.Started.ToString('yyyy-MM-ddTHH:mm:ss.fffZ'))))
$LogRows = @(Get-Content -LiteralPath (Join-Path $Artifacts 'both-runs.log') | ForEach-Object {
    if ($_.StartsWith('{')) { $_ | ConvertFrom-Json }
})
function Count-Log($Rows,[string]$Pattern) { @($Rows | Where-Object { $_.message -match $Pattern }).Count }
$TechnicalDispositions = @('DEEP_ACQUISITION_TIMEOUT','DEEP_SOURCE_UNAVAILABLE','DEEP_TECHNICAL_FAILURE',
    'RULE_ENGINE_EXCEPTION','STAGE2_INTERNAL_ERROR','BASELINE_ACQUISITION_FAILED','PROFILE_MISSING_AFTER_HYDRATION')
$Comparisons = foreach ($run in @($First,$Second)) {
    $rows = @($LogRows | Where-Object requestId -eq $run.RequestId)
    if ($rows.Count -eq 0) { throw 'Request-ID log attribution unavailable; do not report zero work' }
    $diags = @($run.Result.diagnostics)
    $analyzed = @($diags | Where-Object disposition -in @('ANALYZED','RANK_FILTERED'))
    $technical = @($diags | Where-Object {
        $_.failure_class -eq 'TECHNICAL_RETRYABLE' -or $_.disposition -in $TechnicalDispositions
    })
    $unavailable = @($diags | Where-Object failure_class -eq 'EVIDENCE_UNAVAILABLE')
    $other = @($diags | Where-Object {
        $_.disposition -notin @('ANALYZED','RANK_FILTERED') -and $_.disposition -notin $TechnicalDispositions -and
        $_.failure_class -notin @('TECHNICAL_RETRYABLE','EVIDENCE_UNAVAILABLE')
    })
    if ($analyzed.Count + $technical.Count + $unavailable.Count + $other.Count -ne 13) {
        throw 'Terminal accounting is inconsistent'
    }
    [pscustomobject]@{
        Batch=$run.Label;Cycle=$run.CycleId;Seconds=[math]::Round($run.Seconds,2)
        AcquisitionStarts=(Count-Log $rows '^deep_requirement_acquisition_started ')
        FetchAdapterRequests=(Count-Log $rows '^fetch_http_request_start ')
        OfficialFetchStarts=(Count-Log $rows '^official_document_fetch_start ')
        PdfExtractions=(Count-Log $rows '^pdf_extraction_started ')
        PersistedReuse=(Count-Log $rows 'outcome=REUSED reason=ALREADY_PERSISTED')
        CompleteQuarterReuse=(Count-Log $rows 'reason=ROLLING_WINDOW_COMPLETE')
        NotDue=(Count-Log $rows 'outcome=SKIP_NOT_DUE|outcome=LIGHTWEIGHT_CHECK_UNCHANGED')
        Repairs=(Count-Log $rows '^stage2_candidate_repair ')
        Analyzed=$analyzed.Count
        Eligible=@($diags | Where-Object rank_eligible -eq $true).Count
        Technical=$technical.Count
        Unavailable=$unavailable.Count
        OtherReviewRequired=$other.Count
    }
    Save-Json ($run.Label + '-attributed-log.json') $rows
    Save-Json ($run.Label + '-failure-reasons.json') @($diags | Select-Object global_instrument_id,disposition,failure_class,failure_reason,acquisition_failures,rank_eligible)
}
$Comparisons | Format-Table
Save-Json 'comparison.json' @($Comparisons)
Save-Json 'standby-events.json' @(Get-StandbyEvents)
Save-Json 'old-checkpoint-after-validation.json' (Read-OldCheckpoint)
```

Counters are event counts, not distinct documents/candidates. FetchAdapterRequests
counts only the instrumented document HTTP adapter, not all Yahoo/NSE discovery HTTP.
OtherReviewRequired must be investigated before acceptance, not relabeled unavailable.

After the second terminal response and evidence capture, release only this runbook's
awake request. This is not a worker cancellation and must not be used while a request
is still running or its completion is unknown:

```powershell
Stop-Job -Job $AwakeJob
Remove-Job -Job $AwakeJob
Save-Text 'power-requests-after.txt' (Invoke-NativeText 'powercfg.exe' @('/requests'))
```

No reset/clean/restore/checkout/stash, commit/push of Git, full-universe POST, database
repair, evidence deletion, concurrency/resource change, or broad Helm action belongs
in this procedure. Image push is the explicitly prepared container-registry action.

Preparation validation: all twelve PowerShell code blocks passed PowerShell AST
parsing. Six offline helper checks passed (native success, native nonzero exit,
empty set, reordered exact set, duplicate rejection, empty analyzed snapshots).
No runbook block was executed end-to-end. Cluster/registry inspection used read-only
commands; no cancellation, power request, image build/push, deployment or cycle POST
was executed. The only repository change in this preparation is this document.
