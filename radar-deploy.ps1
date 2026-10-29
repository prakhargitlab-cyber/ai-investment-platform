#Requires -Version 5.1
<#
.SYNOPSIS
  Independent, guarded deploy utility for the AI Investment Platform Radar.
.DESCRIPTION
  Does not invoke or modify platform.ps1. Default action is Plan (read-only).
  Existing deployments are updated via kubectl set image ONLY for chosen workloads.
  Fresh installation is an explicitly approved Helm install into an EMPTY namespace.
  No database migrations, cluster creation/deletion, or Radar cycles are triggered explicitly.
  Note: application startup can run its own migrations; inspect that before deployment.
.EXAMPLE
  .\radar-deploy.ps1 -Action Plan
.EXAMPLE
  .\radar-deploy.ps1 -Action Build -Components frontend,research-engine
.EXAMPLE
  .\radar-deploy.ps1 -Action Push -Tag radar-20261015-01 -ApprovePush
.EXAMPLE
  .\radar-deploy.ps1 -Action Deploy -Tag radar-20261015-01 -ApproveDeploy
.EXAMPLE
  .\radar-deploy.ps1 -Action Fresh -Tag radar-20261015-01 -ApproveFresh
#>
[CmdletBinding()]
param(
    [ValidateSet('Plan','Build','Push','Deploy','Fresh')][string]$Action = 'Plan',
    [ValidateSet('frontend','research-engine')][string[]]$Components = @('frontend','research-engine'),
    [string]$Tag = '',
    [string]$Namespace = 'ai-investment',
    [string]$Release = 'ai-investment-platform',
    [string]$Registry = 'localhost:5001',
    # k3d's registries.create.name from infrastructure/k3d/cluster-dev.yaml,
    # on the registry container's own internal port (always 5000 for a
    # k3d-managed registry, independent of hostPort). This is the address
    # containerd on each cluster node actually resolves and pulls from --
    # $Registry (localhost:5001) only exists from the HOST's Docker daemon
    # and is not reachable from inside the cluster, which is exactly what
    # produced the observed ImagePullBackOff/connection-refused.
    [string]$ClusterRegistry = 'ai-investment-registry:5000',
    [string]$Kubeconfig = '',
    [switch]$ApprovePush,
    [switch]$ApproveDeploy,
    [switch]$ApproveFresh,
    [switch]$EnableDevLoginBuild,
    [string]$ApiBaseUrl = '',
    [string]$FreshValuesFile = ''
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$chart = Join-Path $root 'infrastructure/helm/ai-investment-platform'
$devValues = Join-Path $chart 'values-dev.yaml'
$frontendContext = Join-Path $root 'frontend'
$researchContext = Join-Path $root 'ai/research-engine'
$components = @($Components | Select-Object -Unique)

function Run([string]$Command, [string[]]$Arguments) {
    Write-Host ('> ' + $Command + ' ' + ($Arguments -join ' ')) -ForegroundColor Cyan
    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Command exited with code $LASTEXITCODE" }
}
function Capture([string]$Command, [string[]]$Arguments) {
    $result = & $Command @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Command exited with code $LASTEXITCODE" }
    return $result
}
function RequireCommand([string]$Name) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) { throw "Missing executable: $Name" }
}
function KArgs([string[]]$ArgsIn) {
    if ($Kubeconfig) { return @('--kubeconfig', $Kubeconfig) + $ArgsIn }
    return $ArgsIn
}
function HelmArgs([string[]]$ArgsIn) {
    if ($Kubeconfig) { return @('--kubeconfig', $Kubeconfig) + $ArgsIn }
    return $ArgsIn
}
function VerifyContext() {
    $context = (Capture 'kubectl' (KArgs @('config','current-context')) | Out-String).Trim()
    if ($context -ne 'k3d-ai-investment-dev') {
        throw "Refusing mutating operation in context '$context'. Expected k3d-ai-investment-dev."
    }
    $ready = (Capture 'kubectl' (KArgs @('get','--raw=/readyz')) | Out-String).Trim()
    if ($ready -ne 'ok') { throw "Kubernetes API not ready: $ready" }
    Write-Host "Verified cluster context: $context" -ForegroundColor Green
}
function Image([string]$Component) { return "${Registry}/ai-investment/${Component}:${Tag}" }
# Same repository path and tag as Image(), under the cluster-internal
# registry address -- the only correct reference for a kubectl image
# field in this k3d context. Never used for docker build/push/local-tag
# checks, which must keep using $Registry/Image() against the host-side
# Docker daemon.
function ClusterImage([string]$Component) { return "${ClusterRegistry}/ai-investment/${Component}:${Tag}" }
function ValidateTag() {
    if ([string]::IsNullOrWhiteSpace($Tag) -or $Tag -notmatch '^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$') {
        throw 'Specify a valid, unique -Tag (example: radar-20261015-01). Do not reuse deployed tags.'
    }
}
function VerifyLocalImages() {
    foreach ($component in $components) {
        $image = Image $component
        Run 'docker' @('image','inspect','--format','{{.Id}}', $image)
    }
}
function CheckBuildInputs() {
    if (-not (Test-Path (Join-Path $frontendContext 'package-lock.json'))) { throw 'Frontend package-lock.json is missing.' }
    if (-not (Test-Path (Join-Path $researchContext 'pyproject.toml'))) { throw 'Research pyproject.toml is missing.' }
    if (-not (Test-Path $devValues)) { throw "Missing Helm values: $devValues" }
}
function CheckSafeHelm() {
    Run 'helm' @('lint', $chart, '-f', $devValues)
    # Template validation is mandatory even when helm lint passes.
    $arguments = @('template', $Release, $chart, '-n', $Namespace, '-f', $devValues)
    $rendered = Capture 'helm' $arguments
    if (-not ($rendered | Select-String -Quiet 'kind: Deployment')) { throw 'Helm rendered no Deployments.' }
}
function ReadDeployments() {
    Run 'kubectl' (KArgs @('get','deployments','frontend','research-engine','api-gateway','-n',$Namespace,
        '-o','custom-columns=NAME:.metadata.name,IMAGE:.spec.template.spec.containers[*].image'))
}
RequireCommand 'docker'
RequireCommand 'helm'
RequireCommand 'kubectl'
CheckBuildInputs
if ($Kubeconfig -and -not (Test-Path $Kubeconfig)) { throw "Kubeconfig not found: $Kubeconfig" }

switch ($Action) {
    'Plan' {
        Write-Host 'READ-ONLY PLAN; no images built, pushed, or deployed.' -ForegroundColor Yellow
        RequireCommand 'git'
        Run 'git' @('-C', $root, 'status','--short')
        CheckSafeHelm
        Write-Host 'Proposed DEV policy: Equity manual=true; ETF manual=false; ETF full-universe=false; scheduler=false; recovery paused=true.'
        Write-Host 'WARNING: Current release may explicitly allow ETF full-universe; do not reconcile Helm without reviewing all resource diffs.' -ForegroundColor Yellow
        try { ReadDeployments } catch { Write-Warning "Unable to inspect cluster: $_" }
        Write-Host 'Migration prerequisite: verify live research checkpoint constraint permits ETF_ONBOARD (confirmed in earlier checks; rerun if cluster changed).'
    }
    'Build' {
        ValidateTag
        RequireCommand 'git'
        foreach ($component in $components) {
            $img = Image $component
            # Prevent silent reuse/overwrite of an existing immutable-by-convention tag.
            $existingImage = & docker image ls --quiet --filter "reference=$img"
            if ($LASTEXITCODE -ne 0) { throw "Docker image lookup failed for $img" }; if ($existingImage) { throw "Refusing to overwrite existing local image tag: $img" }
            if ($component -eq 'frontend') {
                $loginFlag = if ($EnableDevLoginBuild) { 'true' } else { 'false' }
                Run 'docker' @('build','--pull=false','-f',(Join-Path $frontendContext 'Dockerfile'),
                    '--build-arg',"NEXT_PUBLIC_API_BASE_URL=$ApiBaseUrl",
                    '--build-arg',"NEXT_PUBLIC_AUTH_DEV_LOGIN_ENABLED=$loginFlag",
                    '-t',$img,$frontendContext)
            } else {
                Run 'docker' @('build','--pull=false','-f',(Join-Path $researchContext 'Dockerfile'),'-t',$img,$researchContext)
            }
        }
        Write-Host 'Build complete. Images remain LOCAL; run Push only after approval.' -ForegroundColor Green
    }
    'Push' {
        if (-not $ApprovePush) { throw 'Push requires -ApprovePush.' }
        ValidateTag
        VerifyLocalImages
        foreach ($component in $components) { Run 'docker' @('push',(Image $component)) }
        Write-Host 'Push complete; no deployments changed.' -ForegroundColor Green
    }
    'Deploy' {
        if (-not $ApproveDeploy) { throw 'Deploy requires -ApproveDeploy.' }
        ValidateTag
        VerifyContext
        CheckSafeHelm
        ReadDeployments
        # Targeted deployment avoids chart-wide Helm reconciliation. Helm may later revert this intentional image drift.
        Write-Warning 'This is a kubectl image update, NOT a Helm upgrade. It creates temporary drift from Helm release values.'
        Write-Warning 'Starting research-engine may run application startup/migration logic. Verify startup safety and image availability first.'
        $reply = Read-Host "Type DEPLOY $Tag to update selected workloads ($($components -join ', '))"
        if ($reply -cne "DEPLOY $Tag") { throw 'Deployment cancelled.' }
        foreach ($component in $components) {
            # kubectl's image reference MUST be the cluster-internal registry
            # address (ClusterImage) -- containerd on each node never resolves
            # the host-side $Registry (localhost:5001); using Image() here was
            # the confirmed defect behind the observed ImagePullBackOff.
            $img = ClusterImage $component
            Write-Host "  $component -> $img" -ForegroundColor Cyan
            Run 'kubectl' (KArgs @('set','image',"deployment/$component","$component=$img",'-n',$Namespace))
            Run 'kubectl' (KArgs @('rollout','status',"deployment/$component",'-n',$Namespace,'--timeout=240s'))
        }
        Write-Host 'Targeted deployment completed. Validate authenticated UI/API; no Radar cycles were initiated by this script.' -ForegroundColor Green
    }
    'Fresh' {
        if (-not $ApproveFresh) { throw 'Fresh requires -ApproveFresh.' }
        VerifyContext
        CheckSafeHelm
        $existingNs = Capture 'kubectl' (KArgs @('get','namespace',$Namespace,'-o','name','--ignore-not-found'))
        if ($existingNs) { throw "Fresh mode refuses EXISTING namespace $Namespace. Never install over existing workloads/data." }
        if (-not $FreshValuesFile -or -not (Test-Path $FreshValuesFile)) {
            throw 'Fresh requires -FreshValuesFile with a reviewed, environment-specific Helm overlay for ALL service images, secrets and persistence.'
        }
        Write-Warning 'Fresh install provisions the chart in a new namespace. Startup migrations, secrets, PVCs and all chart workloads may be created.'
        Run 'helm' @('lint',$chart,'-f',$devValues,'-f',$FreshValuesFile)
        $freshRender = Capture 'helm' @('template',$Release,$chart,'-n',$Namespace,'-f',$devValues,'-f',$FreshValuesFile)
        if (-not ($freshRender | Select-String -Quiet 'kind: StatefulSet')) { throw 'Fresh render missing database StatefulSet.' }
        $reply = Read-Host "Type FRESH $Namespace to install a complete platform in a NEW namespace"
        if ($reply -cne "FRESH $Namespace") { throw 'Fresh installation cancelled.' }
        Run 'helm' (HelmArgs @('install',$Release,$chart,'-n',$Namespace,'--create-namespace',
            '-f',$devValues,'-f',$FreshValuesFile,'--wait','--timeout','10m'))
        Write-Host 'Fresh Helm install completed. Verify every workload and database before use.' -ForegroundColor Green
    }
}
