# NSE ETF bootstrap: approval required

The historical manual procedure below is retained as an audit record. The integrated path described next supersedes its manual build/patch instructions. A new deployment approval is required. Never use ExecutionPolicy bypass.

## Integrated four-service deployment — local validation only

`platform.ps1` now routes these four components through the verified path, including mandatory admission isolation, complete read-only database observations and a ten-minute quiet window. A mixed selection containing protected and unrelated components is rejected instead of falling back to the general path. No deployment was attempted while implementing this change.

Future command, only after separate operational approval and an actual freeze of operator/internal callers (close API-server port-forward/proxy tunnels first):

```powershell
.\platform.ps1 deploy -Component api-gateway,research-engine,portfolio-service,frontend -KeepOldImages -NoBrowser -AcceptBootstrapRisk -ConfirmCallerFreeze
```

The switches acknowledge the documented old-process risk and caller freeze; they do not skip any automated gate. `-DryRun` performs only read-only artifact checks and explicitly does not certify database quietness or isolation. The script refuses to bypass Windows policy, kill unrelated PIDs, upgrade Helm, run migrations/apply/ingestion/Radar, clean old images or automatically roll back.

Java packaging is scoped to selected `api-gateway,portfolio-service` modules with `-am`. Frontend public build settings are read from deployed compiled output and preserved; ambiguity aborts. Portfolio migration filenames/checksums/success must match source before build. Unique timestamp/GUID images are verified through OCI indexes, child manifests and blob availability, then pinned by digest. Research main/init image updates and paused=true/scheduler=false settings remain atomic. Snapshots of original selected Deployments and unrelated workloads are saved in a user-restricted audit directory. Resource-version drift aborts before workload patching.

The admission gate rejects unknown cross-namespace application pods, host-network application callers, unfrozen CronJobs, active Jobs, local kubectl tunnels and pre-existing policies requiring review. It proves baseline health connectivity from gateway, portfolio and MCP caller pods, then requires timeout-based denial to both research Service and pod IP while control health stays reachable. Missing probe tools or ambiguous responses abort. Isolation remains installed after an interrupted/failed gated deployment for operator review; no unsafe automatic recovery is attempted. Success removes only the temporary policy after image/init/readiness, unchanged durable-state, unrelated-workload and Helm-revision checks.

### Revised legacy observation

The previous query converts text payloads to JSONB across the complete history and sorts by extracted job ID plus event order. The isolated EXPLAIN confirms sequential scan and sort; the precise contribution of live data size/load to the original timeout remains unmeasured. No live EXPLAIN or database query was executed during this fix.

The replacement uses the existing event primary-key index to read 128-row pages in one repeatable-read, read-only transaction. Every page is read through exhaustion: 128 is a page size, never a coverage limit. JSON is parsed once per row; malformed records, unknown job states and incomplete scans fail closed. Latest events are merged with bounded SQL VALUES sorts using PostgreSQL's original text collation and generated_at/event-ID tie ordering. Durable cycle status supersedes stale legacy events; old unresolved legacy-only jobs still block. No age cutoff, schema change or index migration is required.

Each statement retains a five-second timeout and one-second lock timeout. A complete observation has a 25-second processing budget; exceeding it is UNKNOWN, never partial success. Full-state fingerprints cover durable runs/slots/checkpoints, legacy events, refresh jobs/runs, news search and ETF acquisition/evidence/results. Any change aborts. Samples must sustain 30-second cadence over ten minutes; inability to do so aborts. Quiet snapshots are not proof of old in-process quiescence, which remains an explicitly accepted first-bootstrap limitation.

On the 20,001-event isolated fixture, the old SQL took 818.975 ms; the complete paged client scan took 6.031 seconds, with a representative indexed page taking 0.129 ms. This is a bounded-statement correctness fix, not a claim that total scanning is faster. All history still has to be examined absent a separately reviewed persisted index/projection. Live scale can still cause a safe UNKNOWN/abort.

### Executed focused validation

```powershell
python -m pytest scripts/database/tests/test_deployment_observation.py -q -s
# 18 passed, 22.97 seconds, exit 0; isolated DSN supplied privately
$testCode=Get-Content -Raw scripts/database/tests/VerifiedDeployment.Tests.ps1
& ([scriptblock]::Create($testCode))
# 29 passed, 4.50 seconds, exit 0
$testCode=Get-Content -Raw scripts/database/tests/DeploymentSafety.Tests.ps1
& ([scriptblock]::Create($testCode))
# 22 passed, 2.40 seconds, exit 0; PowerShell 5.1.26100.9444
```

Native tests used only `aip-radar-pg-test-f25e2e00`: localhost 55439, bridge network, tmpfs data, independently created/dropped random test databases. The running database, PVC and backup restore were not accessed. The timeout test really waited for PostgreSQL's unchanged five-second cancellation; read-only enforcement, repeated/terminal/unresolved events, malformed events, durable precedence, migration mismatch and concurrent snapshot behavior were exercised. Initial failures were test-fixture credential/PowerShell object-shape issues, corrected before final validation. Existing application suites were not rerun because ETF application code is unchanged.

## Current four-service approval checkpoint — 2026-10-12

### Approved execution attempt — stopped at mandatory pre-deployment check

Operator approved Phase 2 and accepted the documented old-process residual risk. At `2026-10-11 22:29:33.279278+00` (`2026-10-12 00:29:33+02:00`), a PostgreSQL transaction confirmed database `investment`, `transaction_read_only=on`, statement timeout 5 seconds and lock timeout 1 second. The durable run query returned zero nonterminal/owned rows; the active-slot query returned zero rows. The subsequent legacy CYCLE_JOB query (`DISTINCT ON` over JSON payloads in `research.global_opportunity_top_selection`, as shown below) failed with `ERROR: canceling statement due to statement timeout`. Remote psql exited 3; the host PowerShell wrapper exited 1. The connection closed with the read-only transaction aborted. Later refresh-job and migration-history queries did not execute.

Deployment was aborted under the reviewed query-error condition. No image build/push, NetworkPolicy application, workload patch, restart, database write, ingestion or Radar execution occurred. No isolation policy needs removal and no rollback is needed. Existing research configuration remains unchanged (recovery pause false, scheduler false); no replacement pod exists. Zero active durable rows does not establish global quiescence, and Equity-history preservation was not independently reverified during this attempt.

Independent read-only inspection of deployed frontend JavaScript confirmed `apiBaseUrl:""` and `authDevLoginEnabled:false`; future builds must preserve those values. Before another deployment attempt, the legacy-job observation must be made reliable within the approved bound or the observation procedure explicitly revised. Timeout alone does not establish its root cause; no timeout increase or skipped query was attempted.

This section supersedes the older two-service scope below. At 00:22:29 +02:00 all four live rollback images were verified through registry manifest hashing, OCI index/child traversal and referenced blob availability/size checks. The portfolio digest transcription was corrected against the actual pod and registry. All four targets were ready. No NetworkPolicies or CronJobs were found. Research recovery pause is false and scheduler false: idleness is UNKNOWN. No live database access or cluster mutation occurred. Git index fingerprint is unchanged.

Gate 1 requires explicit acceptance of old process-local/established-connection work remaining unobservable despite ingress isolation and quiet durable snapshots. The 30-second termination grace can interrupt such work. Acceptance does not waive aborts for observed activity, failed isolation, unknown callers or inconsistent state.

Use individually staged commands, not the combined deployment entry point. Extract the existing helper definitions below, expanding only the registry verifier's in-memory allowed-name list to these four services. Preserve its verification algorithm. Build Java with `mvn -f services/pom.xml -pl api-gateway,portfolio-service -am package -DskipTests`. Use a timestamp plus full GUID tag. Docker contexts are `services/api-gateway`, `services/portfolio-service`, `ai/research-engine` and `frontend`; push only `localhost:5001/ai-investment/<name>:<unique-tag>`, then resolve each through the verifier to the internal digest reference.

Before frontend build, establish deployed `NEXT_PUBLIC_API_BASE_URL` and `NEXT_PUBLIC_AUTH_DEV_LOGIN_ENABLED` from build evidence and explicitly preserve them as build arguments. Abort if unavailable; do not assume defaults. Before portfolio rollout, compare packaged migrations with read-only `SELECT installed_rank,version,script,checksum,success FROM portfolio.flyway_schema_history_portfolio ORDER BY installed_rank` inside the bounded read-only transaction below. Abort on pending, failed or inconsistent migrations. No migration execution is authorized.

Save exact original Deployment JSON, rollback references, unrelated workload templates and Helm revision in a restricted audit directory inside the integration repository before mutations. Do not print credentials. Use this minimal patch constructor; four previews against current Deployments and unrelated-workload rejection passed locally (exit 0), without applying any patch. The initial null-init-container guard was corrected before that successful check.

```powershell
function New-FourServicePatch($Deployment,[string]$Name,[string]$Image) {
 if($Name -in @('api-gateway','research-engine')) { return New-VerifiedDeploymentPatch $Deployment $Name $Image }
 if($Name -notin @('portfolio-service','frontend') -or $Deployment.metadata.name -ne $Name -or $Deployment.metadata.namespace -ne 'ai-investment' -or -not $Deployment.metadata.resourceVersion){throw 'Unexpected deployment'}
 if($Image -notmatch "^ai-investment-registry:5000/ai-investment/$Name@sha256:[a-f0-9]{64}$"){throw 'Unverified image format'}
 if(@($Deployment.spec.template.spec.containers | Where-Object name -eq $Name).Count -ne 1 -or @($Deployment.spec.template.spec.initContainers | Where-Object { $null -ne $_ }).Count -gt 0){throw 'Unexpected container layout'}
 return @{metadata=@{resourceVersion=$Deployment.metadata.resourceVersion};spec=@{template=@{spec=@{containers=@(@{name=$Name;image=$Image})}}}}
}
```

After verified builds, caller freeze, successful isolation enforcement and the ten-minute observation, construct all four patch files with fresh Deployment objects and verified new images. Review exact diffs. Apply sequentially: research-engine, API Gateway, portfolio-service, frontend. Each command is pinned to context/namespace by the existing helper:

```powershell
foreach($name in @('research-engine','api-gateway','portfolio-service','frontend')) {
 Invoke-DeploymentKubectl @('patch','deployment',$name,'--type=strategic','--patch-file',$patchPaths[$name])
 Invoke-DeploymentKubectl @('rollout','status',"deployment/$name",'--timeout=420s')
 # Verify imageID, health and preserved configuration before continuing; abort on failure.
}
```

Research main/init images and paused=true/scheduler=false settings change atomically; init command/settings stay intact. No Helm, PID/image cleanup or automatic rollback. Budget ten minutes observation plus up to four seven-minute rollout windows: at most 38 minutes outage, excluding pre-outage builds. Abort rather than extend silently. Old research rollback can resume recovery and needs separate review/acceptance.

Isolation checks must test Service port 80 and pod port 8000 from existing caller pods using bounded health GETs plus a reachable control destination. Recheck additive policies, host-network callers and operator tunnels. Server dry-run validates schema, not enforcement. SQL below matches source migrations; live schema/state and isolation enforcement remain unverified until approval. After replacement pause verification and old-pod termination, remove only the temporary policy, retain caller freeze and perform authenticated preview/preflight/history checks. No apply, acquisition or Radar POST is included; later gates remain separate.

## Gate 1 scope and limits

Approve only gateway/research image builds and pushes, temporary research ingress isolation, read-only durable-state inspection, and digest-pinned replacement of these two Deployments. No Helm upgrade, migration, database writes, universe apply, ingestion or Radar. Research-dependent operations will be unavailable during isolation: budget 10 minutes observation plus up to 14 minutes rollout/verification; abort rather than extend silently. Build before outage.

Current rollback repositories are `ai-investment-registry:5000/ai-investment/api-gateway` and `ai-investment-registry:5000/ai-investment/research-engine`. Verified digests:

- Gateway: `sha256:69895f18d6360cdc087d51b3f54c29a822d283f709a1b50ae77bb2e18239d5b5`
- Research: `sha256:c9ccb210e81c45a3152079baca8ca939e521340ed23dcace4522748eb98edff3`

The old research image has recovery pause false, scheduler false and 30-second termination grace. Process-local pending requests, evidence tasks and existing connections are not comprehensively observable. An ingress NetworkPolicy does not stop internal timers or reliably terminate established connections; node traffic and API-server tunnels can bypass it. Operator acceptance of these residual risks is required, independently of quiet database snapshots. Otherwise STOP. Never claim global quiescence.

## Prepare verified artifacts (after approval)

PowerShell 5.1, repository root, interactive commands:

```powershell
$ErrorActionPreference='Stop'
$tokens=$null; $errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile((Join-Path (Get-Location) 'platform.ps1'),[ref]$tokens,[ref]$errors)
if ($errors.Count) { throw 'Parse failure' }
foreach ($name in @('Invoke-DeploymentCommand','Invoke-DeploymentKubectl','Get-VerifiedRegistryImage','New-VerifiedDeploymentPatch')) {
  $node=$ast.Find({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name},$true)
  if (-not $node) { throw 'Missing helper' }
  . ([scriptblock]::Create($node.Extent.Text))
}
$rollback=@{
 'api-gateway'='sha256:69895f18d6360cdc087d51b3f54c29a822d283f709a1b50ae77bb2e18239d5b5'
 'research-engine'='sha256:c9ccb210e81c45a3152079baca8ca939e521340ed23dcace4522748eb98edff3'
}
$old=@{}
foreach ($name in $rollback.Keys) { $old[$name]=Get-VerifiedRegistryImage $name $rollback[$name] }
$tag='etf-closure-'+(Get-Date -Format 'yyyyMMdd-HHmmss')+'-'+[guid]::NewGuid().ToString('N')
Invoke-DeploymentCommand mvn @('-f','services/pom.xml','-pl','api-gateway','-am','package','-DskipTests')
$images=@{}
foreach ($name in @('api-gateway','research-engine')) {
 $context=if ($name -eq 'api-gateway') {'services/api-gateway'} else {'ai/research-engine'}
 $ref="localhost:5001/ai-investment/${name}:$tag"
 Invoke-DeploymentCommand docker @('build','-t',$ref,$context)
 Invoke-DeploymentCommand docker @('push',$ref)
 $images[$name]=Get-VerifiedRegistryImage $name $tag
}
```

Record returned immutable references in the operator audit record; never print tokens or secrets. Helpers verify OCI indexes, child manifests and blob availability. Recheck current main/init image resolution equals the approved rollback digest; abort on drift. Snapshot Deployment pod templates, UIDs, imageIDs, Helm revision and unrelated workloads before changes, in a restricted operator record.

## Admission isolation and observation checkpoint

Verified research selector: all three labels below. Proposed temporary policy (not applied):

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: research-bootstrap-isolation
  namespace: ai-investment
spec:
  podSelector:
    matchLabels:
      app.kubernetes.io/name: ai-investment-platform
      app.kubernetes.io/instance: ai-investment-platform
      app.kubernetes.io/component: research-engine
  policyTypes: [Ingress]
  ingress: []
```

Save the reviewed policy outside source as an operator artifact. First server-side dry-run, then apply only after approval:

```powershell
Invoke-DeploymentKubectl @('apply','--dry-run=server','-f',$ApprovedPolicyPath)
Invoke-DeploymentKubectl @('apply','-f',$ApprovedPolicyPath)
```

No existing NetworkPolicies were present at inspection; recheck because allow policies are additive. Verify CNI enforcement using nonmutating health connections from existing caller pods to both Service and pod IP. Include gateway, portfolio-service and mcp-gateway; inventory all namespaces' workloads/jobs/cronjobs/endpoints. Freeze operator submissions and close independently managed direct port-forwards/API proxies; unknown callers or unavailable enforcement checks mean ABORT. Do not assume ingress denial blocks node/host-network traffic. Do not modify unrelated services to simulate isolation.

After isolation, observe for 10 minutes, sampling every 30 seconds. Before/after and intermediate samples must include pod UID/restarts, request logs, active database state and changing state fingerprints. Any active/cancellation-pending work, lease movement, checkpoint/evidence changes, unexpected API traffic, malformed historical payload, query error or inconsistent sample means ABORT. Quiet logs do not prove idleness.

Use an operator-provided read-only database connection only after explicit approval; do not fetch or print credentials. SQL below is schema-qualified against the configured `research` schema and source migrations. Each sample uses a new transaction so changes remain observable:

```sql
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL statement_timeout='5s';
SET LOCAL lock_timeout='1s';
SELECT current_database(), current_setting('transaction_read_only'), clock_timestamp();
SELECT cycle_id,market,status,owner_id,lease_expires_at,updated_at
FROM research.global_opportunity_cycle_run
WHERE status NOT IN ('COMPLETED','FAILED','CANCELLED') OR owner_id IS NOT NULL
ORDER BY cycle_id;
SELECT * FROM research.global_opportunity_cycle_active ORDER BY market;
SELECT cycle_id,phase,state,count(*),max(updated_at)
FROM research.global_opportunity_cycle_progress GROUP BY cycle_id,phase,state ORDER BY 1,2,3;
WITH events AS (
 SELECT DISTINCT ON (payload::jsonb->>'cycle_id') payload::jsonb AS p
 FROM research.global_opportunity_top_selection
 WHERE payload::jsonb->>'record_kind'='CYCLE_JOB'
 ORDER BY payload::jsonb->>'cycle_id',generated_at DESC,cycle_id DESC
)
SELECT p->>'cycle_id',p->>'status',p->>'updated_at' FROM events
WHERE p->>'status' NOT IN ('COMPLETED','FAILED','CANCELLED');
SELECT job_id,status,last_progress_at FROM research.research_refresh_jobs
WHERE completed_at IS NULL ORDER BY job_id;
SELECT refresh_run_id,status,updated_at FROM research.research_refresh_runs
WHERE completed_at IS NULL ORDER BY refresh_run_id;
SELECT count(*),max(attempted_at) FROM research.etf_acquisition_attempts;
SELECT count(*),max(completed_at) FROM research.research_news_search_runs;
SELECT count(*) FROM research.etf_radar_cycles;
SELECT count(*),max(retrieved_at) FROM research.etf_listing_observations;
SELECT count(*),max(retrieved_at) FROM research.etf_fact_observations;
SELECT count(*),max(retrieved_at) FROM research.etf_nav_observations;
COMMIT;
```

These watermarks detect activity but do not prove absence of updates or unfinished process-local tasks. Capture full durable run/checkpoint fingerprints for comparisons where needed. Snapshot existing Equity result and portfolio/canonical counts using separately reviewed read-only queries; no live database query was executed in this task.

## Rollout checkpoint (only after accepted observations and residual-risk approval)

Never use stale resourceVersions. Keep isolation active. Reverify rollback artifacts, then use freshly read Deployment objects:

```powershell
foreach ($name in @('research-engine','api-gateway')) {
 if ((Get-VerifiedRegistryImage $name $rollback[$name]) -ne $old[$name]) { throw 'Rollback changed' }
 $deployment=Invoke-DeploymentKubectl @('get','deployment',$name,'-o','json') | ConvertFrom-Json
 $patch=New-VerifiedDeploymentPatch $deployment $name $images[$name]
 $path=Join-Path $ApprovedAuditDirectory "$name-$tag.patch.json"
 $patch | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $path -Encoding ASCII
 Invoke-DeploymentKubectl @('patch','deployment',$name,'--type=strategic','--patch-file',$path)
 Invoke-DeploymentKubectl @('rollout','status',"deployment/$name",'--timeout=420s')
}
```

Research patch atomically updates main and migration-wait init images, sets recovery pause true and scheduler false, and preserves init command/settings. No migrations are invoked. Verify new pod imageIDs, environment, init completion, health and absence of startup recovery/provider activity; require old pod termination before restoring admission. Compare unrelated workload templates/images, Helm revision and approved data snapshots. Abort on drift.

Isolation prevents end-to-end Gateway tests; remove only this temporary policy after replacement pause verification and with caller freeze still enforced. Pause prevents Radar submissions, not all other research mutations. Restore admission deliberately:

```powershell
Invoke-DeploymentKubectl @('delete','networkpolicy','research-bootstrap-isolation')
```

Then perform authenticated ADMIN read-only preview:

```powershell
Invoke-RestMethod -Uri 'http://localhost:18080/api/v1/market-universe/india/nse-etf/preview' -Headers $AdminBearerHeaders -Method Get
```

Readiness-only bounded preflight after canonical instruments exist:
`GET /api/v1/etf-radar/preflight?instrument_ids=<verified-uuid>` (repeat parameter, max five). Returns scope, canonical identity match, mandatory evidence readiness, provider/as-of details, blocking reasons, active cycle and recovery pause. Does not ingest, enumerate or start work.

## Rollback and later gates

No automatic rollback. Keep admission isolated on failure and stop. Old research image may ignore the corrected pause and recover/write during startup. Rolling it back requires separate explicit acceptance of that risk and fresh durable-state checks. Gateway-only rollback can use the same patch helper with its verified old digest; no Helm rollback. Retain all old images/artifacts.

Gate 2: authenticated official preview must show reviewed new/existing identities/conflicts and expected writes before POST apply. Gate 3: approve at most five canonical ETF IDs for POST `/api/v1/etf-radar/evidence` with `instrument_ids`; read back provenance and actual observation dates. Gate 4: separately authorize any required pause-control change and exactly one POST `/api/v1/etf-radar/controlled-cycles` for those ready IDs. Runner defaults to no-request dry run and uses the new read-only preflight before one start request. A normal unpause may recover other durable work; inspect it first. No Equity cycle.

Calendar support remains conservative: old Friday evidence may be stale on Sunday. Do not override freshness for validation. Real readiness/exclusions, including an explainable empty ranking, must be preserved.

## Full-universe extension — source implemented, operational approval pending

The controlled 1–5 instrument runner above remains a smoke test. It is not full-universe acceptance. ADMIN `POST /api/v1/etf-radar/full-universe-cycles` now submits the official NSE universe to the existing durable ETF worker. Body: `{"max_universe":2000,"max_requests":1500,"max_seconds":3600}`. These are fail-closed resource guards, not shortlist limits. API maxima are 5000 instruments, 15000 requests and 14400 seconds. Recovery pause rejects submission. No request has been executed live.

ADMIN `GET /api/v1/etf-radar/full-universe-cycles/{cycle_id}/coverage` reports frozen official scope, completed outcomes, request reservations and provider failure reason. Existing status/result contracts retain their legacy fields. Every official row receives an identity/acquisition outcome; unmatched identities remain visible. `activated` counts already-active matched canonical ETFs, not writes performed by this workflow. Acquisition and analysis incompleteness are distinct counts; counts are not mutually exclusive.

Acquisition uses batches of five over the entire frozen official listing, sequential provider requests with at least one-second spacing, 15-second timeouts, bounded 5xx retries and cumulative durable request reservations. HTTP 403/429 stops the run and prevents automatic provider retries after recovery. Successful completed batches are not reacquired; interrupted uncheckpointed work can retry idempotently. Publication requires complete first-pass coverage and the existing owner/lease/cancellation fence. Technical acquisition failure cannot replace the prior result. No schema migration was added.

Price/volume observation dates and separately dated NAV retain existing conservative freshness. Stale supporting facts/history do not become scores. NAV premium requires matching valuation dates (and scoring currency). Peer ranks use verified official category metadata; unknown/conflicting classifications are excluded from the shortlist. Existing numerical ranking thresholds remain unchanged. DailyMarketBar history is reused, not automatically populated by quote acquisition. Total-return adjustment completeness, comparable benchmark returns, independently timed iNAV, spread, AUM/TER and tracking data remain unavailable unless independently supported by existing evidence. Missing data is disclosed, never synthesized.

### Additional rollout scope and approval

The original gateway/research-only helper and its previous 23 mocked checks are unchanged. This extension also changes portfolio-service ETF enumeration to avoid user upserts and frontend full-universe controls/display. **Do not silently use the two-component helper to deploy all four services.** Before a separately approved expanded rollout, preserve frontend build-time configuration, verify portfolio Flyway history has no pending migration, and review exact digest patches for those two additional Deployments. Abort if either check is unavailable. PostgreSQL and all unrelated workloads remain outside rollout scope.

Additional current rollback references verified read-only in this task (registry index/child manifests/blobs):

- `ai-investment-registry:5000/ai-investment/portfolio-service@sha256:6d5e111a118a659bf84bdee03e2b0561feed003f7feb6b41fa9ddebef80ddd9f`
- `ai-investment-registry:5000/ai-investment/frontend@sha256:af0ddfbc03c38e44187a89382c88d717cc0b6bfa5ec199b9a0f0d519c889c60d`

Request one combined Gate 1 approval for unique image builds/pushes, reviewed four-service digest rollout, temporary admission isolation, read-only durable-state/migration checks, the ten-minute abort-on-activity observation and explicit acceptance of old-process observability limitations. Research must start paused with scheduler disabled; preserve migration-wait settings. Expect research/API interruption during isolation plus rollout; frontend/portfolio may also briefly interrupt requests. No automatic old-research rollback: it may resume durable work. Keep the verified backup and all rollback artifacts.

Gate 2 still requires reviewed official preview and separate synchronization approval. Gate 3 still requires separate approval for initial evidence ingestion of at most five verified ETFs. **Full-universe Gate 4 is additional explicit authorization**, covering pause restoration risks and one full-universe acquisition/evaluation request, not merely the five-instrument smoke test. Inspect other recoverable work before unpausing. At 352 instruments the happy path uses 354 NSE requests, approximately six minutes of minimum request spacing plus evaluation/persistence; the default wall limit is one hour per invocation. Provider refusal, stale evidence or unavailable history can prevent a shortlist. Never rerun Equity Radar.

Future request, only after Gate 4 approval and prerequisites:

```powershell
$body = @{ max_universe = 2000; max_requests = 1500; max_seconds = 3600 } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri 'http://localhost:18080/api/v1/etf-radar/full-universe-cycles' -Headers $AdminBearerHeaders -ContentType 'application/json' -Body $body
# Monitor the returned cycle ID using existing status and the read-only coverage route.
```

### Validation of this extension

Fixture sizes 7, 352 and 357 each produced exactly N official/reconciled/active/attempted/acquired/evaluated records, N+2 provider requests, zero failed/rejected records and zero ranked/shortlisted records. Those fixtures deliberately lack analytical history/category evidence: all N are disclosed as incomplete/unclassified, not fabricated buys. Separate rule-engine fixtures exercise qualifying results, exclusions and stable peer ordering. Conflict fixtures retain missing and conflicting identities.

Executed commands (exit 0 unless noted):

```powershell
# Research directory; SQLite :memory:, persistence disabled for fixture suites
python -m pytest tests/test_etf_full_universe.py tests/test_etf_rule_engine.py tests/test_etf_ingestion.py tests/test_nse_etf_market.py tests/test_etf_acquisition_persistence.py tests/test_etf_readiness.py tests/test_etf_metrics.py tests/test_etf_opportunity_cycle.py tests/test_etf_preflight.py tests/test_paused_startup.py tests/test_opportunity_worker.py tests/test_cycle_cancellation.py tests/test_cycle_checkpoint_recovery.py tests/test_radar_recovery_closure.py tests/test_global_opportunity_scheduler.py tests/test_etf_radar_async_lifecycle.py tests/test_etf_radar_api.py -q -o faulthandler_timeout=120
# 405 passed; subsequent focused tests cover final refinements
python -m pytest tests/test_etf_full_universe.py -q -o faulthandler_timeout=120
# 28 passed
python -m pytest tests/test_etf_full_universe_postgres.py -q
# 5 passed; DSN supplied privately only after isolation checks
python -m pytest tests/test_etf_full_universe.py::test_acquisition_fencing tests/test_etf_acquisition_persistence.py -q
# 89 passed after the final transactional lease-expiry correction
# Repository root
mvn -f services/pom.xml -pl portfolio-service -am test '-Dtest=NseEtfUniverseControllerTest' '-Dsurefire.failIfNoSpecifiedTests=false'
# 10 passed
```

Native tests used existing disposable `aip-radar-pg-test-f25e2e00`, localhost-only port 55439, bridge network only, tmpfs data and random test databases. Neither Kubernetes PostgreSQL nor the backup restore was accessed. Existing migrations were applied only to disposable test databases.

Refreshed frontend source in the isolated Node 24 validation volumes: `node --test tests/etf-radar.test.mjs tests/radar-run-action.test.mjs` (61 passed), `tsc --noEmit --incremental false` and scoped ESLint (exit 0). Earlier missing dependencies/obsolete assertions and fixture-date failures were corrected. One final mistyped `-k fence` selected no tests and exited 1; it is not counted as validation. No final broad tests were skipped/deselected. Python emits an existing Starlette deprecation warning. Previous 23 deployment mocks were not rerun because deployment scripts did not change.

Files changed for this extension: `app/etf_full_universe.py`, `app/etf_acquisition.py`, `app/etf_ingestion.py`, `app/etf_persistence.py`, `app/etf_opportunity_cycle.py`, `app/etf_rule_engine.py`, `app/main.py`; research tests `test_etf_full_universe.py`, `test_etf_full_universe_postgres.py`, `test_etf_rule_engine.py`; portfolio `GlobalInstrumentController.java`, `NseEtfUniverseControllerTest.java`; frontend `app/components/etf-radar.tsx`, `tests/etf-radar.test.mjs`, `tests/radar-run-action.test.mjs`; this runbook. Existing unrelated modifications and the Git index are preserved.

Outcome: **READY FOR APPROVAL**, not live completion. Runtime official reconciliation, category coverage, evidence readiness, full-universe terminal coverage and preservation checks remain acceptance gates after separately authorized operations.

Final preservation checks: Git index SHA256 `f0132cefb4cfbd4a75fbd88f37237eb50bab413130fd2c2021e663c6e0bf1343` and both diagnostic JAR hashes remain unchanged. Scoped `git diff --check` and new-file whitespace checks pass. Repository-wide `git diff --check` exits 2 for pre-existing whitespace in `scripts/database/PostgresBackupCommon.psm1`; that unrelated file was not changed by this extension.

## Local validation commands

From `ai/research-engine`, with `AIP_RESEARCH_DATABASE_BACKEND=sqlite`, `AIP_RESEARCH_DATABASE_NAME=:memory:` and `AIP_RESEARCH_PERSISTENCE_ENABLED=false`:

```powershell
python -m pytest tests/test_etf_preflight.py tests/test_etf_radar_async_lifecycle.py -q -o faulthandler_timeout=60
python -m pytest tests/test_paused_startup.py tests/test_opportunity_worker.py tests/test_cycle_cancellation.py tests/test_cycle_checkpoint_recovery.py tests/test_radar_recovery_closure.py tests/test_global_opportunity_scheduler.py tests/test_etf_radar_async_lifecycle.py tests/test_etf_radar_api.py tests/test_etf_preflight.py tests/test_etf_ingestion.py tests/test_etf_opportunity_cycle.py -q -o faulthandler_timeout=60
```

From repository root (all external commands mocked; runner entry point is not executed):

```powershell
$testCode=Get-Content -Raw scripts/database/tests/EtfControlledRunner.Tests.ps1
& ([scriptblock]::Create($testCode))
$testCode=Get-Content -Raw scripts/database/tests/VerifiedDeployment.Tests.ps1
& ([scriptblock]::Create($testCode))
git diff --check -- ai/research-engine/app/main.py ai/research-engine/app/etf_opportunity_cycle.py ai/research-engine/app/opportunity_worker.py etf-radar-controlled-run.ps1
```

Executed results: targeted preflight/lifecycle 28 passed (exit 0); final broader regression 215 passed, one Starlette deprecation warning, 200.67 seconds (exit 0); runner mocks 14 passed (exit 0); deployment mocks 23 passed (exit 0); scoped diff/whitespace checks passed (exit 0). No tests skipped or deselected in the final broader run. An earlier broader run was stopped after a shutdown hang; the deterministic reproduction failed before the worker loop correction and passed afterward. Initial new-test fixture failures were corrected. Prior PostgreSQL 9-pass and provider/persistence 144-pass results were not rerun or represented as new execution. No persistence schema changes.
