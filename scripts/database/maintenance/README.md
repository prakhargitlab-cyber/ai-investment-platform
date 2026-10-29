# Research maintenance — review only / NO-GO

Nothing here is authorization to execute. Each mutation phase needs a separate
recorded approval. Placeholders are mandatory reviewed inputs, never defaults.
Use a NEW access-controlled evidence directory; never overwrite prior evidence.
Check `$LASTEXITCODE` after EVERY native command and stop on failure.

For the single unresolved refresh `ea48b845-e2a3-453b-8225-80a2a362d995`, use
[SINGLE_REFRESH_RUNBOOK.md](SINGLE_REFRESH_RUNBOOK.md) and the separate
`reconcile_single_refresh.sql`. The six-record SQL and capture procedure below
are unchanged. Old approvals and six-record preimages do not authorize the
single-record procedure. A GO for local package review is not permission to run it.

## 1. Fixed-input artifact preparation (build/publication approval)

The existing Dockerfile uses a floating Python base, Python dependency ranges and
network apt repositories. It is NOT reproducible as written. Before building:

1. Freeze a new staging directory outside both worktrees containing `app/` (no
   bytecode) and `pyproject.toml`. Record every file's SHA-256 and the original
   Dockerfile. Include `main.py`, `opportunity_worker.py` and
   `global_opportunity_scheduler.py`. Reject drift from the tested source.
2. Supply an approved, locally available linux/amd64 Python 3.12 base digest;
   exact transitive `requirements.lock` with hashes (including setuptools/wheel)
   and a matching wheelhouse; and the complete compatible .deb closure for
   tesseract-ocr, tesseract-ocr-eng and poppler-utils with reviewed hashes.
3. Pin the toolchain/platform and use the following staging-only Dockerfile.
   Dependency acquisition/locking is separate work; these inputs are NOT supplied.

Minimum immutable inputs are the source tree/pyproject, base digest, complete
Python wheel closure (including pip/build tools), complete OS package closure,
their SHA-256 inventory and the staging Dockerfile/toolchain settings. Do not use
host `pip freeze` as the linux/amd64 lock. Resolve wheels for Python 3.12/linux
amd64, not Windows. No new resolution is allowed during the approved offline build.
The original Dockerfile stays unchanged. There is no verified new artifact yet.

Before an approved build, validate every supplied wheel/deb against the approved
inventory with `Get-FileHash -Algorithm SHA256`; require exact file-set equality,
not merely that all listed files exist. Use this function to bind staged and
copied-image source (no file creation or image startup by this function):

```powershell
function SourceInventory([string]$Root) {
  $Resolved = (Resolve-Path -LiteralPath $Root).Path.TrimEnd('\')
  Get-ChildItem -LiteralPath $Resolved -Recurse -File |
    Where-Object { $_.FullName -notmatch '[\\/]__pycache__[\\/]' -and $_.Extension -ne '.pyc' } |
    ForEach-Object {
      [pscustomobject]@{
        path = $_.FullName.Substring($Resolved.Length + 1).Replace('\','/')
        sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash
      }
    } | Sort-Object path
}
$ExpectedSource = @(SourceInventory "$Stage\app")
# Save this inventory to the NEW evidence directory before building.
```

```dockerfile
ARG BASE
FROM ${BASE}
WORKDIR /app
COPY debs/ /opt/debs/
RUN dpkg -i /opt/debs/*.deb
COPY wheelhouse/ /opt/wheelhouse/
COPY requirements.lock /opt/requirements.lock
RUN pip install --no-index --find-links=/opt/wheelhouse --require-hashes -r /opt/requirements.lock
COPY pyproject.toml .
COPY app/ ./app/
RUN pip install --no-index --no-deps --no-build-isolation .
RUN groupadd --gid 10001 appuser && useradd --create-home --uid 10001 --gid 10001 appuser
USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

```powershell
# Approved build only, no network dependency resolution:
docker build --platform linux/amd64 --network none --pull=false `
  --build-arg "BASE=$ApprovedBaseDigest" --iidfile "$Stage\image-id.txt" `
  -t "localhost:5001/ai-investment/research-engine:$Tag" $Stage
docker image inspect "localhost:5001/ai-investment/research-engine:$Tag"
# Create but NEVER start inspection container:
docker create --name $InspectionName --network none --entrypoint /bin/false `
  "localhost:5001/ai-investment/research-engine:$Tag"
docker cp "${InspectionName}:/app/app" "$Evidence\image-app"
docker cp "${InspectionName}:/app/pyproject.toml" "$Evidence\image-pyproject.toml"
```

Compare complete copied source inventories to staged inputs, ignoring generated
bytecode only. Inspect UID 10001, CMD and absence of unexpected ENTRYPOINT,
HEALTHCHECK/ONBUILD instructions. Test image-specific paused startup in isolation
without production credentials. Preserve evidence/inspection container; no cleanup.
Fixed inputs do not guarantee byte-identical images across timestamps/toolchains.
Pin the digest actually produced, not a predicted digest or Git commit.

```powershell
$ActualSource = @(SourceInventory "$Evidence\image-app")
if (@(Compare-Object $ExpectedSource $ActualSource -Property path,sha256).Count) {
  throw 'Artifact source differs from reviewed build input'
}
if ((Get-FileHash "$Stage\pyproject.toml").Hash -ne
    (Get-FileHash "$Evidence\image-pyproject.toml").Hash) { throw 'pyproject mismatch' }
```

Retain wheel/deb manifests and installed package inventories (`pip list --format=json`,
`dpkg-query -W`) from separately approved isolated artifact inspection. Resolve the
actual imported `app` location there before accepting source equality; a duplicate
installed package must not supersede the reviewed `/app/app` tree.

```powershell
# Separate publish approval, after verification:
docker push "localhost:5001/ai-investment/research-engine:$Tag"
Set-Location C:\workspace\ai-investment-platform-integration
. .\scripts\database\DeploymentSafety.ps1
$Image = Get-VerifiedRegistryImage research-engine $Tag
# Record immutable $Image plus local image ID and source/test evidence.
```

Local image ID and registry digest differ. Later use the recorded immutable digest.
**Build inputs, build and artifact verification/publication remain pending.**

## 2. Inventory and freeze (approval A)

```powershell
Set-Location C:\workspace\ai-investment-platform-integration
$ProjectRoot = $PWD.Path
$ErrorActionPreference = 'Stop'
function K {
  & kubectl --context k3d-ai-investment-dev -n ai-investment @args
  if ($LASTEXITCODE -ne 0) { throw 'kubectl failed; STOP' }
}
. .\scripts\database\DeploymentSafety.ps1
K get deployment,replicaset,pods -o wide
K get hpa,jobs,cronjobs
K get networkpolicy -o yaml
K get nodes -o wide
K get deployment research-engine -o json | Set-Content "$Evidence\deployment.json" -Encoding UTF8
K get services -o json | Set-Content "$Evidence\services.json" -Encoding UTF8
K get pods -o json | Set-Content "$Evidence\pods.json" -Encoding UTF8
```

Kubernetes reads are read-only; evidence creation requires approval. Record Helm
revision, replicas, every old pod/container/node, secret/configmap resource versions
(not secret contents), external execution locations and all possible recreators.
Freeze external callers, API-server tunnels, GitOps/deployment automation and any
HPA/job/operator. If one exists, obtain its concrete suspension procedure first.
Unknown writer/controller/location means STOP. Keep research-service running for
the readiness-only init check. No Helm upgrades or migrations.

The current policy identity is **research-bootstrap-isolation**. Its selector
must exactly equal the live Research Engine Deployment's complete selector;
using only its component label is not sufficient. Reject existing/additive
policies for separate review; never delete them to make admission pass.

`platform.ps1` is the implementation source of truth. The compatibility loader
above loads its deployment/isolation function definitions without executing the
platform entry point. Construct the reviewed `$Gate` from live deployment, pod,
service and caller identities (see the single-refresh runbook), then, only under
approval A, use the existing helper:

```powershell
Start-DeploymentIsolation -Gate $Gate
```

This helper calls `Assert-DeploymentIsolation -Probe`, which binds
`$script:ActiveIsolationGate` before probing every reviewed caller and both
research Service/pod paths. The readiness control must return HTTP 200. The
current kube-router denied-path implementation requires native exit 1 and
connection-refused evidence **together with** verification of this pod's firewall
chain, terminal ICMP REJECT rule and an increasing reject counter. A refused
connection, exit 1 or timeout alone is not proof. Counter changes are
corroboration, not per-connection attribution; the frozen caller set remains
mandatory. No isolation/deployment implementation is changed by this correction.

## 3. Stop and verify all writers (approval A continued)

```powershell
$OldPods = (K get pods -l app.kubernetes.io/component=research-engine -o json | ConvertFrom-Json).items
$Deployment = K get deployment research-engine -o json | ConvertFrom-Json
$OriginalReplicas = [int]$Deployment.spec.replicas
K scale deployment/research-engine "--current-replicas=$OriginalReplicas" --replicas=0
foreach ($p in $OldPods) { K wait --for=delete "pod/$($p.metadata.name)" --timeout=180s }
K get deployment,replicaset,pods -l app.kubernetes.io/component=research-engine -o wide
# READ ONLY; repeat across EVERY relevant node/container:
docker exec $NodeContainer crictl ps -a --name research-engine
docker exec $NodeContainer crictl inspect $CapturedContainerID
docker exec $NodeContainer ctr -n k8s.io tasks list
```

Require zero replicas, no recreation and independent runtime/process/cgroup proof
that all historical owners and external workers are terminated. Confirm research
database sessions/transactions ended. Missing pod/session/logs is not termination
proof. Unreachable node, unknown history or undrained work means STOP; never
force-delete. Explicit approval must cover interruption of uncommitted memory work.

## 4. Current recoverable backup (approval B)

After termination, before reconciliation, freeze schema changes. Existing backup
uses the SAME exported snapshot for dump and per-table counts. It creates archives
and removes only its own temporary files; this behavior is included in approval B.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\platform.ps1 backup
if ($LASTEXITCODE -ne 0) { throw 'Backup failed' }
# Use the newly printed explicit $BackupId, never latest:
powershell -NoProfile -ExecutionPolicy Bypass -File .\platform.ps1 backup-verify -Backup $BackupId
if ($LASTEXITCODE -ne 0) { throw 'Backup verification failed' }
$BackupDir = Join-Path $ProjectRoot "backups\postgres\$BackupId"
$Manifest = Get-Content "$BackupDir\manifest.json" -Raw | ConvertFrom-Json
$Dump = Join-Path $BackupDir 'database.dump'
$DbManifest = @($Manifest.databases | Where-Object name -eq investment)
if ($DbManifest.Count -ne 1) { throw 'Unexpected manifest' }
if ((Get-FileHash $Dump -Algorithm SHA256).Hash -ne $DbManifest[0].sha256) { throw 'Checksum mismatch' }
# Unique independent verifier; compatible approved already-local PG image:
docker run -d --pull=never --name $Verifier --network none `
  -e POSTGRES_HOST_AUTH_METHOD=trust -e PGDATA=/var/lib/postgresql/maintenance-data `
  --tmpfs /var/lib/postgresql $ApprovedPostgresImage
# Bounded readiness wait; do not continue until ready:
docker exec $Verifier pg_isready -U postgres
docker cp $Dump "${Verifier}:/tmp/database.dump"
docker exec $Verifier pg_restore --list /tmp/database.dump
docker exec $Verifier createdb -U postgres investment_verify
docker exec $Verifier pg_restore -U postgres -d investment_verify `
  --exit-on-error --no-owner --no-privileges /tmp/database.dump
```

Check every native exit. Compare exact table set, every table count and Flyway
histories against the NEW manifest; compare six full preimages. Expected 71 tables /
7102 refresh rows only if the new snapshot confirms no change. Review any drift.
No production restore, production volumes, ports, network or cleanup. Retain both
old and new backups and verifier evidence. This restore verifies contents, not ACLs.

## 5. Preimages and reconciliation (separate approval C)

NO preimage CSV is supplied. Missing full company/error/null/precision evidence
must never be guessed. Use approved operator psql credentials/configuration, not
passwords in commands; explicitly verify current_database() and server identity.

```powershell
# Separately approved READ-ONLY capture; new local evidence file:
psql -X -q -v ON_ERROR_STOP=1 `
  -f "$ProjectRoot\scripts\database\maintenance\capture_refresh_preimages.sql" `
  -o "$Evidence\reviewed-preimages.csv"
if ($LASTEXITCODE -ne 0) { throw 'Capture failed' }
Get-FileHash "$Evidence\reviewed-preimages.csv" -Algorithm SHA256
```

Review all 17 columns, exact UTC timestamps and nulls, six approved IDs,
RUNNING/TARGETED_LIVE/null completion and SIX zero counter columns. Require an
approval record binding the immutable CSV hash to termination and backup evidence.
Do not stream CSV through PowerShell conversion. Capture alone is not approval.

Before production execution, run positive/negative PostgreSQL tests ONLY against
the isolated restore: missing approval, changed ID/null/microsecond/counter/status,
missing/extra keys, extra incomplete row, locks, triggers/rules and update mismatch.
Every rejected transaction must preserve all rows; success must change only the
six rows' five lifecycle/error columns. Compare all other table contents.
The supplied static tests do not claim PostgreSQL runtime validation.

```powershell
# APPROVAL C ONLY, after isolated tests and fresh preimage re-review:
Push-Location $Evidence
try {
  psql -X -v ON_ERROR_STOP=1 `
    -v maintenance_approval=APPROVED_REFRESH_TERMINATION_AND_RECONCILIATION `
    -v "evidence_id=$ApprovedEvidenceId" `
    -f "$ProjectRoot\scripts\database\maintenance\reconcile_refresh_runs.sql" `
    -L "$Evidence\reconciliation-output.log"
  if ($LASTEXITCODE -ne 0) { throw 'Reconciliation failed; inspect outcome' }
} finally { Pop-Location }
```

Use a new log path. Require BEFORE, AFTER_PENDING_COMMIT, COMMIT and COMMITTED receipt;
independently read back all six rows. Disconnect means UNKNOWN outcome: inspect before
retrying. The script locks the table, checks all columns/global IDs, rejects user
triggers/rules, updates exactly six rows and asserts postimages before commit.
Nothing in startup or deployment invokes it. No checkpoints/events/correlations change.

## 6. Observation while research is stopped (approval D)

```powershell
python -B .\scripts\database\maintenance\observation_pod.py `
  --deployment "$Evidence\deployment.json" --services "$Evidence\services.json" `
  --image $Image | Set-Content "$Evidence\observer.json" -Encoding UTF8
if ($LASTEXITCODE -ne 0) { throw 'Generation failed' }
K create --dry-run=server -f "$Evidence\observer.json" -o json
# Review admission output and CURRENT service selectors before actual create:
K create -f "$Evidence\observer.json"
K wait --for=condition=Ready pod/research-maintenance-observer --timeout=120s
K get pod research-maintenance-observer -o json
$Gate = @{ResearchPod='research-maintenance-observer'; Migrations=$null}
$Baseline = Invoke-DeploymentObservation $Gate
```

Offline generator explicitly overrides image entrypoint with absolute Python
`-I -S -B` running only stdlib sleep. No app import, init/ephemeral containers,
ports, probes, lifecycle hooks, mounts, volumes, host namespaces or API token.
UID/GID 10001, read-only rootfs, no privilege escalation, RuntimeDefault seccomp,
all capabilities dropped. Only allowlisted DB env and pause flags; envFrom rejected.
Database passwords must use a nonoptional Secret reference: literal passwords and
ConfigMap passwords are rejected instead of being copied into evidence JSON. Secret
names/keys remain visible; generated manifests still require controlled handling.
Deployment selectors (including expressions) and Service selectors are checked.
There is deliberately no readiness probe: Kubernetes Ready means the idle process
is running, NOT that credentials, PostgreSQL, schema or observation are healthy.
Only a successful `Invoke-DeploymentObservation` establishes the database gate.
Reject admission injection, altered command/env/security, sidecars/token mounts,
Service or Deployment selection, or unexpected policy effects. Compare the actual
pod before exec. Read-only DB options come from deployment_observation.py, not flags.
Credentials can still write if misused: no unrelated exec is authorized.

Expected QUIET_SNAPSHOT only. BLOCKED/UNKNOWN abort. Migrations=null is NOT migration
verification; retain/revalidate approved Flyway comparison separately. Save the
post-reconciliation baseline. Observer deadline is four hours; expiry aborts work.

## 7. Controlled immutable paused startup (approval E)

Require verified artifact, termination, backup/reconciliation approvals, zero
replicas, healthy research-service, enforced isolation and QUIET_SNAPSHOT FIRST.
This separate maintenance path resolves normal admission's running-pod prerequisite;
it never permits a BLOCKED observation.

```powershell
$Deployment = K get deployment research-engine -o json | ConvertFrom-Json
if ([int]$Deployment.spec.replicas -ne 0) { throw 'Research recreated' }
$Patch = New-VerifiedDeploymentPatch -Deployment $Deployment -Name research-engine -Image $Image
$Patch | ConvertTo-Json -Depth 30 | Set-Content "$Evidence\paused-patch.json" -Encoding UTF8
K patch deployment research-engine --type=strategic --patch-file "$Evidence\paused-patch.json"
K get deployment research-engine -o json
# Require still zero; digest in app AND init; recovery=true/scheduler=false.
# Inspect actual init: readiness and schema SELECT checks only, NO migrations.
$Last = Invoke-DeploymentObservation $Gate
if ($Last.fingerprint -ne $Baseline.fingerprint) { throw 'Database drift' }
K scale deployment/research-engine --current-replicas=0 --replicas=1
K rollout status deployment/research-engine --timeout=180s
K get pods -l app.kubernetes.io/component=research-engine -o json
```

Require exactly one ready app pod, init exit 0, both actual imageIDs accepted by
`Assert-VerifiedPodImage`, actual process pause flags/source hashes, no old processes
and no recovery launches/writes. Re-run denied probes against the NEW pod from EVERY
caller with reachable controls. Sample `Invoke-DeploymentObservation $Gate` every
30 seconds for at least 600 seconds: QUIET_SNAPSHOT and identical baseline each time.
Verify unchanged pod UID/container/restarts, readiness, policies, workload specs,
Helm revision, secret/configmap versions and absence of recovery activity. Probe
enforcement at both ends. Missed cadence, drift, BLOCKED or UNKNOWN means abort.
Quiet logs/time never override database admission or termination requirements.

Do NOT call Complete-DeploymentSafety (it removes isolation). Do not run normal
deploy again to rebuild the accepted artifact. Keep callers and automation frozen.

## Abort / rollback / remaining gates

No automatic retry, old-image restart, DB restore, traffic restoration, policy
removal or scheduler/recovery enablement. SQL errors roll back; after commit retain
truthful FAILED metadata. Restoring RUNNING cannot restore a worker. Failed startup
requires separately approved stop/termination, not automatic image rollback.
Whole-DB restore is a separate disaster-recovery decision because it can discard
unrelated writes. Preserve PVCs, backups, evidence, Equity/ETF history and checkpoints.

Local check: `python -B -m unittest discover -s scripts/database/maintenance -p test_maintenance.py -v`.
By default the PostgreSQL cases are skipped. To exercise synthetic SQL, explicitly run:

```powershell
$env:AIP_MAINTENANCE_ISOLATED_PG='1'
python -B -m unittest discover -s scripts/database/maintenance -p test_maintenance.py -v
```

This uses the already-local postgres:16 image pinned to its inspected ID, creates a
uniquely named network-none container with tmpfs data, no mounts/ports/production
credentials, and removes ONLY that test container in cleanup. It cannot accept a
live DSN. Tests use capture output, all 17 individual preimage mutations, a seventh
active run, an update constraint failure, missing approval, and a successful six-row
commit preserving a completed history row and checkpoint sentinel. Negative tests
assert both the intended error and unchanged database snapshot. Missing approval
uses SQL RAISE with ON_ERROR_STOP; `psql` does not support `\quit 3` as an exit code.

Remaining: locked build inputs, artifact verification, full approved preimages,
writer/controller inventory, fresh backup/restore, admitted observer review and all
maintenance approvals. Operational verdict: **NO-GO** until independently verified.
