# Preserve original k3d OCI indexes in the local registry

## Verified diagnosis

The k3d node retains complete original images in containerd, including the
`linux/amd64` application manifest and the `unknown/unknown` attestation
manifest. The original `api-gateway` OCI index digest is
`sha256:63a8c916c63745d172dc8e6635c1b89ab80ffdc08f0225f0382d2ee0139cb2a3`.

Both regular and `--local` containerd pushes remained at 0 bytes with no
`PATCH` or `PUT` after the registry's successful `POST 202`.
A Windows PowerShell 5.1 test independently uploaded the bytes
`registry-test` via registry `POST` then `PUT`, yielding HTTP 201 with a
matching SHA-256 digest. OCI export from the original node image succeeded
using `ctr images export --local --all-platforms --skip-manifest-json`.
Its `index.json` referenced the exact original index, and its blob folder
contained 15 entries. **The 15-count alone is not an integrity proof.**

## Fix proposed by PR #27

`infrastructure/scripts/recover_k3d_registry_image.ps1` defaults to read-only
preflight. With `-Publish`, it:

1. Checks the original containerd image's approved digest and complete content.
2. Exports the image, including its attestation, into an OCI archive on the k3d node.
3. Copies the archive to a unique, retained local audit directory and extracts it.
4. Checks that the archive index references the original digest, verifies
   each blob's SHA-256 and every descriptor size, and checks leaf dependencies.
5. Uploads missing *config and layer blobs* through the tested registry
   `POST` + `PUT` API; checks each resulting digest.
6. Publishes child OCI manifests **by digest**, then the original OCI index
   **by tag**, verifying the registry's content digest at each stage.

Uses Windows PowerShell 5.1 APIs; requires `docker`, `tar.exe`, and
`Get-FileHash`. It does not rebuild or overwrite images, clear caches, restart
services, edit Kubernetes resources, or call Helm. Do not expose the full
upload `Location` URL in logs: its query string holds session state.

### Test exactly one image

Run from the repository root, in a checkout containing PR #27:

```powershell
.\infrastructure\scripts\recover_k3d_registry_image.ps1 -Service api-gateway
.\infrastructure\scripts\recover_k3d_registry_image.ps1 -Service api-gateway -Publish
```

Demand `REGISTRY_IMAGE_RECOVERED_AND_DIGEST_VERIFIED: api-gateway` before
continuing. This new archive-based method has NOT YET completed end-to-end
in the user's environment; therefore PR #27 remains a draft and no bulk
image recovery is authorized by its CI checks.

After successful one-image recovery, repeat for each of the other 15
allowlisted services, one at a time, stopping at the first failure.

## Separate Helm release gate

Image recovery is not an application rollout and does not authorize PR #24
merge, Helm upgrade, or modifications to protected Secrets, PVCs or
Deployments. Re-run full release safety and rollback pullability gates
before any Helm change.
