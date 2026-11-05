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
