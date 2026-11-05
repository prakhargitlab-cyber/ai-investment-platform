# Cross-platform deployment program (stage 1)

The release guard uses Python 3.10+ standard library with equivalent Windows
PowerShell and Linux Bash entrypoints. It operates on **local k3d** now. The
native mode deliberately refuses to treat Azure/AKS/EKS/OpenShift as ready
until registry authentication, context isolation, permissions and target profiles
have been implemented and tested.

## Why we are doing this

The latest release candidate is PR #28, fixing origin-safe password-reset links
and local CORS for auth-service and api-gateway. On November 5, 2026, Java
tests passed locally and in CI. New image index digests were pushed and
independently verified at localhost:5001 with immutable tag
auth-reset-pr28-20261105-145100. Helm remains **revision 1**. The live
research-engine uses its separate newer ETF image, which must not be rolled
back or restarted accidentally.

## Stage 1: read-only gates (this PR)

1. Require an explicit release specification containing namespace, release,
   revision, origin allowlist, two service names and exact OCI index digests.
2. Verify both immutable images exist in the registry at their expected digests.
3. Query Kubernetes explicitly in the k3d server node. Never rely on the
   operator's default kubeconfig, which may point to AKS.
4. Verify exactly one deployed Helm revision, and matching Helm ownership,
   desired workgroup, readiness and update state.
5. Verify the post-release images and both allowed-origin configurations.
6. Print only sanitized statuses, never cluster Secret contents, kubeconfig,
   bearer tokens or rendered Helm manifests.

### Windows

From the PR worktree root:

```powershell
.\infrastructure\deploy\deploy.ps1 -Action plan
```

### Linux

```bash
bash infrastructure/deploy/deploy.sh plan
```

A successful PLAN is **not a release authorization**. It prints
NO_DEPLOYMENT_AUTHORIZATION because Helm may still modify fields outside these
two services due to revision-1 drift. It does not issue any write operation.

After a separately approved Helm revision-2 deployment, either wrapper
can run `verify` to require new images, origins and rollout readiness;
post-release UI verification remains mandatory.

## Stage 2: work still required before deployment

- Reconcile the exact Helm-managed live resources with revision-1 manifests
  and PR #28 desired values.
- Calculate and review the **real Helm three-way patch**, not only
  the rendered template diff.
- Require that the only permitted writes are the two intended Deployments:
  their new image references and approved origin-related environment entries.
- Block any Secret, PVC, StatefulSet, research-engine or unrelated change.
- Use an operator-approved atomic upgrade with a known pullable rollback
  revision; verify actual image IDs and healthy application probes.
- Run CORS preflight and password-reset acceptance through both browser URLs;
  do not expose real reset tokens or SMTP credentials in logs.

The current release guard intentionally has no apply command, so nobody can
mistake a partial preflight for authorization to modify the live cluster.

## Future Azure Linux target

Keep the same Python core, but introduce an explicit Azure target profile
with pinned kube-context, Azure Container Registry auth via workload identity,
TLS verification, Key Vault integration, private networking, and backup policy.
Do not reuse localhost:5001, local k3d node names, or insecure HTTP for Azure.

## Current Helm upgrade diagnostic (November 5, 2026)

The original Helm revision 1 is healthy and PR #28 images are published. The
upgrade dry run still fails before any patch calculation. Both
`--reuse-values` and `--reset-values` dry runs failed. Independent
`helm template` succeeds. This **does not prove a stored-values defect**:
the chart's `aip.devCredential` template helper uses
`.Release.IsUpgrade` and mandatory Kubernetes Secret lookups, whereas
an independent template render defaults to an installation context.

The impact audit now verifies that the protected credential Secrets have
their expected structural data keys, and extracts only categorized YAML
error signatures, numeric error lines, and safe template basenames.
All output is read-only and Secret values remain hidden. Do not try
`helm upgrade --reset-values` as a workaround: it could replace important
live configuration. Neither Helm revision 2 nor an Azure profile is
authorized until the exact patch is classified and reviewed.

Stage-2 full deployment requirements still include a fresh-cluster installer,
an upgrade path, and Azure Linux profile with secure registry and secret
management. The present wrappers expose only read-only `plan` and `verify`.

## Strict two-Deployment simulation (read-only)

The full Helm upgrade candidate for auth reset correctly rendered but would
patch 21 resources, including three Secrets and the pinned research-engine.
Do **not** apply that candidate. To distinguish unrelated release drift from
the necessary auth changes, run the same audit with
`--simulate-scoped-candidate`. This reads the actual Helm preview and Helm
revision-1 manifest, and creates an **in-memory simulation** that retains all
non-target manifests exactly as recorded in revision 1. For auth-service and
api-gateway it copies only the approved image and origin environment value,
and verifies both against the immutable release specification. It then invokes
the existing real strategic three-way patch calculator with the live objects.

The simulation is **not** a Helm post-renderer and cannot be deployed. It
does not authorize an upgrade, even when only two Deployment patches remain.
A production-safe Helm post-renderer or equivalently strict release mechanism
and an end-to-end apply rehearsal are separate required deliverables.

## PR #28: scoped Kubernetes plan when full Helm upgrade is unsafe

The actual full Helm candidate would patch 21 managed resources; an in-memory
two-service-only Helm manifest simulation still yielded 19 unrelated
reconciliation patches, even though 52 of 54 desired manifests were unchanged.
The whole-object live-vs-old comparison includes server-owned metadata, so it
does not establish 54 meaningful workload drifts.

Use the **read-only** `target_patch_plan.py` to inspect live Helm-owned
`auth-service` and `api-gateway` fields directly. It checks the existing
pinned research-engine before issuing any target plan:

```powershell
& .\.venv\Scripts\python.exe .\.tmp\deploy-pr29\infrastructure\deploy\target_patch_plan.py --spec .\.tmp\deploy-pr29\infrastructure\deploy\releases\auth-reset-pr28.k3d.json
```

This is neither a Helm upgrade nor a Kubernetes patch. Do not use
`kubectl set image` or `kubectl set env` without validated compare-and-swap
resource-version guards and read-back verification. For the immediate
two-service fix, the forthcoming apply tool must reject arbitrary workloads,
pin image digests, and avoid modifying unrelated Helm or cluster objects.
It must document that Helm revision will remain unchanged, with rebase of
release ownership/state handled separately.
