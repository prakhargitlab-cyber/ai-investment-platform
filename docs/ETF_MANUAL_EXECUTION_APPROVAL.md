# ETF manual execution — proposed commands, NOT executed

Implementation is local only. Image build/publication, rollout, canonical ETF
provisioning, evidence ingestion and each live cycle require separate approval.
Do not use the commands below until the corresponding approval is granted.

## Admission contract

- `AIP_RESEARCH_ETF_MANUAL_EXECUTION_ENABLED` defaults to `false`. Scope:
  BOUNDED execution only (readiness-validated controlled ETF POSTs, <=5
  candidates). Full-universe execution is **no longer** authorized by this
  flag (see below) -- this closes a gap where enabling bounded execution for
  diagnostics would have silently also authorized a full-universe run.
- `AIP_RESEARCH_ETF_FULL_UNIVERSE_EXECUTION_ENABLED` defaults to `false` and is
  a SEPARATE, independent flag required for `POST .../full-universe-cycles`
  (all official NSE ETFs, including catalog onboarding of any genuinely-missing
  official ETF -- see `NSE_ETF_SINGLE_PROVISIONING.md`'s bulk-apply sibling,
  now also invoked automatically, once per full-universe cycle, from inside
  this same admin-gated boundary). Enabling one of these two flags never
  enables the other.
- Both flags require recovery paused, scheduler disabled and existing ADMIN
  authorization. Ordinary ETF/Equity submissions stay paused.
- Only newly created, process-local ETF cycle IDs may enter the worker. Existing
  active slots block with `ETF_ACTIVE_CYCLE_REQUIRES_REVIEW`; startup and idle
  takeover remain disabled. Concurrent local requests coalesce; another worker
  cannot acquire an existing slot through this admission path.
- Existing durable slot arbitration, leases, cancellation, publication fences,
  provider pacing and budgets are unchanged. A crash/restart leaves an interrupted
  slot for operator review, never automatic recovery or history repair.
- Canonical empty/unavailable/unreconciled universe failures are distinct durable
  errors and cannot overwrite saved recommendations.

## Current blocker

A read-only canonical ETF GET from research-engine using its existing service
identity returned successfully with **zero ETFs**. This verifies connectivity,
not a usable universe. The earlier `UNIVERSE_UNAVAILABLE` is consistent with the
empty-universe branch; the historical upstream response was not recovered.

Do not approve a live cycle until canonical identities and fresh evidence for at
least one selected ETF have been provisioned and reviewed separately. No data
bootstrap, ingestion or migration is included here. No new image exists yet;
the approved new and rollback manifest digests must be supplied after artifact
review. Do not substitute a Docker image ID for a registry manifest digest.

## Proposed scoped rollout (PowerShell 5.1)

This function performs one resource-version-checked patch of only the
research-engine container image and dedicated manual gate, then observes the
rollout. It refuses unexpected recovery/scheduler settings. No Helm upgrade,
replica change, policy, database or PVC operation is included. Both commands
below cause a workload rollout and require approval.

```powershell
function Set-ReviewedEtfImage([string]$Image, [bool]$EnableManual) {
    if ($Image -notmatch '^ai-investment-registry:5000/ai-investment/research-engine@sha256:[0-9a-f]{64}$') {
        throw 'Reviewed immutable research-engine manifest reference required'
    }
    $gate = if ($EnableManual) { 'true' } else { 'false' }
    @'
import json, subprocess, sys
image, gate = sys.argv[1:]
k = ['kubectl', '--context', 'k3d-ai-investment-dev', '-n', 'ai-investment']
d = json.loads(subprocess.check_output(k + ['get', 'deployment', 'research-engine', '-o', 'json']))
containers = d['spec']['template']['spec']['containers']
indices = [i for i,c in enumerate(containers) if c['name'] == 'research-engine']
assert len(indices) == 1, 'Unexpected container identity'
i = indices[0]
env = containers[i]['env']
def literal(name, expected):
    matches = [e for e in env if e['name'] == name]
    assert len(matches) == 1 and matches[0].get('value') == expected and 'valueFrom' not in matches[0], 'Unsafe recovery/scheduler configuration'
literal('AIP_RESEARCH_OPPORTUNITY_RECOVERY_PAUSED', 'true')
literal('AIP_RESEARCH_OPPORTUNITY_SCHEDULER_ENABLED', 'false')
patch = [{'op':'test', 'path':'/metadata/resourceVersion', 'value':d['metadata']['resourceVersion']},
         {'op':'replace', 'path':f'/spec/template/spec/containers/{i}/image', 'value':image}]
name = 'AIP_RESEARCH_ETF_MANUAL_EXECUTION_ENABLED'
matches = [j for j,e in enumerate(env) if e['name'] == name]
assert len(matches) <= 1, 'Duplicate manual gate'
path = f'/spec/template/spec/containers/{i}/env/' + (str(matches[0]) if matches else '-')
patch.append({'op':'replace' if matches else 'add', 'path':path, 'value':{'name':name,'value':gate}})
subprocess.run(k + ['patch', 'deployment', 'research-engine', '--type=json', '-p', json.dumps(patch)], check=True)
'@ | python - $Image $gate
    if ($LASTEXITCODE -ne 0) { throw 'Patch blocked; do not retry automatically' }
    kubectl --context k3d-ai-investment-dev -n ai-investment rollout status deployment/research-engine --timeout=180s
    if ($LASTEXITCODE -ne 0) { throw 'Rollout incomplete; preserve diagnostics; no automatic rollback' }
}

# Set these process variables to separately reviewed registry manifest references.
# Never use placeholders, floating tags or a Docker config/image ID.
Set-ReviewedEtfImage -Image $env:ETF_APPROVED_RESEARCH_IMAGE -EnableManual $true
```

## Proposed first controlled run — one existing ETF, no ingestion

Requires a reviewed canonical instrument UUID in `$env:ETF_APPROVED_INSTRUMENT_ID`
and an existing ADMIN bearer token held in `$env:ETF_ADMIN_TOKEN`. Do not print
the token. This is not the full-universe button: it evaluates fresh, already
persisted evidence for exactly one canonical ETF. Preflight must be READY. A
full NSE acquisition run remains separately authorized after this check.

```powershell
$id = [guid]::Parse($env:ETF_APPROVED_INSTRUMENT_ID).ToString()
if (-not $env:ETF_ADMIN_TOKEN) { throw 'Existing ADMIN session required' }
$headers = @{ Authorization = 'Bearer ' + $env:ETF_ADMIN_TOKEN }
$base = 'http://localhost:18080/api/v1/etf-radar'
$preflight = Invoke-RestMethod -Uri "$base/preflight?instrument_ids=$id" -Headers $headers
if ($preflight.status -ne 'READY' -or $preflight.can_submit -ne $true -or
    $preflight.recovery_paused -ne $true -or $preflight.manual_etf_enabled -ne $true) {
    throw 'Controlled ETF admission blocked; do not ingest or repair automatically'
}
$body = @{ instrument_ids = @($id) } | ConvertTo-Json -Compress
$run = Invoke-RestMethod -Method Post -Uri "$base/controlled-cycles" -Headers $headers -ContentType 'application/json' -Body $body
$cycleId = [guid]::Parse($run.cycle_id).ToString()
$run | Select-Object cycle_id,status
# Repeat only this GET to observe progress. Never repeat the POST as a poll.
$status = Invoke-RestMethod -Uri "$base/cycles/$cycleId/status" -Headers $headers
$status | Select-Object cycle_id,status,error_code
# Only after COMPLETED: verify this exact cycle's durable result, not /current.
if ($status.status -eq 'COMPLETED') {
    $result = Invoke-RestMethod -Uri "$base/cycles/$cycleId" -Headers $headers
    if ($result.cycle_id -ne $cycleId -or @($result.candidates).Count -ne 1) { throw 'Unexpected durable coverage' }
    if ($result.candidates[0].global_instrument_id -ne $id -or $result.candidates[0].disposition -ne 'EVALUATED') {
        throw 'Selected ETF was not meaningfully evaluated; review the durable outcome'
    }
    $result | Select-Object cycle_id,universe_count
}
```

Completion requires actual candidate evaluation and truthful readiness/outcomes;
202 alone or empty results are insufficient. Stop on FAILED or CANCELLED and
retain the previously saved results. No recommendation is guaranteed by a run.

## Rollback proposal — requires approval, never automatic

Disable the manual gate and restore the reviewed previous image in one patch:

```powershell
Set-ReviewedEtfImage -Image $env:ETF_APPROVED_ROLLBACK_IMAGE -EnableManual $false
```

Keep recovery paused and scheduler disabled. Prefer rollback while idle. A
rollout during a manual run interrupts it and may leave an active durable slot;
preserve it for explicit operator review. Do not clear slots, repair history,
unpause recovery or delete data as rollback. Previously persisted results remain.
