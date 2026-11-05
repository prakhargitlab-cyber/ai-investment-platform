# Read-only Helm access to the existing k3d cluster without published host ports.
[CmdletBinding()]
param(
    [string]$ServerContainer = "k3d-ai-investment-dev-server-0",
    [string]$Namespace = "ai-investment",
    [string]$ReleaseName = "ai-investment-platform",
    [string]$HelmImage = "alpine/helm:4.2.3"
)
$ErrorActionPreference = "Stop"
$tempDirectory = Join-Path ([IO.Path]::GetTempPath()) ("aip-helm-preflight-" + [Guid]::NewGuid().ToString("N"))
$config = Join-Path $tempDirectory "kubeconfig"
$apiConfig = "/etc/rancher/k3s/k3s.yaml"
function Assert-Exit([string]$Message) {
    if ($LASTEXITCODE -ne 0) { throw $Message }
}
try {
    # Never use the Windows default kubeconfig (which may point at an old AKS cluster).
    $running = (& docker inspect $ServerContainer --format "{{.State.Running}}") -join ""
    Assert-Exit "K3D_SERVER_INSPECTION_FAILED"
    if ($running.Trim() -ne "true") { throw "K3D_SERVER_NOT_RUNNING" }

    $health = (& docker exec $ServerContainer /bin/kubectl --kubeconfig=$apiConfig get --raw=/readyz) -join ""
    Assert-Exit "K3D_INTERNAL_API_UNAVAILABLE"
    if ($health.Trim() -ne "ok") { throw "K3D_INTERNAL_API_UNHEALTHY" }

    # Docker cp tries to copy the k3s kubeconfig symlink, failing on some Windows hosts.
    # cat follows that symlink INSIDE Linux; do not print the resulting credentials.
    $lines = @(& docker exec $ServerContainer /bin/cat $apiConfig)
    Assert-Exit "K3D_KUBECONFIG_READ_FAILED"
    $configText = ($lines -join [Environment]::NewLine) + [Environment]::NewLine
    if ($configText -notmatch '(?m)^\s*server:\s*https://127\.0\.0\.1:6443\s*$') {
        throw "UNEXPECTED_INTERNAL_KUBERNETES_ENDPOINT"
    }
    New-Item -ItemType Directory -Path $tempDirectory -Force | Out-Null
    [IO.File]::WriteAllText($config, $configText, (New-Object Text.UTF8Encoding($false)))
    $configText = $null
    $lines = $null

    $mount = "type=bind,source=$config,target=/tmp/kubeconfig,readonly"
    Write-Host "Checking Helm through the internal Kubernetes API (read-only)..."
    & docker run --rm --network "container:$ServerContainer" --mount $mount --entrypoint helm $HelmImage --kubeconfig /tmp/kubeconfig list --namespace $Namespace
    Assert-Exit "INTERNAL_HELM_LIST_FAILED"
    & docker run --rm --network "container:$ServerContainer" --mount $mount --entrypoint helm $HelmImage --kubeconfig /tmp/kubeconfig history $ReleaseName --namespace $Namespace
    Assert-Exit "INTERNAL_HELM_HISTORY_FAILED"
    Write-Host "READ_ONLY_HELM_PREFLIGHT_PASSED"
}
finally {
    # Contains admin kubeconfig credentials: always remove the temporary copy.
    $configText = $null
    $lines = $null
    if (Test-Path -LiteralPath $tempDirectory) {
        Remove-Item -LiteralPath $tempDirectory -Force -Recurse -ErrorAction SilentlyContinue
    }
}
