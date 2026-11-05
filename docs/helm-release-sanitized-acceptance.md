# Helm revision-2 sanitized acceptance

This is a read-only acceptance tool for the existing local k3d cluster. It is not a deployment tool. It reuses the internal k3d Helm access established by PR #23 without depending on the broken Windows host-port mapping or copying a Linux symlink to Windows.

From the repository root, after fetching this PR branch, with the Helm Docker image already present and PyYAML installed in the project's Python venv:

    .\.venv\Scripts\python.exe infrastructure\scripts\helm_k3d_acceptance.py

The tool:
- Verifies that the Kubernetes API is healthy and Helm revision 1 is the deployed revision.
- Runs a server-connected Helm revision-2 dry-run with protected values and the approved NSE image tag; no install/upgrade is submitted.
- Keeps unredacted rendered manifests only in process memory because equality checks cannot use Helm's --hide-secret output; stdout and stderr from Helm are never displayed.
- Compares 54 proposed identities with live resources and intended spec/data fields, ignoring server-injected defaults and bookkeeping metadata.
- Checks rendered data equality for both DEV PostgreSQL and SearXNG credential keys.
- Requires research-engine app/init images to match the approved tag.
- Prints only counts and changed field paths, never Secret values. Blocks the release on any unexpected difference.

Security: The tool reads live Kubernetes Secrets and temporarily mounts the existing admin kubeconfig in a disposable Helm container. Use on a trusted Windows workstation and verified Docker image only. It removes its temporary kubeconfig on exit. No log, dump, or manifest artifacts are created.

The tool deliberately fails closed on drift. It does not verify the research-image cache on every k3d node, rollback, or backup validity; these are separate gates before an actual upgrade. Any BSE market-data recovery requiring executed price, positive volume, currency, and timestamps remains out of scope.

Do not merge this PR or run a Helm upgrade on the basis of an untested result. First run the script and its pure unit tests (pytest infrastructure/scripts/test_helm_k3d_acceptance.py), review sanitized output, and separately verify image availability and rollback capability.

## First operator result (blocked safely)

The first run verified 54/54 resource identities, exact DEV Postgres/SearXNG credential matches and approved research images, but reported 105 desired-vs-live field differences, including unrelated Java workload image/env fields. No upgrade occurred. This is **not** an application incident: it means the proposed upgrade was not equivalent to live desired state. The first script used only --reuse-values; the chart's established LOCAL/DEV configuration is in values-dev.yaml. The next revision explicitly supplies -f /chart/values-dev.yaml in addition to --reuse-values, retaining the guard. This is a hypothesis to test, not proof that the 105 differences are harmless. If any differences remain, the gate must continue blocking. No automatic drift repair or deployment is authorized.

## Diagnostic revision after second operator run

The explicit DEV profile did **not** change the 105-field result (4 pure tests passed, all 54 identities and two DEV credentials matched). The values-file hypothesis is therefore not supported by this observation. The script now obtains the **stored Helm revision-1 manifest** via a read-only internal Helm call and compares three states: stored revision 1, live Kubernetes, and proposed revision 2. It reports only the number of changed field paths and affected resource identifiers for each pair; Secret bodies remain memory-only. The Helm revision-2 acceptance **continues to fail closed** on ANY proposed-vs-live difference. This triage distinguishes old Helm drift from changes introduced by the proposed upgrade, rather than assuming that loading a values file fixes the problem. Do not deploy or merge on the basis of the 105-field result.

## Three-way operator result and conservative baseline

Observed: 54/54 resource identities, credential equality passed, 110 v1-to-live field differences, 6 proposed-v2-to-v1 differences affecting portfolio-service and research-engine, and 105 proposed-v2-to-live field differences. No deployment. This establishes substantial pre-existing drift, but it does **not** establish how Helm would construct and apply its three-way patch; direct manifest comparison is deliberately more conservative than Helm's patch calculation.

The experiment with adding values-dev.yaml did not reduce the 105 differences and introduced a portfolio-service change relative to stored v1. The acceptance script has reverted that addition and now bases the upgrade dry-run on stored release values with only the explicitly approved research flags and image tag overrides. It prints **only field paths** for changes relative to v1, in addition to prior counts. Those paths must be reviewed; any unexpected portfolio-service change blocks release.

**Do not automatically reconcile historical drift by forcing live manifests back to stored v1.** The next release gate needs an audited Helm three-way patch / server dry-run impact review to determine which live fields would actually be changed. No upgrade is approved by this diagnostic.

## Revised release-scope gate (awaiting operator test)

The three-way evidence is now stable: **110 stored-v1-to-live paths**, **5 proposed-v2-to-v1 paths exclusively in research-engine**, **105 proposed-v2-to-live paths**. The prior direct desired-to-live zero-drift requirement incorrectly conflated historical drift with a release change. The script now performs a strict *rendered-release scope* check: starting from the proposed manifests, it substitutes only the two approved research container image changes and three named manual execution flags with the stored v1 values, then requires **full manifest structural equality** with stored v1 for all 54 resources. It separately requires live research-engine to already have the approved images, safety flags, and manual execution values and keeps byte equality checks on the two DEV credentials. Historical live drift is reported but **is not by itself a failure**.

This is **not Helm's actual Kubernetes strategic three-way patch calculation** and does not prove Helm cannot touch an unintended live field. A successful scope gate may be used to advance release review, **not** directly to authorize upgrade. Actual change-impact verification, per-node image availability, current backup integrity, and rollback readiness remain mandatory. This tool does not mutate resources. Pure synthetic regression tests cover unrelated desired changes, wrong live images and flags, unapproved resource identities, and tolerated historical drift.

## Credential representation regression (awaiting operator verification)

The first release-scope run after 9 passing unit tests was stopped by
`UNAPPROVED_STORED_RELEASE_CHANGE: Secret/dev-postgres`. The chart's old
release manifest used `Secret.stringData` while the new preserving helper
emits `Secret.data`. The scope guard now considers those representations
equivalent **only if** the normalized Secret type/data bytes are identical
and all remaining Secret metadata/fields are exactly unchanged. A different
password, missing data key, added annotation or type change still blocks.
Live Postgres and SearXNG credential equality is checked separately before
any release success message. Three synthetic regression tests cover matching
bytes, changed bytes and changed metadata. This is not authorization to
perform the actual Helm upgrade.

## Final read-only prerequisite inventory (added, not yet operator-tested)

The release-scope guard passed on Windows: 12 tests passed, 54 identities, both DEV credentials unchanged, only the 5 approved research-engine fields differ from stored v1. That is strong evidence for a narrow proposed manifest change, **not** a proof of exact Kubernetes strategic merge patch behavior.

A second script inventories node-readiness and image-tag cache evidence plus basic backup existence/freshness, without reading backup content or changing Kubernetes. Run from repo root with the trusted project venv:

    .\.venv\Scripts\python.exe infrastructure\scripts\helm_k3d_release_prerequisites.py

The script intentionally reports cache presence as evidence, **not** registry pull availability, and reports backup file presence **not** backup restorability. A recent successful restore test, verified release rollback readiness, and explicit review of the Helm three-way apply impact remain mandatory before any live upgrade. No one should run `helm upgrade` merely because this inventory succeeds. Backup files are not committed.

For a no-change Kubernetes object where the old and new Helm manifests are identical, Helm generally has no user-authored field difference to patch, but this is not a substitute for reviewing generated Kubernetes patches and potential defaults/metadata side effects. Historical drift remains visible; the current validator deliberately avoids changing that live drift to match old revision 1.

## Final release gate: database backup format + patch-impact caution

The operator confirmed the prerequisite inventory: 3/3 nodes Ready; 3/3 nodes advertise the approved research-engine tag; one nonempty main-database backup 3.1 hours old at the time of check. This establishes **image-cache and file-existence evidence, not a successful restore**.

The read-only backup-format inspector `infrastructure/scripts/helm_k3d_backup_format.py` checks the newest file in `.tmp/main-db-backups`: identifies PostgreSQL custom dump, gzip, tar, or apparent SQL format; validates gzip stream checksums where applicable. It never reads or modifies the production database, never outputs backup contents or filename, and never creates Kubernetes resources. Test only with the local venv Python; four offline synthetic format tests are in `infrastructure/scripts/test_helm_k3d_backup_format.py`.

A recognized signature or valid gzip checksum is **not** proof that a backup can be restored or that the dump includes all necessary roles/schema/data. A real restore test must use a **separate temporary PostgreSQL instance and isolated storage**, not the live `postgres-0` or its PVC.

**Patch-impact risk persists:** Helm 3/4 uses a three-way strategic merge patch for built-in Kubernetes resources. An unchanged old-vs-new manifest does not by itself prove that drifted live fields cannot be reconciled, particularly for images and environment arrays. The 110 stored-v1-to-live field differences (including many unrelated deployments) justify preserving the deployment gate until the patch plan is verified through an implementation faithful to the Helm/Kubernetes patch algorithm or a fully isolated representative rehearsal. Neither `helm upgrade --dry-run=server` nor the current structural guard provides an exact patch payload. Do not claim that any change to live workloads is impossible. No real upgrade is approved yet.

## Upgrade-impact decision after the isolated restore succeeded

Operator result: PostgreSQL 16 isolated restore completed with 71 user tables,
no restore-command errors, and disposable container cleanup succeeded. The
working `postgres-0` and its PVC were untouched.

**RELEASE BLOCKED — pending faithful patch-impact evaluation.**
The Helm v1 stored manifest differs from live deployment fields across 18
Deployments (110 reported field paths, principally container images and env
values). Proposed v2 differs from stored v1 in only five approved research-engine
paths, but this **does not** establish that 18 unrelated workload pod templates
will remain unchanged: Helm's implementation generates a strategic three-way
patch from the old release, the new release, and the current Kubernetes object,
not merely an old-to-new diff.

A no-deploy decision is warranted without further user-side reruns. A successful
unit-test suite, cluster readiness check, backup restore, and scoped Helm manifest
change are all necessary evidence, but insufficient to authorize upgrading.

### Safe engineering paths

**Preferred:** Build a reviewed release reconciliation that produces a Helm
revision-2 desired manifest preserving all already-approved live image and env
values for unrelated deployments (without copying credentials into git). Feed
the exact effective manifests into a Helm/Kubernetes strategic three-way patch
calculation against the stored revision 1 and current live objects. Reject any
patch to unrelated Deployment pod templates, protected Secrets, StatefulSets or
PVCs. Dry-run with the **exact same** options and post-rendering behavior as
the approved real upgrade. Preserve the five research-engine flags and image
in both manifest and live state. A tested, deterministic process for acquiring
live values and reconciling only reviewed fields is required; do not blindly
replace Helm values or use a bulk live-manifest takeover.

**Alternative:** Intentionally adopt Helm's older stored values for each
drifted workload only after a separate change review and application-health
plan; this is **not** approved in the current DEV release.

Do not use `helm upgrade --force`, `helm rollback`, `kubectl apply`, secret
rotation, cluster recreation, blanket pod restarts, or delete any DB/PVC in
an attempt to make the acceptance checks pass. The current live cluster is
the operational baseline until a faithful patch impact is proven safe.

**Milestone:** Backup recovery evidence now supports the release; the only
remaining blocker is exact upgrade-impact/reconciliation engineering, not a
request for more manual backup, node, or scope commands.

## Read-only conservative patch-risk gate (added; not operator-tested)

`infrastructure/scripts/helm_k3d_impact_gate.py` reads stored Helm v1,
server-connected proposed revision-2 dry-run, and live Kubernetes resource
JSON using the same internal k3d API approach as the existing scope guard.
It checks the previously approved exact research-engine release scope,
then **blocks** on any unrelated Deployment pod-template discrepancy between
desired and live, any change to unrelated desired Deployment pod templates,
protected Secret data/type differences, or StatefulSet/PVC discrepancies.
Only safe resource names and risk codes are printed; Secret contents are
never printed and no write is performed.

This gate is a **conservative risk filter**, not an implementation of Helm's
actual strategic merge patch. A block means release safety is not established;
it is not proof that a particular pod would roll in practice. A pass is only
a prerequisite for a faithful Helm/Kubernetes patch calculation. Do not merge
or upgrade solely because it passes. Five synthetic offline tests check risk
classification; operator validation is pending.

When required, run from repo root with trusted Python venv:

    .\.venv\Scripts\python.exe -m pytest infrastructure\scripts\test_helm_k3d_impact_gate.py -q
    .\.venv\Scripts\python.exe infrastructure\scripts\helm_k3d_impact_gate.py

No credentials, backups, logs or kubeconfig files are written to the
repository. The current 110 known field differences across 18 Deployments
make a conservative block likely until a separately reviewed reconciliation.

## Impact gate refinement: distinguish Kubernetes defaults (awaiting operator run)

Earlier conservative risk output: 20 findings (19 unrelated Deployments and the
PostgreSQL StatefulSet). That was a **potential** impact classification, not
the actual strategic patch. The prior checker compared whole live pod templates,
where Kubernetes-injected fields such as default `restartPolicy`,
`terminationMessagePolicy`, container settings, and StatefulSet defaults can
create false positives.

The revised gate compares the **chart-declared** subset of the proposed
fields with live Kubernetes values; it ignores additional Kubernetes-injected
keys and matches named container/environment lists by `name` rather than
array position. It retains fail-closed behavior for changes to declared
images, env, replica fields, container settings and other authored fields,
as well as protected Secret data/type, PVC and StatefulSet declared fields.
Additional synthetic tests cover defaults, reordered env entries and
genuine env drift. Operator verification is pending.

This change intentionally does **not** claim a precise Helm upgrade patch,
nor does ignoring default-only differences prove the upgrade safe.
A genuine live image or env mismatch remains a blocker until the exact
three-way merge effect is verified or the drift is explicitly reconciled.
No live resource mutation, bulk reconciliation or actual Helm revision-2
upgrade has been performed.

## Kubernetes strategic three-way patch calculator (draft; operator test needed)

A separate Go module `infrastructure/tools/helm-patch-impact` now uses
`k8s.io/apimachinery/pkg/util/strategicpatch.CreateThreeWayMergePatch`
with typed Kubernetes patch metadata for the chart's built-in object
kinds (Deployment, StatefulSet, Service, ServiceAccount, Secret,
ConfigMap, PVC, Ingress, Role, RoleBinding). The call uses stored Helm
revision 1, proposed Helm revision 2 and current live JSON with the
`overwrite=true` argument Helm uses for built-in objects. The tool
**never sends a patch** to Kubernetes, never prints patch JSON, and
classifies only resource names with nonempty patches. Any nonempty
patch, including one on research-engine, blocks release pending human
review. This calculation is substantially more faithful than the
previous authored-field heuristic, though it is not a fully integrated
Helm operation or a test of admission controllers and hooks.

On the trusted Windows workstation, fetch PR #24's latest version
of these files before running:

- `infrastructure/tools/helm-patch-impact/go.mod`
- `infrastructure/tools/helm-patch-impact/main.go`
- `infrastructure/tools/helm-patch-impact/main_test.go`
- `infrastructure/scripts/build_helm_patch_calculator.ps1`
- `infrastructure/scripts/helm_k3d_strategic_patch.py`

Use `infrastructure/scripts/build_helm_patch_calculator.ps1` to run
the pure Go tests and compile a Windows executable under
`.tmp/helm-patch-impact`. The build container has network access
for Go module downloads but mounts **source code and .tmp output only**
(no live kubeconfig, backups, environment or Kubernetes manifest data).
The locally compiled Windows binary does not require network.
Then run `infrastructure/scripts/helm_k3d_strategic_patch.py` using
the existing project Python venv. It obtains the three documents via
the previously verified read-only internal k3d/Helm calls and passes
them to the Go calculator through a stdin pipe. The calculator prints
only counts and safe resource identity labels; raw patch contents,
Secret data and command stderr are suppressed.

**This is a hard STOP gate, not an automatic reconciliation tool.**
A nonempty patch to an unrelated workload or protected Secret/StatefulSet
must be reviewed before any Helm upgrade. This tool does not change
Helm history, live workloads, PostgreSQL, Secrets, or PVCs; the 71-table
isolated PostgreSQL restore test is already complete.

## Operator's first strategic-patch result and safe category review

Windows operator built the Go module successfully. The 54-resource
Kubernetes strategic merge evaluation found **21 nonempty patches**:
`Deployment/research-engine`, 17 unrelated Deployments, plus the
`aip-auth-jwt`, `dev-postgres` and `dev-searxng` Secrets. The cluster
was unchanged. This does **not** establish a Secret credential rotation:
the helper previously verified unchanged Postgres/SearXNG encoded data,
but a patch may still touch metadata, data/stringData representation or
another field. An unrelated Deployment patch may or may not affect pod
template rollout.

A new `categories.go` classifier reports **only hard-coded patch
category labels**, including `container-image`, `container-env`,
`metadata`, `secret-data-or-config-data` and `secret-stringData`;
it does not print patch JSON, credential values or arbitrary patch keys.
`helm_k3d_strategic_patch.py` validates that categories belong to
the exact 54-resource result and displays their labels. Three
synthetic tests cover confidential value redaction and classification.

After fetching the updated Go source and Python wrapper, the operator
must **rebuild the local Windows executable** with
`build_helm_patch_calculator.ps1` before rerunning the strategic patch
wrapper. Otherwise the old binary lacks the new report fields. A blocked
result is expected; this reports *why* and is not release authorization.

**Do not overwrite running settings or rotate Secrets** in order to
suppress warnings. Patch categories require explicit reviewer approval
and appropriate reconciliation before upgrade.

## Fail-closed write-only Secret patch validation (operator test pending)

The first strategic merge report showed exactly `secret-stringData` for
`aip-auth-jwt`, `dev-postgres` and `dev-searxng`. A category alone does
**not** prove harmlessness. `secret_audit.go` now exempts a Secret from
the unresolved risk count ONLY when all these hold:

1. The Secret is precisely one of those three reviewed names.
2. The **complete** strategic patch is exactly `{"stringData":null}`
   (no metadata, type, data, or other operations).
3. All encoded `data` fields are canonical valid base64.
4. All effective decoded Secret bytes, keys, and Secret types match
   across stored Helm v1, candidate v2, and the live Secret.
5. No ambiguous duplicate keys between `data` and `stringData`.

Any failed check remains a regular blocking risk. The report records
`VERIFIED_SECRET_NOOP` names without revealing any Secret bytes.
New Go unit tests cover successful write-only deletion and malicious
changes to bytes, Secret type, patch shape, invalid base64 and ambiguous
representations. This change **does not** relax the 17 unrelated
Deployment risks or the research-engine field audit. No live write or
Helm upgrade is performed.

After fetching all changed Go files plus the updated Python wrapper,
rerun the existing isolated build script and strategic patch checker.
The exact results have **not yet been observed** on the operator's
Windows environment.

## JWT non-verification and research-engine patch diagnostics (pending operator validation)

The prior operator run passed Go tests and verified two harmless stringData-only
Secret patches, `dev-postgres` and `dev-searxng`, but the third Secret
`aip-auth-jwt` did **not** pass. Unresolved patches remain **17 unrelated
Deployments, JWT Secret, and research-engine**.

The patch checker now emits a **fixed JWT failure code** instead of a
generic Secret risk. The code can be `PATCH_SHAPE`,
`OLD_DATA_INVALID`, `NEW_DATA_INVALID`, `LIVE_DATA_INVALID`,
`TYPE_MISMATCH`, `OLD_TO_NEW_BYTES_MISMATCH`,
`NEW_TO_LIVE_BYTES_MISMATCH` or `VERIFIED`. No credential values,
Secret key names, hashes, full patches or exception strings are exposed.

It also audits the **actual research-engine strategic patch** and emits
only fixed diagnostic labels such as `APPROVED_ENV_NAME`,
`OTHER_ENV_NAME`, `ENV_VALUE_PATCH`,
`OTHER_CONTAINER_FIELD`, `OTHER_POD_SPEC`,
`OTHER_DEPLOYMENT_SPEC`, `STRATEGIC_LIST_DIRECTIVE`,
`CONTAINER_IMAGE`, and `OTHER_TOP_LEVEL`.
This is a *path-family audit*, not a full value-by-value authorization.
Recognizing an allowed environment-variable name does not validate its
value, and any unapproved pod-template or strategic list directive remains
a stop condition. The exact research patch still requires semantic
review, and the 17 unrelated deployments must be reconciled separately.

Regression tests were added for the codes and redaction. Operator must
fetch `secret_audit.go`, `research_audit.go`, `main.go`,
`audit_diagnostics_test.go`, and updated
`helm_k3d_strategic_patch.py`, then rebuild the executable before the
read-only check. **No deployment permission is implied by any diagnostic
output**, and no Kubernetes write is included.

## Windows executable launch refusal and isolated Linux execution (operator verification pending)

The operator's Windows Python wrapper produced `PATCH_DIAGNOSTIC_PHASE:
EXECUTE_CALCULATOR`, `PATCH_DIAGNOSTIC_KIND: OS_ERROR`, and
`PATCH_OS_ERROR_CODE: WINERROR_4551` while starting the previously
compiled `.exe`. This prevented any new patch calculation; the earlier
successful 21-patch output remains the last verified risk baseline.

The build now runs Go unit tests and compiles a **static Linux** executable
`.tmp/helm-patch-impact/helm-patch-impact-linux` instead of a Windows EXE.
The Python wrapper executes it via `docker run --rm -i --network none
--read-only --cap-drop ALL --security-opt no-new-privileges
--user 65534:65534`, with **only the Linux executable** bind-mounted
read-only and the 54-resource input sent over stdin. Neither kubeconfig
nor backup/Secret files are mounted into the calculator container. The
Docker image is the already-approved `golang:1.23-alpine`; Docker itself
must remain accessible on the Windows host. This avoids trying to launch
the Windows EXE directly and does not call the Kubernetes write API.

The diagnostic's `OSError` error-code helper was corrected to use
`isinstance`, covering Python's `FileNotFoundError` subclass;
offline tests also verify the isolated Docker command. The updated build
and wrapper have **not yet been operator-tested**. A successful invocation
will still hard-block revision 2 on any unapproved three-way patch. No
release, Secret, database, PVC or live pod changes are made.

## Operator: Linux patch checker completed, exact JWT shape and research env diagnostics pending

The operator's Linux calculator completed successfully (Go tests and
9/9 Python wrapper tests passed). It evaluated 54 resources with 21
nonempty patches. PostgreSQL and SearXNG Secrets were verified safe
stringData-only no-ops. The JWT Secret was **not** verified:
`JWT_SECRET_PATCH_AUDIT: PATCH_SHAPE`. Research engine showed
`ENV_VALUE_PATCH`, `OTHER_ENV_NAME`, and
`STRATEGIC_LIST_DIRECTIVE`. The unrelated 17 Deployment patches
remain blockers. There were no live writes.

Added an additional read-only patch-shape classifier for JWT that emits
only a fixed code distinguishing a whole-object stringData removal
from individual key deletions, write operations, or combined patch
operations; it never prints field names or values. This is
**diagnostic only**, and does not expand the strict Secret no-op
exemption. Also added the research env-list audit to separate actual
approved/unapproved env edit objects from order-only directives,
using fixed output labels and no environment variable names or values.
The existing research patch remains **unapproved** even if only
approved names are reported, because values and unrelated patch
operations still require review.

The new Go build and diagnostics are **not yet operator-tested**.
Fetch updated `main.go`, `secret_audit.go`,
`research_audit.go`, `patch_shape_test.go` and
`helm_k3d_strategic_patch.py`; rebuild the isolated Linux checker
and rerun the read-only wrapper. No upgrade or live mutation.

## Consolidated release reconciliation: decision and implementation boundary

This is the release engineering checkpoint after the operator's final read-only
Linux patch-calculator run (54 resources, 21 nonempty strategic patches):

* **JWT Secret/aip-auth-jwt: BLOCKED.** Its exact patch is classified
  `STRINGDATA_OBJECT_VALUES`, **not** a write-only `stringData:null`
  deletion. No exemption or rotation is safe. Fix at the chart/values
  rendering layer so candidate desired values and live JWT credentials are
  byte-equivalent, **without writing the credential to Git, logging it,
  copying it into a Helm override, or creating a new JWT**.
* **Deployment/research-engine: BLOCKED.** Actual patch contains
  `UNAPPROVED_ENV_PATCH_ENTRY` and env/container ordering directives,
  not merely the five reviewed research flags. Determine why Helm's
  three-way merge is reconciling other settings; preserve the approved
  live image, paused recovery, disabled scheduler and manual flags.
  A name-only list directive is not automatically a harmless patch.
* **17 unrelated Deployments: BLOCKED**:
  api-gateway, auth-service, broker-service, company-service, frontend,
  ibkr-connector, kafka, mcp-gateway, notification-service,
  portfolio-optimizer, portfolio-service, ranking-engine,
  recommendation-service, research-service, risk-service,
  valuation-engine, yahoo-finance-mcp. Their strategic patches touch
  images, env, container or pod configuration. **No automatic adoption
  of live values** into chart, no blind rollback to stored v1 values.
  The owner must review each intended config difference; author approved
  values as chart/Helm release inputs and calculate all 54 three-way
  patches against the same old/new/live baseline.
* **Database-related Secrets** dev-postgres and dev-searxng are independently
  verified byte-preserving `stringData` no-op patches. **PostgreSQL
  StatefulSet and PVC are not to be modified**, and no database recovery
  test needs repeating (isolated PostgreSQL 16 restore yielded 71 tables).
* **Deployment decision:** DO NOT RUN `helm upgrade` while any unreviewed
  strategic patch remains. PR #24 is draft and is not deployment authorization.

**Efficiency change:** the Go calculator build script now persists module
downloads and compiled object cache only under local `.tmp/helm-patch-impact`.
This avoids repeated Go module downloads during future engineering revisions.
The build container still mounts only local tool source/cache/output—no
Kubernetes credentials or database files. This is an implementation change
in PR #24; it has not yet been operator-run after the caching update.

**Stop point:** no more operator diagnostics solely to rediscover the above
21 patches. The next meaningful action is a separately reviewed chart/value
reconciliation commit for each affected field, followed by one final exact
patch run. There is no safe blanket automatic fix based only on category
labels because they intentionally omit the values and operations necessary
to authorize the changes.

## Single consolidated, non-applicable reconciliation feasibility preview

Added `infrastructure/scripts/helm_k3d_reconciliation_preview.py` and
synthetic tests. **This is an executable in-memory feasibility experiment,
not a Helm override, post-renderer, approved values migration, or
production patch.** It obtains the same 54 old/new/live objects as the
previous working read-only gate, hypothetically copies only the live
`spec` fields of the exact 17 listed unrelated Deployments into the
candidate manifest **in process memory**, then sends all 54 triples to
the existing Kubernetes strategic merge calculator in an isolated Linux
container. It prints only total counts (no config, Secret, image, env
values or patch JSON), and ALWAYS exits nonzero. The original candidate,
JWT Secret and research-engine remain unchanged; there is no Helm upgrade
or Kubernetes write path. This allows us to determine whether preserving
the existing live Deployment specs would even avoid those 17 patches,
before engineering reviewed individual chart values. It does **not**
authorize copying those specs into a stored release, and may include
Kubernetes defaulted fields that would be unsuitable for a real chart.
The preview script has not been run on the operator's live cluster.

The active Go build now has persistent module/build caches under ignored
`.tmp`. The next proper review must reconcile exact deployment
settings in authored chart values and solve the JWT write operation,
while preserving credentials entirely outside Git and command-line
arguments. Only then use the original strict 54-resource gate and
obtain explicit approval for an upgrade. Do not run this preview as an
upgrade pipeline step; its output is an experiment only.

## Actual JWT Helm source reconciliation implemented (operator tests pending)

The JWT problem was traced to a real template defect in
`templates/dev-dependencies.yaml`: unlike PostgreSQL and SearXNG,
the development JWT Secret was rendered from
`auth.devJwtSecret` directly under `stringData`. When that stored
Helm value was stale or blank, the new candidate attempted a
`STRINGDATA_OBJECT_VALUES` write, risking credential replacement.

The JWT now uses the **existing, fail-closed** `aip.devCredential`
helper to read the already persisted JWT key from the live Secret,
validate its type, key, and canonical base64, and render the same
bytes under `data`. The helper fails on missing/invalid live Secret
during upgrade; the value supplied in `auth.devJwtSecret` is a
bootstrap-only input for fresh installations. This change does not
write credentials into Git, CLI flags, logs or local files.

The scope gate has a JWT-specific exception for stale stored Helm
`stringData` only when the newly rendered JWT `data` is exactly
equal to the live Secret's complete effective data and type, with
unchanged metadata. The Go strategic-patch gate independently
recognizes a JWT-only write as credential-preserving only if the
**entire patch** consists of `stringData:null` plus `data` entries
whose decoded bytes equal live values, with no deletions, unknown
keys, metadata changes or type changes. All other Secret operations
remain blocked, as do changes to PostgreSQL/SearXNG bytes. Added
synthetic Go tests rejecting changed bytes, deletion, wrong keys and
metadata operations, plus chart-level static tests to prevent a
regression to direct JWT stringData templating.

**Validation status:** these new edits are committed to draft PR #24
but have **not yet been run through operator's Windows/cluster
validator**. No upgrade is authorized; the unreviewed research-engine
env patch and 17 unrelated Deployment patches still block revision 2.
Do not rerun a full Go build until the remaining chart/value
reconciliation is prepared.

## Unrelated Deployment actual-spec-effect guard (pending operator validation)

The Go strategic calculator now checks every nonempty patch for the **17
specifically identified unrelated Deployments** by applying it to the current
live resource *in memory* via Kubernetes strategic merge. It exempts a
Deployment from the **spec-change risk** list only if:

1. The patch has exactly one top-level key, `spec`, with a non-null value.
2. The patch can be applied by Kubernetes' typed strategic merge implementation.
3. The resulting entire `spec` is structurally identical to the original
   live `spec`, including pod template, image, environment, replicas,
   storage, scheduling, and rollout fields.
4. The Deployment is one of the 17 explicitly allowlisted resource names;
   research-engine and other kinds are never exempted.

An image tag, env, replica, security, or any other spec change therefore
remains blocked even if a report labels the patch as a strategic-list
operation. This test handles benign nonempty patches that reassert exactly
the same current values. **Important limitation:** a spec-identical strategic
patch does not prove Helm will make zero API requests, and admission
webhooks or concurrent updates can still affect upgrade safety. It does
not alter the rendered chart, use a post-renderer, or approve the release.
All original stored-v1/proposed-v2/live objects are used without
modification. The Python wrapper now validates the 17-name allowlist and
reports only `VERIFIED_DEPLOYMENT_SPEC_NOOP_PATCHES` and resource names.

Synthetic Go tests reject changed image, environment, replica, metadata
and actual Helm-style image update patches. This code has **not yet been
operator-tested**. JWT live-preserving chart change is also awaiting final
validation. Keep PR #24 draft and the release blocked until the new
checker runs and all remaining patch effects are independently reviewed.
