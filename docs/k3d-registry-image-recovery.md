# Recover existing k3d images into the local registry

Scope: **LOCAL k3d only**. This procedure recovers 16 first-party images that are
complete in the node's containerd cache but absent from `localhost:5001`.
It does not build images, change Deployments, deploy Helm, modify PostgreSQL,
or delete any images.

## Root cause and fix

The registry accepted blob-upload sessions (`202 Accepted`), but the first
`ctr images push` did not complete. The earlier diagnostic command containing
`--http-trace` was rejected because containerd v2.2.3-k3s1 requires
`--local` alongside that flag. The recovery script therefore uses
`ctr images push --local --plain-http`. This is a **candidate fix** pending
successful execution in the actual cluster, not a claim of verified registry
recovery. The script intentionally avoids `--http-trace` because it may reveal
upload-session state.

The registry generates upload `Location` URLs from the supplied `Host`
header. Requests made with the internal `ai-investment-registry:5000` Host
produced the correct internal URL, so changing the registry configuration is
not justified by the current evidence.

## Use from Windows PowerShell

Run after reviewing and merging this PR, or execute from an isolated checkout.
No new PowerShell YAML modules are required.

1. Preflight for one workload (read-only):

   ```powershell
   .\infrastructure\scripts\recover_k3d_registry_image.ps1 -Service api-gateway
   ```

2. Recover that **one** image and verify its original OCI index digest:

   ```powershell
   .\infrastructure\scripts\recover_k3d_registry_image.ps1 -Service api-gateway -Publish
   ```

   A successful result prints `REGISTRY_IMAGE_RECOVERED_AND_DIGEST_VERIFIED`.
   Any error stops recovery. Do not proceed to additional images if it fails.

3. Once the single-image recovery succeeds, run one image at a time, stopping
   on the first failure:

   ```powershell
   $services = @(
       'auth-service','broker-service','company-service','frontend',
       'ibkr-connector','mcp-gateway','notification-service',
       'portfolio-optimizer','portfolio-service','ranking-engine',
       'recommendation-service','research-service','risk-service',
       'valuation-engine','yahoo-finance-mcp'
   )
   foreach ($service in $services) {
       .\infrastructure\scripts\recover_k3d_registry_image.ps1 -Service $service -Publish
       if (-not $?) { throw "Recovery failed for $service" }
   }
   ```

The script pins the `dev-20261103-152626` tag and exact OCI index digests
observed in containerd. It refuses digest mismatches or incomplete local
content; it never overwrites a different existing registry digest. Its
default invocation is read-only, and `-Publish` creates a local image alias
and pushes cached content to the internal registry.

## Release acceptance is separate

After recovery, verify every image tag and digest from the host, then run the
sanitized Helm impact gates and actual three-way strategic patch checker.
The original Helm reconciliation remains blocked until its own authorization
conditions are fulfilled. An image registry push does **not** authorize
`helm upgrade`, PR #24 merge, or a Kubernetes rollout.

The image recovery deliberately does not attempt to make immutable digest
promises about unrelated container images, Kafka, database migrations, or
storage. These require their own release checks.
