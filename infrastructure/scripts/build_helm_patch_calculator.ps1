# Compile and test Kubernetes strategic patch calculator without mounting kubeconfig.
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$module = (Resolve-Path (Join-Path $root 'infrastructure/tools/helm-patch-impact')).Path
$out = Join-Path $root '.tmp/helm-patch-impact'
New-Item -ItemType Directory -Path $out -Force | Out-Null
Write-Host 'Building Kubernetes strategic patch calculator (Go dependencies only)...'
# Source is read-only; module download uses disposable container filesystem.
# No credentials, database, backups or Kubernetes manifests are mounted.
$shell = 'mkdir -p /tmp/module; cp /source/go.mod /source/*.go /tmp/module/; cd /tmp/module; go mod tidy; go test ./...; CGO_ENABLED=0 GOOS=windows GOARCH=amd64 go build -o /out/helm-patch-impact.exe .'
docker run --rm --mount "type=bind,source=$module,target=/source,readonly" --mount "type=bind,source=$out,target=/out" --entrypoint sh golang:1.23-alpine -ec $shell
if ($LASTEXITCODE -ne 0) { throw 'GO_STRATEGIC_PATCH_BUILD_OR_TEST_FAILED' }
$exe = Join-Path $out 'helm-patch-impact.exe'
if (-not (Test-Path $exe)) { throw 'GO_STRATEGIC_PATCH_EXECUTABLE_MISSING' }
Write-Host 'GO_STRATEGIC_PATCH_BUILD_AND_TEST_PASSED'
Write-Host 'NEXT: .\.venv\Scripts\python.exe infrastructure\scripts\helm_k3d_strategic_patch.py'
