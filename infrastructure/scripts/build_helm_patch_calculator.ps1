# Compile static Linux calculator; execute later inside networkless Docker.
# Avoid Windows executable launch restrictions (operator observed WinError 4551).
# No kubeconfig or sensitive manifests enter the build container.
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$module = (Resolve-Path (Join-Path $root 'infrastructure/tools/helm-patch-impact')).Path
$out = Join-Path $root '.tmp/helm-patch-impact'
$modCache = Join-Path $out 'gomodcache'
$buildCache = Join-Path $out 'gobuildcache'
New-Item -ItemType Directory -Path $out,$modCache,$buildCache -Force | Out-Null
Write-Host 'Building Kubernetes strategic patch calculator (Go dependencies only)...'
# Source is read-only; Go dependency and build caches persist under .tmp
# to avoid downloading the same Kubernetes modules on every validator revision.
# No credentials, database, backups or Kubernetes manifests are mounted.
$shell = 'mkdir -p /tmp/module; cp /source/go.mod /source/*.go /tmp/module/; cd /tmp/module; go mod tidy; go test ./...; CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -o /out/helm-patch-impact-linux .'
docker run --rm --mount "type=bind,source=$module,target=/source,readonly" --mount "type=bind,source=$out,target=/out" --mount "type=bind,source=$modCache,target=/go/pkg/mod" --mount "type=bind,source=$buildCache,target=/root/.cache/go-build" --entrypoint sh golang:1.23-alpine -ec $shell
if ($LASTEXITCODE -ne 0) { throw 'GO_STRATEGIC_PATCH_BUILD_OR_TEST_FAILED' }
$exe = Join-Path $out 'helm-patch-impact-linux'
if (-not (Test-Path $exe)) { throw 'GO_STRATEGIC_PATCH_LINUX_BINARY_MISSING' }
Write-Host 'GO_STRATEGIC_PATCH_LINUX_BUILD_AND_TEST_PASSED'
Write-Host 'NEXT: .\.venv\Scripts\python.exe infrastructure\scripts\helm_k3d_strategic_patch.py'
