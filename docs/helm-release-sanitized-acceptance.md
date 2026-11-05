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
