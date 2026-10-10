param(
    [ValidateSet("up", "deploy", "down", "clean", "status", "url", "components", "backup", "backups", "backup-verify", "restore")]
    [string]$Command = "up",

    # For `deploy`, choose individual components. Example:
    #   .\platform.ps1 deploy -Component frontend,research-engine
    [string[]]$Component,

    # Optional named deployment profile. Supported values are printed by
    # `.\platform.ps1 components`.
    [string]$Profile,

    [switch]$SkipBuild,
    [switch]$NoCache,
    [switch]$KeepOldImages,
    [switch]$ForceClean,
    [switch]$NoBrowser,
    [switch]$EnableIbkr,

    # First-ADMIN bootstrap (Feature: automatic first-ADMIN initialization).
    [string]$FirstAdminEmail,

    # On-demand PostgreSQL backup/restore (Feature: manual backup/restore).
    [string]$Backup,
    [string]$TargetDatabase,
    [switch]$AuthorizeDestructiveRestore,
    [switch]$SkipSafetyBackup,
    [switch]$ConfirmSafetyBackup
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

# -----------------------------------------------------------------------------
# AI Investment Platform - LOCAL full-stack deployment helper
#
# Usage from repository root:
#   .\platform.ps1
#   .\platform.ps1 up
#   .\platform.ps1 up -SkipBuild
#   .\platform.ps1 up -EnableIbkr
#   .\platform.ps1 deploy -Component frontend,research-engine
#   .\platform.ps1 deploy -Profile research-ui
#   .\platform.ps1 components
#   .\platform.ps1 status
#   .\platform.ps1 url
#   .\platform.ps1 down
#   .\platform.ps1 clean
#   .\platform.ps1 backup
#   .\platform.ps1 backups
#   .\platform.ps1 backup-verify -Backup latest
#   .\platform.ps1 restore -Backup latest
#   .\platform.ps1 restore -Backup 2026-10-10_153000_ab12cd -AuthorizeDestructiveRestore
#   .\platform.ps1 restore -Backup latest -TargetDatabase investment_test_restore -AuthorizeDestructiveRestore
#
# LOCAL behavior:
# - Creates/starts the k3d cluster.
# - Creates required external DEV secrets interactively when missing.
# - `up` builds the complete stack; `deploy` rebuilds only selected components/profiles.
# - Every build uses a new immutable timestamp tag. After a successful rollout,
#   older images for the deployed component(s) are removed unless -KeepOldImages is used.
# - Imports the immutable images directly into k3d (no local-registry push required).
# - Installs/upgrades the Helm release without blocking on startup, then reconciles
#   application Deployments to the exact immutable release tag.
# - Applies LOCAL runtime normalization before waiting for readiness.
# - IBKR runtime is disabled by default because broker authentication is parked.
#   Pass -EnableIbkr only when actively testing IBKR.
# - Waits for the complete enabled stack, prints diagnostics on failure,
#   verifies the gateway application URL, and opens it unless -NoBrowser is supplied.
# - Never falls back to a direct frontend port-forward because the frontend uses
#   same-origin /api routes through the API Gateway.
# - "down" stops the k3d cluster and preserves PostgreSQL/PVC state.
# - "clean" is intentionally destructive: it deletes the local k3d cluster,
#   its Kubernetes/Helm/PVC runtime state, and all host Docker application image
#   tags for this platform. Use -ForceClean to skip the confirmation prompt.
# - A genuinely fresh "up" automatically provisions the first ADMIN account
#   (see Invoke-FirstAdminBootstrapIfNeeded); it is a no-op whenever an ADMIN
#   already exists, and fails closed (with a clear warning) if accounts exist
#   but none holds ADMIN, rather than silently creating a second one.
# - "backup"/"backups"/"backup-verify"/"restore" are manual, on-demand only.
#   Nothing in this script schedules or auto-triggers a backup or a restore.
# -----------------------------------------------------------------------------

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$HelmChart = Join-Path $ProjectRoot "infrastructure\helm\ai-investment-platform"
$HelmValues = Join-Path $HelmChart "values-dev.yaml"
$K3dConfig = Join-Path $ProjectRoot "infrastructure\k3d\cluster-dev.yaml"

$ClusterName = "ai-investment-dev"
$Namespace = "ai-investment"
$ReleaseName = "ai-investment-platform"
$ImageRegistry = "localhost:5001"
$ImageTag = $null
$IngressApplicationUrl = "http://localhost:18080"
$RuntimeDir = Join-Path $ProjectRoot ".tmp"
$ApplicationUrlFile = Join-Path $RuntimeDir "application-url.txt"
$ImageTagFile = Join-Path $RuntimeDir "last-image-tag.txt"
$LegacyPortForwardPidFile = Join-Path $RuntimeDir "frontend-port-forward.pid"
$OpportunityBootstrapMarkerFile = Join-Path $RuntimeDir "initial-opportunity-cycle-triggered.txt"

# On-demand PostgreSQL backup/restore (Feature: manual backup/restore).
# Modular scripts under scripts/database/; never auto-invoked.
Import-Module (Join-Path $ProjectRoot "scripts\database\PostgresBackupCommon.psm1") -Force
Import-Module (Join-Path $ProjectRoot "scripts\database\RadarBootstrapEligibility.psm1") -Force
. (Join-Path $ProjectRoot "scripts\database\backup.ps1")
. (Join-Path $ProjectRoot "scripts\database\list-backups.ps1")
. (Join-Path $ProjectRoot "scripts\database\verify-backup.ps1")
. (Join-Path $ProjectRoot "scripts\database\restore.ps1")

$DatabaseConfig = @{
    Name     = "investment"
    Username = "investment"
}

$JavaServices = @(
    "api-gateway",
    "auth-service",
    "portfolio-service",
    "broker-service",
    "company-service",
    "research-service",
    "recommendation-service",
    "risk-service",
    "notification-service"
)

$DatabaseJavaServices = @(
    "auth-service",
    "portfolio-service",
    "broker-service",
    "research-service"
)

$AiServices = @(
    "research-engine",
    "valuation-engine",
    "ranking-engine",
    "portfolio-optimizer",
    "mcp-gateway"
)

$PythonServices = @("ibkr-connector")

$DeploymentProfiles = [ordered]@{
    "frontend"    = @("frontend")
    "research"    = @("research-engine")
    "research-ui" = @("frontend", "research-engine", "portfolio-service")
    "portfolio"   = @("portfolio-service")
    "gateway"     = @("api-gateway")
    "mcp"         = @("mcp-gateway", "yahoo-finance-mcp")
}

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Require-Command {
    param([string]$Name)
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required command '$Name' was not found on PATH."
    }
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$Description,
        [Parameter(Mandatory = $true)][scriptblock]$Script
    )

    Write-Step $Description
    & $Script
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE."
    }
}

function Assert-FileExists {
    param([string]$Path, [string]$Description)
    if (-not (Test-Path $Path)) {
        throw "$Description was not found: $Path"
    }
}

function Assert-LocalPrerequisites {
    Require-Command "docker"
    Require-Command "k3d"
    Require-Command "kubectl"
    Require-Command "helm"

    Assert-FileExists $HelmChart "Helm chart"
    Assert-FileExists $HelmValues "DEV Helm values"
    Assert-FileExists $K3dConfig "k3d cluster configuration"

    docker info *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "Docker Desktop is not running or is not reachable. Start Docker Desktop and retry."
    }
}

function Assert-BuildPrerequisites {
    Require-Command "mvn"
}

function Ensure-RuntimeDirectory {
    if (-not (Test-Path $RuntimeDir)) {
        New-Item -ItemType Directory -Path $RuntimeDir -Force | Out-Null
    }
}

function Initialize-ImageTag {
    Ensure-RuntimeDirectory

    if ($SkipBuild) {
        if (-not (Test-Path $ImageTagFile)) {
            throw "-SkipBuild requires a previously successful immutable build. '$ImageTagFile' does not exist."
        }

        $script:ImageTag = (Get-Content -Raw $ImageTagFile).Trim()
        if ([string]::IsNullOrWhiteSpace($script:ImageTag)) {
            throw "-SkipBuild could not resolve a previous immutable image tag from '$ImageTagFile'."
        }

        Write-Host "Reusing immutable image tag: $script:ImageTag" -ForegroundColor DarkGray
        return
    }

    $script:ImageTag = "dev-" + (Get-Date -Format "yyyyMMdd-HHmmss")
    Write-Host "Immutable image tag: $script:ImageTag" -ForegroundColor DarkGray
}

function Save-SuccessfulFullImageTag {
    Ensure-RuntimeDirectory
    Set-Content -Path $ImageTagFile -Value $script:ImageTag -Encoding ASCII
}

function Test-ClusterExists {
    try {
        $json = k3d cluster list -o json
        if ($LASTEXITCODE -ne 0 -or -not $json) { return $false }
        $clusters = @($json | ConvertFrom-Json)
        return [bool]($clusters | Where-Object { $_.name -eq $ClusterName } | Select-Object -First 1)
    }
    catch {
        return $false
    }
}

function Select-ClusterContext {
    Invoke-Checked "Selecting kube context for '$ClusterName'" {
        k3d kubeconfig merge $ClusterName --kubeconfig-switch-context
    }
}

function Ensure-Cluster {
    $existing = @()
    try {
        $json = k3d cluster list -o json
        if ($LASTEXITCODE -eq 0 -and $json) {
            $existing = @($json | ConvertFrom-Json)
        }
    }
    catch {
        $existing = @()
    }

    $cluster = $existing | Where-Object { $_.name -eq $ClusterName } | Select-Object -First 1
    if ($cluster) {
        Invoke-Checked "Starting existing k3d cluster '$ClusterName'" {
            k3d cluster start $ClusterName
        }
    }
    else {
        Invoke-Checked "Creating k3d cluster '$ClusterName'" {
            k3d cluster create --config $K3dConfig
        }
    }

    Select-ClusterContext
}

function Ensure-Namespace {
    # Clean clusters legitimately do not have this namespace yet. Avoid a
    # failing kubectl existence check because ErrorActionPreference=Stop can
    # promote native stderr to a terminating NativeCommandError.
    $existingNamespace = kubectl get namespace $Namespace --ignore-not-found -o name 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to query namespace '$Namespace'."
    }

    if ([string]::IsNullOrWhiteSpace(($existingNamespace | Out-String).Trim())) {
        Invoke-Checked "Creating namespace '$Namespace'" {
            kubectl create namespace $Namespace
        }
    }
}

function Test-SecretExists {
    param([string]$Name)
    $existingSecret = kubectl get secret $Name -n $Namespace --ignore-not-found -o name 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to query secret '$Name' in namespace '$Namespace'."
    }
    return (-not [string]::IsNullOrWhiteSpace(($existingSecret | Out-String).Trim()))
}

function Convert-SecureStringToPlainText {
    param([Security.SecureString]$SecureValue)
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureValue)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
    }
}

function New-OrReplaceLiteralSecret {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][hashtable]$Literals
    )

    $args = @("create", "secret", "generic", $Name, "-n", $Namespace)
    foreach ($key in $Literals.Keys) {
        $args += "--from-literal=$key=$($Literals[$key])"
    }
    $args += @("--dry-run=client", "-o", "json")

    $manifest = & kubectl @args
    if ($LASTEXITCODE -ne 0 -or -not $manifest) {
        throw "Unable to render Kubernetes secret '$Name'."
    }

    $tempFile = Join-Path $RuntimeDir "$Name.secret.json"
    try {
        Set-Content -Path $tempFile -Value $manifest -Encoding UTF8
        kubectl apply -f $tempFile | Out-Host
        if ($LASTEXITCODE -ne 0) {
            throw "Unable to apply Kubernetes secret '$Name'."
        }
    }
    finally {
        Remove-Item $tempFile -Force -ErrorAction SilentlyContinue
    }
}

function New-OrReplaceSecretFromDataValues {
    # Like New-OrReplaceLiteralSecret, but never passes a secret value as a
    # literal on the kubectl command line (where it would be visible to
    # other processes on the same machine, e.g. via Task Manager/WMI, for
    # the duration of the call). Instead, this builds the Secret's base64
    # "data" map directly in PowerShell and applies it the same way
    # New-OrReplaceLiteralSecret applies its rendered manifest: written to
    # a temp file, applied with `kubectl apply -f`, then deleted. Used
    # specifically for values that must never appear as a process
    # argument, such as the generated first-ADMIN bootstrap password.
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][hashtable]$Literals
    )

    $data = [ordered]@{}
    foreach ($key in $Literals.Keys) {
        $bytes = [Text.Encoding]::UTF8.GetBytes([string]$Literals[$key])
        $data[$key] = [Convert]::ToBase64String($bytes)
    }

    $secret = [ordered]@{
        apiVersion = "v1"
        kind       = "Secret"
        type       = "Opaque"
        metadata   = @{ name = $Name; namespace = $Namespace }
        data       = $data
    }

    $tempFile = Join-Path $RuntimeDir "$Name.secret.json"
    try {
        $secret | ConvertTo-Json -Depth 6 | Set-Content -Path $tempFile -Encoding UTF8
        kubectl apply -f $tempFile | Out-Host
        if ($LASTEXITCODE -ne 0) {
            throw "Unable to apply Kubernetes secret '$Name'."
        }
    }
    finally {
        Remove-Item $tempFile -Force -ErrorAction SilentlyContinue
    }
}

function Ensure-DevSecrets {
    Ensure-RuntimeDirectory
    Ensure-Namespace

    if (-not (Test-SecretExists "auth-smtp-credentials")) {
        Write-Step "Configuring LOCAL SMTP credentials"
        Write-Host "auth-service requires SMTP credentials in LOCAL mode."
        Write-Host "For Gmail, use an App Password rather than your normal account password."

        $defaultUser = $env:AIP_SMTP_USERNAME
        if (-not $defaultUser) { $defaultUser = "prakhar.unique@gmail.com" }
        $enteredUser = if ($env:AIP_SMTP_USERNAME) { $env:AIP_SMTP_USERNAME } else { Read-Host "SMTP username [$defaultUser]" }
        if ([string]::IsNullOrWhiteSpace($enteredUser)) { $enteredUser = $defaultUser }

        $smtpPasswordPlain = $env:AIP_SMTP_PASSWORD
        if (-not $smtpPasswordPlain) {
            $securePassword = Read-Host "SMTP/App password" -AsSecureString
            $smtpPasswordPlain = Convert-SecureStringToPlainText $securePassword
        }
        if ([string]::IsNullOrWhiteSpace($smtpPasswordPlain)) {
            throw "SMTP password cannot be empty."
        }

        New-OrReplaceLiteralSecret "auth-smtp-credentials" @{
            SMTP_USERNAME = $enteredUser
            SMTP_PASSWORD = $smtpPasswordPlain
        }
        $smtpPasswordPlain = $null
    }
    else {
        Write-Host "Secret auth-smtp-credentials already exists; keeping it." -ForegroundColor DarkGray
    }

    # LOCAL research/news search uses the in-cluster SearXNG service.
    # The current Helm template still references the legacy Google search
    # secret keys as mandatory env sources even though Google search is not
    # part of the active LOCAL acquisition path. Create a non-sensitive
    # compatibility secret automatically so a clean deployment does not ask
    # the user for paid Google credentials.
    if (-not (Test-SecretExists "research-search-secret")) {
        Write-Step "Creating LOCAL search compatibility secret (SearXNG is the active search provider)"
        New-OrReplaceLiteralSecret "research-search-secret" @{
            "google-engine-id" = "disabled-local-searxng"
            "google-api-key"   = "disabled-local-searxng"
        }
    }
    else {
        Write-Host "Secret research-search-secret already exists; keeping it." -ForegroundColor DarkGray
    }

    if (-not (Test-SecretExists "ibkr-runtime-internal-token")) {
        Write-Step "Creating LOCAL IBKR internal service token"
        $token = ([guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N"))
        New-OrReplaceLiteralSecret "ibkr-runtime-internal-token" @{
            AIP_INTERNAL_TOKEN = $token
        }
        $token = $null
    }
    else {
        Write-Host "Secret ibkr-runtime-internal-token already exists; keeping it." -ForegroundColor DarkGray
    }
}

function Get-ImageDefinitions {
    param([string[]]$Names)

    $images = @()

    $images += [pscustomobject]@{
        Name           = "frontend"
        Context        = Join-Path $ProjectRoot "frontend"
        Dockerfile     = $null
        Repo           = "$ImageRegistry/ai-investment/frontend"
        Image          = "$ImageRegistry/ai-investment/frontend:$ImageTag"
        BootstrapImage = "$ImageRegistry/ai-investment/frontend:dev"
    }

    foreach ($service in $JavaServices) {
        $context = Join-Path $ProjectRoot "services\$service"
        if (Test-Path (Join-Path $context "Dockerfile")) {
            $images += [pscustomobject]@{
                Name           = $service
                Context        = $context
                Dockerfile     = $null
                Repo           = "$ImageRegistry/ai-investment/$service"
                Image          = "$ImageRegistry/ai-investment/$($service):$ImageTag"
                BootstrapImage = "$ImageRegistry/ai-investment/$($service):dev"
            }
        }
        else {
            Write-Warning "Skipping '$service' because no Dockerfile exists at '$context'."
        }
    }

    foreach ($service in $AiServices) {
        $context = Join-Path $ProjectRoot "ai\$service"
        if (Test-Path (Join-Path $context "Dockerfile")) {
            $images += [pscustomobject]@{
                Name           = $service
                Context        = $context
                Dockerfile     = $null
                Repo           = "$ImageRegistry/ai-investment/$service"
                Image          = "$ImageRegistry/ai-investment/$($service):$ImageTag"
                BootstrapImage = "$ImageRegistry/ai-investment/$($service):dev"
            }
        }
        else {
            Write-Warning "Skipping '$service' because no Dockerfile exists at '$context'."
        }
    }

    foreach ($service in $PythonServices) {
        $dockerfile = Join-Path $ProjectRoot "services\$service\Dockerfile"
        if (Test-Path $dockerfile) {
            $images += [pscustomobject]@{
                Name           = $service
                Context        = $ProjectRoot
                Dockerfile     = $dockerfile
                Repo           = "$ImageRegistry/ai-investment/$service"
                Image          = "$ImageRegistry/ai-investment/$($service):$ImageTag"
                BootstrapImage = "$ImageRegistry/ai-investment/$($service):dev"
            }
        }
    }

    $yahooDockerfile = Join-Path $ProjectRoot "ai\yahoo-finance-mcp\Dockerfile"
    if (-not (Test-Path $yahooDockerfile)) {
        throw "First-party Yahoo Finance MCP Dockerfile was not found: $yahooDockerfile"
    }
    $images += [pscustomobject]@{
        Name           = "yahoo-finance-mcp"
        Context        = $ProjectRoot
        Dockerfile     = $yahooDockerfile
        Repo           = "$ImageRegistry/ai-investment/yahoo-finance-mcp"
        Image          = "$ImageRegistry/ai-investment/yahoo-finance-mcp:$ImageTag"
        BootstrapImage = "$ImageRegistry/ai-investment/yahoo-finance-mcp:dev"
    }

    if ($Names -and $Names.Count -gt 0) {
        $unknown = @($Names | Where-Object { $_ -notin @($images.Name) })
        if ($unknown.Count -gt 0) {
            throw "Unknown component(s): $($unknown -join ', '). Run '.\\platform.ps1 components' for valid names."
        }
        return @($images | Where-Object { $_.Name -in $Names })
    }

    return $images
}

function Resolve-SelectedComponents {
    param([switch]$RequireSelection)

    $all = @((Get-ImageDefinitions).Name)
    $selected = @()

    if ($Profile) {
        $profileKey = $Profile.Trim().ToLowerInvariant()
        if ($profileKey -eq "all") {
            $selected += $all
        }
        elseif ($DeploymentProfiles.Contains($profileKey)) {
            $selected += @($DeploymentProfiles[$profileKey])
        }
        else {
            throw "Unknown profile '$Profile'. Run '.\\platform.ps1 components' for supported profiles."
        }
    }

    if ($Component) {
        foreach ($item in $Component) {
            if ([string]::IsNullOrWhiteSpace($item)) { continue }
            $selected += @($item -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
        }
    }

    $selected = @($selected | Select-Object -Unique)
    if ($RequireSelection -and $selected.Count -eq 0) {
        throw "The deploy command requires -Component or -Profile. Example: .\\platform.ps1 deploy -Profile research-ui"
    }
    if ($selected.Count -eq 0) { return $all }

    $unknown = @($selected | Where-Object { $_ -notin $all })
    if ($unknown.Count -gt 0) {
        throw "Unknown component(s): $($unknown -join ', '). Run '.\\platform.ps1 components' for valid names."
    }

    return $selected
}

function Show-Components {
    $previousTag = $script:ImageTag
    if (-not $script:ImageTag) { $script:ImageTag = "preview" }
    try {
        Write-Host ""
        Write-Host "Available deployable components:" -ForegroundColor Cyan
        foreach ($name in @((Get-ImageDefinitions).Name)) {
            Write-Host "  - $name"
        }
        Write-Host ""
        Write-Host "Profiles:" -ForegroundColor Cyan
        Write-Host "  - all         : complete application image set"
        foreach ($entry in $DeploymentProfiles.GetEnumerator()) {
            Write-Host ("  - {0,-11} : {1}" -f $entry.Key, ($entry.Value -join ', '))
        }
        Write-Host ""
        Write-Host "Examples:" -ForegroundColor Cyan
        Write-Host "  .\\platform.ps1 deploy -Component frontend"
        Write-Host "  .\\platform.ps1 deploy -Component frontend,research-engine"
        Write-Host "  .\\platform.ps1 deploy -Profile research-ui"
        Write-Host "  .\\platform.ps1 up -NoCache"
    }
    finally {
        $script:ImageTag = $previousTag
    }
}

function Build-Images {
    param([Parameter(Mandatory = $true)][string[]]$Names)

    $definitions = @(Get-ImageDefinitions -Names $Names)
    $selectedJava = @($definitions | Where-Object { $_.Name -in $JavaServices })

    if ($selectedJava.Count -gt 0) {
        Invoke-Checked "Building Java artifacts required by selected services" {
            mvn -f (Join-Path $ProjectRoot "services\pom.xml") package -DskipTests
        }
    }

    foreach ($image in $definitions) {
        Invoke-Checked "Building fresh $($image.Name) -> $($image.Image)" {
            $args = @("build", "--pull")
            if ($NoCache) { $args += "--no-cache" }
            if ($image.Dockerfile) { $args += @("-f", $image.Dockerfile) }
            $args += @("-t", $image.Image, "-t", $image.BootstrapImage, $image.Context)
            & docker @args
        }
    }
}

function Prepare-BootstrapImageAliases {
    param([Parameter(Mandatory = $true)][string[]]$Names)

    foreach ($image in Get-ImageDefinitions -Names $Names) {
        docker image inspect $image.Image *> $null
        if ($LASTEXITCODE -ne 0) {
            throw "Immutable image '$($image.Image)' is not available locally. Re-run without -SkipBuild."
        }

        docker tag $image.Image $image.BootstrapImage
        if ($LASTEXITCODE -ne 0) {
            throw "Unable to create temporary bootstrap alias '$($image.BootstrapImage)'."
        }
    }
}

function Import-ImagesIntoK3d {
    param([Parameter(Mandatory = $true)][string[]]$Names)

    Prepare-BootstrapImageAliases -Names $Names

    $images = @(
        Get-ImageDefinitions -Names $Names |
            ForEach-Object { @($_.Image, $_.BootstrapImage) } |
            ForEach-Object { $_ }
    ) | Select-Object -Unique

    if (-not $images) { throw "No application images were discovered to import." }

    Invoke-Checked "Importing selected immutable application images into k3d" {
        k3d image import --cluster $ClusterName @images
    }
}

function Validate-Helm {
    Invoke-Checked "Linting Helm chart" {
        helm lint $HelmChart -f $HelmValues
    }

    Invoke-Checked "Rendering Helm chart" {
        helm template $ReleaseName $HelmChart -n $Namespace -f $HelmValues *> $null
    }
}

function Deploy-Helm {
    # Intentionally do NOT use --wait here. We must normalize LOCAL env values
    # and optional broker runtime settings before Kubernetes readiness is judged.
    Invoke-Checked "Installing/upgrading complete DEV stack" {
        helm upgrade --install $ReleaseName $HelmChart `
            --namespace $Namespace `
            --create-namespace `
            --values $HelmValues `
            --force-conflicts `
            --timeout 10m
    }
}

function Set-ImmutableDeploymentImages {
    param([Parameter(Mandatory = $true)][string[]]$Names)

    Write-Step "Reconciling selected Deployments to immutable image tag '$ImageTag'"

    foreach ($image in Get-ImageDefinitions -Names $Names) {
        $deploymentName = $image.Name
        $deploymentJsonRaw = kubectl get deployment $deploymentName -n $Namespace --ignore-not-found -o json 2>$null
        if ($LASTEXITCODE -ne 0) { throw "Unable to query deployment '$deploymentName'." }
        if ([string]::IsNullOrWhiteSpace(($deploymentJsonRaw | Out-String).Trim())) {
            Write-Host "Deployment '$deploymentName' is not present; skipping immutable image reconciliation." -ForegroundColor DarkGray
            continue
        }

        $deployment = $deploymentJsonRaw | ConvertFrom-Json
        $containerPatch = @()
        $initContainerPatch = @()

        foreach ($container in @($deployment.spec.template.spec.containers)) {
            if ($container.name -eq $deploymentName -or $container.image -like "$($image.Repo):*") {
                $containerPatch += @{ name = $container.name; image = $image.Image }
            }
        }
        foreach ($container in @($deployment.spec.template.spec.initContainers)) {
            if ($null -ne $container -and ($container.name -eq $deploymentName -or $container.image -like "$($image.Repo):*")) {
                $initContainerPatch += @{ name = $container.name; image = $image.Image }
            }
        }

        if ($containerPatch.Count -eq 0 -and $initContainerPatch.Count -eq 0) {
            throw "Deployment '$deploymentName' exists, but no container uses repository '$($image.Repo)'. Refusing to guess."
        }

        $templateSpec = @{}
        if ($containerPatch.Count -gt 0) { $templateSpec["containers"] = $containerPatch }
        if ($initContainerPatch.Count -gt 0) { $templateSpec["initContainers"] = $initContainerPatch }

        $patch = @{
            spec = @{
                template = @{
                    metadata = @{ annotations = @{ "aip.openai.com/local-image-tag" = $ImageTag } }
                    spec = $templateSpec
                }
            }
        }

        $patchFile = Join-Path $RuntimeDir ("immutable-image-" + $deploymentName + ".json")
        try {
            $patch | ConvertTo-Json -Depth 12 | Set-Content -Path $patchFile -Encoding UTF8
            kubectl patch deployment $deploymentName -n $Namespace --field-manager=helm --type=strategic --patch-file $patchFile | Out-Host
            if ($LASTEXITCODE -ne 0) { throw "Unable to set immutable image for deployment '$deploymentName'." }
        }
        finally {
            Remove-Item $patchFile -Force -ErrorAction SilentlyContinue
        }
    }
}

function Assert-ImmutableDeploymentImages {
    param([Parameter(Mandatory = $true)][string[]]$Names)

    Write-Step "Verifying selected immutable deployment images"
    $failures = @()

    foreach ($image in Get-ImageDefinitions -Names $Names) {
        $deploymentJsonRaw = kubectl get deployment $image.Name -n $Namespace --ignore-not-found -o json 2>$null
        if ($LASTEXITCODE -ne 0) { $failures += "$($image.Name): unable to query deployment"; continue }
        if ([string]::IsNullOrWhiteSpace(($deploymentJsonRaw | Out-String).Trim())) { continue }

        $deployment = $deploymentJsonRaw | ConvertFrom-Json
        $matchingImages = @()
        foreach ($container in @($deployment.spec.template.spec.containers)) {
            if ($container.name -eq $image.Name -or $container.image -like "$($image.Repo):*") { $matchingImages += $container.image }
        }
        foreach ($container in @($deployment.spec.template.spec.initContainers)) {
            if ($null -ne $container -and ($container.name -eq $image.Name -or $container.image -like "$($image.Repo):*")) { $matchingImages += $container.image }
        }

        $wrong = @($matchingImages | Where-Object { $_ -ne $image.Image })
        if ($matchingImages.Count -eq 0) { $failures += "$($image.Name): no matching container image found" }
        elseif ($wrong.Count -gt 0) { $failures += "$($image.Name): expected '$($image.Image)', found '$($wrong -join ', ')'" }
        else { Write-Host "$($image.Name) -> $($image.Image)" -ForegroundColor DarkGray }
    }

    if ($failures.Count -gt 0) {
        throw "Immutable deployment image verification failed:`n - $($failures -join "`n - ")"
    }
}

function Set-DeploymentEnvIfExists {
    param(
        [Parameter(Mandatory = $true)][string]$Deployment,
        [Parameter(Mandatory = $true)][string[]]$Assignments
    )

    $existingDeployment = kubectl get deployment $Deployment -n $Namespace --ignore-not-found -o name 2>$null
    if ($LASTEXITCODE -ne 0) { throw "Unable to query deployment '$Deployment'." }
    if ([string]::IsNullOrWhiteSpace(($existingDeployment | Out-String).Trim())) { return }

    # Use Helm's field manager for LOCAL runtime normalization so rerunning
    # `platform.ps1 up` does not create kubectl-set ownership conflicts with
    # Helm's server-side apply on the next upgrade.
    kubectl set env deployment/$Deployment -n $Namespace --field-manager=helm @Assignments | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to normalize environment for deployment '$Deployment'."
    }
}

function Apply-LocalRuntimeNormalization {
    Write-Step "Applying LOCAL runtime normalization"

    # PowerShell/Helm/YAML may render 1800000 as 1.8e+06. Spring Hikari binds
    # max-lifetime as a Java long and rejects scientific notation. Force exact
    # integer strings on all DB-backed Java services before readiness checks.
    foreach ($service in $DatabaseJavaServices) {
        Set-DeploymentEnvIfExists $service @(
            "DB_POOL_MAXIMUM_SIZE=8",
            "DB_POOL_MINIMUM_IDLE=1",
            "DB_POOL_CONNECTION_TIMEOUT_MS=10000",
            "DB_POOL_IDLE_TIMEOUT_MS=600000",
            "DB_POOL_MAX_LIFETIME_MS=1800000"
        )
    }

    # Kafka KRaft can take several minutes on a brand-new k3d cluster while the
    # controller/broker catches up. The chart's current startup probe restarts
    # Kafka too early, which interrupts KRaft startup and produces:
    # "Received a fatal error while waiting for the controller to acknowledge
    # that we are caught up". Give Kafka a bounded 10-minute startup window.
    $kafkaDeployment = kubectl get deployment kafka -n $Namespace --ignore-not-found -o name 2>$null
    if ($LASTEXITCODE -ne 0) { throw "Unable to query Kafka deployment." }
    if (-not [string]::IsNullOrWhiteSpace(($kafkaDeployment | Out-String).Trim())) {
        Write-Step "Applying LOCAL Kafka startup grace period"

        # Use --patch-file rather than passing JSON directly on the command line.
        # This avoids Windows PowerShell/native-command quoting differences that
        # can make a syntactically successful kubectl call leave the probe unchanged.
        $kafkaPatchObject = @{
            spec = @{
                template = @{
                    spec = @{
                        containers = @(
                            @{
                                name = "kafka"
                                startupProbe = @{
                                    tcpSocket = @{ port = "broker" }
                                    initialDelaySeconds = 10
                                    periodSeconds = 5
                                    timeoutSeconds = 3
                                    successThreshold = 1
                                    failureThreshold = 120
                                }
                            }
                        )
                    }
                }
            }
        }

        $kafkaPatchFile = Join-Path ([System.IO.Path]::GetTempPath()) ("aip-kafka-startup-probe-" + [guid]::NewGuid().ToString("N") + ".json")
        try {
            $kafkaPatchObject | ConvertTo-Json -Depth 10 | Set-Content -Path $kafkaPatchFile -Encoding UTF8
            kubectl patch deployment kafka -n $Namespace --field-manager=helm --type=strategic --patch-file $kafkaPatchFile | Out-Host
            if ($LASTEXITCODE -ne 0) {
                throw "Unable to apply LOCAL Kafka startup probe normalization."
            }
        }
        finally {
            Remove-Item $kafkaPatchFile -Force -ErrorAction SilentlyContinue
        }

        # Fail immediately if the live Deployment did not receive the intended
        # startup probe. Do not wait 12 minutes with an ineffective configuration.
        $liveKafkaProbe = kubectl get deployment kafka -n $Namespace -o jsonpath="{.spec.template.spec.containers[?(@.name=='kafka')].startupProbe.failureThreshold}{'|'}{.spec.template.spec.containers[?(@.name=='kafka')].startupProbe.periodSeconds}{'|'}{.spec.template.spec.containers[?(@.name=='kafka')].startupProbe.timeoutSeconds}{'|'}{.spec.template.spec.containers[?(@.name=='kafka')].startupProbe.initialDelaySeconds}"
        if ($LASTEXITCODE -ne 0) {
            throw "Unable to verify Kafka startup probe normalization."
        }

        if (($liveKafkaProbe | Out-String).Trim() -ne "120|5|3|10") {
            throw "Kafka startup probe normalization was not applied. Live values: $liveKafkaProbe"
        }

        Write-Host "Kafka startup probe verified: failureThreshold=120, periodSeconds=5, timeoutSeconds=3, initialDelaySeconds=10" -ForegroundColor DarkGray
    }

    if ($EnableIbkr) {
        Write-Host "IBKR LOCAL runtime explicitly enabled." -ForegroundColor Yellow
        Set-DeploymentEnvIfExists "broker-service" @("IBKR_ENABLED=true")
    }
    else {
        # IBKR authentication/runtime is currently parked. Keeping its standalone
        # connector enabled makes a fresh local install depend on its PVC/package.
        # Disable the integration and scale the connector to zero for normal DEV.
        Set-DeploymentEnvIfExists "broker-service" @("IBKR_ENABLED=false")
        $ibkrDeployment = kubectl get deployment ibkr-connector -n $Namespace --ignore-not-found -o name 2>$null
        if ($LASTEXITCODE -ne 0) { throw "Unable to query LOCAL ibkr-connector deployment." }
        if (-not [string]::IsNullOrWhiteSpace(($ibkrDeployment | Out-String).Trim())) {
            kubectl scale deployment/ibkr-connector -n $Namespace --replicas=0 | Out-Host
            if ($LASTEXITCODE -ne 0) { throw "Unable to disable LOCAL ibkr-connector deployment." }
        }
    }
}

function Write-FailureDiagnostics {
    # Diagnostics must never hide the original deployment failure. A pod may
    # legitimately have no previous logs when it failed before container start.
    $previousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        Write-Host ""
        Write-Host "================ DEPLOYMENT DIAGNOSTICS ================" -ForegroundColor Yellow
        try { kubectl get pods -n $Namespace -o wide 2>$null | Out-Host } catch {}

        $badPods = @()
        try {
            $badPods = kubectl get pods -n $Namespace --no-headers 2>$null | ForEach-Object {
                $parts = ($_ -split '\s+')
                if ($parts.Count -ge 3 -and ($parts[1] -notmatch '^([1-9][0-9]*)/\1$' -or $parts[2] -ne "Running")) {
                    $parts[0]
                }
            }
        } catch {}

        foreach ($pod in @($badPods | Select-Object -Unique)) {
            if (-not $pod) { continue }

            Write-Host ""
            Write-Host "--- $pod : events ---" -ForegroundColor Yellow
            try {
                kubectl describe pod $pod -n $Namespace 2>$null |
                    Select-String -Pattern "Events:","Warning","Failed","BackOff","Error","Unhealthy","Mount","secret","configmap","scheduling" -Context 0,2 |
                    ForEach-Object { $_.ToString() } |
                    Out-Host
            } catch {}

            Write-Host "--- $pod : previous/current logs ---" -ForegroundColor Yellow
            $previousLogs = $null
            try {
                $previousLogs = kubectl logs $pod -n $Namespace --all-containers --previous --tail=80 2>$null
            } catch {}

            if ($previousLogs) {
                $previousLogs | Out-Host
            }
            else {
                try {
                    kubectl logs $pod -n $Namespace --all-containers --tail=80 2>$null | Out-Host
                } catch {}
            }
        }

        Write-Host "========================================================" -ForegroundColor Yellow
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
}
function Wait-ForDeployments {
    param([string[]]$Names, [switch]$AllEnabled)

    Write-Step "Waiting for deployments to become ready"

    if ($AllEnabled) {
        $deploymentNames = kubectl get deployment -n $Namespace -o jsonpath="{.items[*].metadata.name}"
        if ($LASTEXITCODE -ne 0) { throw "Unable to list deployments in namespace '$Namespace'." }
        $deployments = @($deploymentNames -split " " | Where-Object { $_ })
    }
    else {
        $deployments = @($Names | Select-Object -Unique)
    }

    if (-not $deployments) { throw "No deployments were selected for readiness validation." }

    foreach ($deployment in $deployments) {
        if (-not $EnableIbkr -and $deployment -eq "ibkr-connector") { continue }
        $exists = kubectl get deployment $deployment -n $Namespace --ignore-not-found -o name 2>$null
        if ($LASTEXITCODE -ne 0) { throw "Unable to query deployment '$deployment'." }
        if ([string]::IsNullOrWhiteSpace(($exists | Out-String).Trim())) { continue }

        Write-Step "Waiting for deployment '$deployment'"
        $rolloutTimeout = if ($deployment -eq "kafka") { "720s" } else { "420s" }
        kubectl rollout status deployment/$deployment -n $Namespace --timeout=$rolloutTimeout
        if ($LASTEXITCODE -ne 0) {
            Write-FailureDiagnostics
            throw "Deployment '$deployment' did not become ready. See diagnostics above."
        }
    }

    if ($AllEnabled) {
        $postgresStatefulSet = kubectl get statefulset postgres -n $Namespace --ignore-not-found -o name 2>$null
        if ($LASTEXITCODE -ne 0) { throw "Unable to query PostgreSQL StatefulSet." }
        if (-not [string]::IsNullOrWhiteSpace(($postgresStatefulSet | Out-String).Trim())) {
            Write-Step "Waiting for PostgreSQL"
            kubectl rollout status statefulset/postgres -n $Namespace --timeout=300s
            if ($LASTEXITCODE -ne 0) { Write-FailureDiagnostics; throw "PostgreSQL did not become ready." }
        }
    }
}

function Remove-OldApplicationImages {
    param([Parameter(Mandatory = $true)][string[]]$Names)

    if ($KeepOldImages) {
        Write-Step "Keeping previous component images by request"
        return
    }

    Write-Step "Removing older images for successfully deployed component(s)"
    $selected = @(Get-ImageDefinitions -Names $Names)

    # Host Docker cleanup. Keep only the current immutable tag for each selected
    # application repository. The temporary :dev bootstrap alias is removed too.
    $hostRows = docker images --format "{{.Repository}}|{{.Tag}}|{{.ID}}" 2>$null
    if ($LASTEXITCODE -ne 0) { throw "Unable to enumerate local Docker images for cleanup." }

    foreach ($image in $selected) {
        $suffix = "/ai-investment/$($image.Name)"
        $rows = @($hostRows | ForEach-Object {
            $parts = $_ -split '\|'
            if ($parts.Count -lt 3) { return }
            [pscustomobject]@{ Repository=$parts[0]; Tag=$parts[1]; Id=$parts[2] }
        } | Where-Object {
            ($_.Repository -eq "ai-investment/$($image.Name)" -or $_.Repository.EndsWith($suffix)) -and
            -not ($_.Repository -eq $image.Repo -and $_.Tag -eq $ImageTag)
        })

        foreach ($row in $rows) {
            $ref = "$($row.Repository):$($row.Tag)"
            Write-Host "Removing old host image tag $ref" -ForegroundColor DarkGray
            docker image rm -f $ref *> $null
            if ($LASTEXITCODE -ne 0) { Write-Warning "Unable to remove old host image '$ref'." }
        }
    }

    # k3d node/containerd cleanup. This runs only after successful rollout, so
    # deleting stale image references cannot affect the new running pods.
    $nodeNames = @(docker ps --format "{{.Names}}" | Where-Object {
        $_ -like "k3d-$ClusterName-server-*" -or $_ -like "k3d-$ClusterName-agent-*"
    })

    foreach ($node in $nodeNames) {
        $refs = @(docker exec $node ctr -n k8s.io images ls -q 2>$null)
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "Unable to enumerate containerd images on '$node'; host cleanup still completed."
            continue
        }

        foreach ($image in $selected) {
            $nameToken = "/ai-investment/$($image.Name):"
            $currentTagPattern = "*$nameToken$ImageTag"
            $oldRefs = @($refs | Where-Object {
                $_ -like "*$nameToken*" -and $_ -notlike $currentTagPattern
            } | Select-Object -Unique)

            foreach ($ref in $oldRefs) {
                Write-Host "Removing old k3d image reference on $node -> $ref" -ForegroundColor DarkGray
                docker exec $node ctr -n k8s.io images rm $ref *> $null
                if ($LASTEXITCODE -ne 0) { Write-Warning "Unable to remove '$ref' from node '$node'." }
            }
        }
    }
}


function Resolve-ResearchDatabaseSchemaName {
    # Reads the schema name research-engine is actually configured with
    # (AIP_RESEARCH_DATABASE_SCHEMA on the deployed Deployment) rather than
    # assuming the chart default, so the Radar freshness check always
    # queries the schema that is genuinely in use. Falls back to the
    # chart's documented default ("research") if the Deployment cannot be
    # read or the value is missing/unsafe -- this is only ever used to
    # pick WHICH table to probe; the freshness decision itself remains
    # fail-closed regardless of which schema name is probed.
    param([Parameter(Mandatory = $true)][string]$Namespace)
    $defaultSchema = "research"
    try {
        $schema = kubectl get deployment research-engine -n $Namespace -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="AIP_RESEARCH_DATABASE_SCHEMA")].value}' 2>$null
        if ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace($schema)) {
            $schema = $schema.Trim()
            Test-SafeIdentifier -Value $schema -FieldName "Research database schema"
            return $schema
        }
    }
    catch {
        Write-Warning "Unable to resolve the configured research database schema name from the research-engine Deployment ($($_.Exception.Message)); using the default '$defaultSchema'."
    }
    return $defaultSchema
}

function Invoke-InitialOpportunityCycleAfterCleanDeploy {
    # A clean removes this marker. Therefore only the first successful full `up`
    # after a clean explicitly requests an initial production opportunity cycle.
    # Normal `up` reruns and component deploys do not trigger a cycle; after the
    # initial cycle, the research-engine scheduler owns subsequent executions.
    if (Test-Path $OpportunityBootstrapMarkerFile) {
        Write-Host "Initial opportunity cycle already handled for this LOCAL runtime; scheduler owns future runs." -ForegroundColor DarkGray
        return
    }

    # The local marker file alone is never a reliable signal: a restored,
    # already-populated database (or simply a marker file missing on a
    # different machine/checkout) must never cause an expensive initial
    # Radar cycle to run again against data that already has one. Before
    # relying on the marker's absence, positively confirm the database is
    # genuinely fresh. Require an initdb-only durable provisioning token,
    # claimed atomically at most once within one hour of provisioning.
    # Flyway/seed/reference rows are not evidence of prior use. Existing
    # cycles and users still deny startup, except the single ADMIN account
    # whose creation this invocation positively confirmed. Missing tables,
    # unreadable counts, or a missing/consumed marker always deny eligibility.
    # The separate research-engine scheduler is unchanged.
    Write-Step "Positively verifying database freshness before the automatic initial opportunity cycle"

    $freshProvisioningClaimed = $false
    $cycleTableProbeSucceeded = $false
    $cycleTableExists = $false
    $cycleRowCountProbeSucceeded = $false
    $cycleRowCount = $null
    $userTableProbeSucceeded = $false
    $userTableExists = $false
    $userRowCountProbeSucceeded = $false
    $userRowCount = $null

    try {
        $radarCheckPodName = Get-PostgresPodName -Namespace $Namespace
        $freshProvisioningClaimed = Use-FreshProvisioningMarker -Namespace $Namespace -PodName $radarCheckPodName -DatabaseUser $DatabaseConfig.Username -DatabaseName $DatabaseConfig.Name
        $researchSchema = Resolve-ResearchDatabaseSchemaName -Namespace $Namespace

        # One round trip: positively confirm existence of BOTH the Radar
        # cycle-run table and the auth user table before trusting either.
        $existsResult = Invoke-PodExec -Namespace $Namespace -PodName $radarCheckPodName -Arguments @(
            "psql", "-U", $DatabaseConfig.Username, "-d", $DatabaseConfig.Name, "-At", "-c",
            "select (select exists (select 1 from information_schema.tables where table_schema = '$researchSchema' and table_name = 'global_opportunity_cycle_run'))::text || '|' || " +
            "(select exists (select 1 from information_schema.tables where table_schema = 'auth' and table_name = 'app_users'))::text;"
        )
        if ($existsResult.ExitCode -eq 0) {
            $existsParts = $existsResult.Output.Trim() -split '\|'
            if ($existsParts.Count -eq 2) {
                $cycleTableProbeSucceeded = $true
                $userTableProbeSucceeded = $true
                $cycleTableExists = ($existsParts[0].Trim() -eq "true")
                $userTableExists = ($existsParts[1].Trim() -eq "true")
            }
        }

        if ($cycleTableProbeSucceeded -and $userTableProbeSucceeded -and $cycleTableExists -and $userTableExists) {
            $countResult = Invoke-PodExec -Namespace $Namespace -PodName $radarCheckPodName -Arguments @(
                "psql", "-U", $DatabaseConfig.Username, "-d", $DatabaseConfig.Name, "-At", "-c",
                "select (select count(*) from `"$researchSchema`".global_opportunity_cycle_run)::text || '|' || (select count(*) from `"auth`".app_users)::text;"
            )
            if ($countResult.ExitCode -eq 0) {
                $countParts = $countResult.Output.Trim() -split '\|'
                if ($countParts.Count -eq 2) {
                    $parsedCycleCount = 0L
                    $parsedUserCount = 0L
                    if ([long]::TryParse($countParts[0].Trim(), [ref]$parsedCycleCount)) {
                        $cycleRowCountProbeSucceeded = $true
                        $cycleRowCount = $parsedCycleCount
                    }
                    if ([long]::TryParse($countParts[1].Trim(), [ref]$parsedUserCount)) {
                        $userRowCountProbeSucceeded = $true
                        $userRowCount = $parsedUserCount
                    }
                }
            }
        }
    }
    catch {
        Write-Warning "Unable to check database state for initial opportunity cycle eligibility: $($_.Exception.Message)"
    }

    $eligibility = Resolve-OpportunityRadarBootstrapEligibility -FreshProvisioningClaimed $freshProvisioningClaimed -FirstAdminCreatedThisStartup ([bool]$script:FirstAdminCreatedThisStartup) `
        -CycleTableProbeSucceeded $cycleTableProbeSucceeded -CycleTableExists $cycleTableExists `
        -CycleRowCountProbeSucceeded $cycleRowCountProbeSucceeded -CycleRowCount $cycleRowCount `
        -UserTableProbeSucceeded $userTableProbeSucceeded -UserTableExists $userTableExists `
        -UserRowCountProbeSucceeded $userRowCountProbeSucceeded -UserRowCount $userRowCount

    if (-not $eligibility.Eligible) {
        Write-Host "Initial opportunity cycle skipped ($($eligibility.Decision)): $($eligibility.Reason)" -ForegroundColor Yellow
        Write-Host "Ordinary platform startup continues; the research-engine scheduler's own behavior is unaffected." -ForegroundColor DarkGray
        if ($eligibility.WriteMarker) {
            Set-Content -Path $OpportunityBootstrapMarkerFile -Value (Get-Date).ToString("o") -Encoding ASCII
        }
        return
    }

    Write-Host "New provisioning marker claimed, no existing Radar cycle, and user state verified; proceeding to the universe-readiness-gated initial cycle." -ForegroundColor Green

    # Kubernetes readiness is not sufficient here. portfolio-service can be Ready
    # while CanonicalIdentityBootstrap is still populating the active NSE equity
    # universe. Starting the production cycle before that bootstrap is populated
    # can produce universe_count=0 and an empty Global Opportunity Radar.
    #
    # Use the exact canonical-universe API consumed by GlobalScanner and wait for
    # a production-sized active EQUITY universe before submitting the one-time
    # initial cycle. This is a data-readiness gate, not an arbitrary sleep.
    Write-Step "Waiting for canonical NSE equity universe before initial opportunity cycle"

    $universeReadyCode = @'
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

base_url = "http://portfolio-service"
params = urllib.parse.urlencode({"status": "ACTIVE", "assetType": "EQUITY", "page": 0, "size": 1})
url = f"{base_url}/api/v1/instruments?{params}"
minimum_universe = 2500
poll_seconds = 10
deadline = time.monotonic() + (20 * 60)
last_count = None
last_error = None

while time.monotonic() < deadline:
    try:
        # portfolio-service protects /api/v1/instruments with the same
        # X-AIP-User-* identity contract used by research-engine.  A Kubernetes
        # Ready pod can therefore still return 401 to an anonymous readiness
        # probe.  Use a deterministic server-owned UUID identity for this
        # read-only bootstrap check; do not bypass portfolio authentication.
        headers = {
            "X-AIP-User-Id": "00000000-0000-0000-0000-000000000001",
            "X-AIP-User-Issuer": "platform-bootstrap",
            "X-AIP-User-Subject": "global-opportunity-bootstrap",
            "Accept": "application/json",
        }
        request = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))

        if not isinstance(payload, dict) or not isinstance(payload.get("instruments"), list):
            raise ValueError("Invalid canonical universe response")

        count = int(payload.get("totalElements", len(payload["instruments"])))
        if count != last_count:
            print(f"Canonical active EQUITY universe count: {count}", flush=True)
            last_count = count

        if count >= minimum_universe:
            print(f"Canonical NSE equity universe ready: {count} instruments", flush=True)
            sys.exit(0)

        last_error = f"canonical universe contains only {count} active EQUITY instruments"
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        message = f"HTTP {exc.code}: {body[:500]}"
        if message != last_error:
            print(f"Canonical universe not ready yet: {message}", flush=True)
        last_error = message
    except Exception as exc:
        message = str(exc)
        if message != last_error:
            print(f"Canonical universe not ready yet: {message}", flush=True)
        last_error = message

    time.sleep(poll_seconds)

print(
    f"Timed out waiting for canonical NSE equity universe; last_count={last_count}, last_error={last_error}",
    file=sys.stderr,
)
sys.exit(1)
'@

    $universeReadyBytes = [System.Text.Encoding]::UTF8.GetBytes($universeReadyCode)
    $universeReadyBase64 = [Convert]::ToBase64String($universeReadyBytes)
    $universeReadyLauncher = "import base64;exec(base64.b64decode('$universeReadyBase64'))"

    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        kubectl exec deployment/research-engine -n $Namespace -c research-engine -- python -c $universeReadyLauncher
        $universeReadyExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }

    if ($universeReadyExitCode -ne 0) {
        throw "Canonical NSE equity universe did not become ready. Initial opportunity cycle was NOT submitted and the bootstrap marker was NOT written."
    }

    Write-Step "Triggering initial opportunity cycle for fresh LOCAL runtime"

    # Execute the request from inside the research-engine pod so platform.ps1
    # does not need to create or maintain a local port-forward.
    $pythonCode = @'
import json
import sys
import urllib.error
import urllib.request

url = "http://127.0.0.1:8000/api/v1/research/opportunities/cycles"
payload = json.dumps({"candidate_ids": None, "shortlist_limit": 25, "top_n": 4}).encode("utf-8")
request = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
try:
    with urllib.request.urlopen(request, timeout=15) as response:
        print(response.read().decode("utf-8"))
except urllib.error.HTTPError as exc:
    body = exc.read().decode("utf-8", errors="replace")
    # If another valid production cycle became active after the universe readiness
    # gate, treat HTTP 409 as handled rather than creating a duplicate.
    if exc.code == 409:
        print(body)
        sys.exit(10)
    print(body, file=sys.stderr)
    sys.exit(1)
except Exception as exc:
    print(str(exc), file=sys.stderr)
    sys.exit(1)
'@

    # PowerShell 5 / kubectl can corrupt quotes and newlines when a multiline
    # Python program is passed directly to `python -c`. Encode the program first
    # and pass only a shell-safe Base64 token through kubectl.
    $pythonBytes = [System.Text.Encoding]::UTF8.GetBytes($pythonCode)
    $pythonBase64 = [Convert]::ToBase64String($pythonBytes)
    $pythonLauncher = "import base64;exec(base64.b64decode('$pythonBase64'))"

    # Native stderr is surfaced as PowerShell error records on Windows PowerShell
    # 5 when ErrorActionPreference=Stop. Temporarily allow the native process to
    # finish so its explicit exit code (0 / 10 / failure) remains authoritative.
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $result = kubectl exec deployment/research-engine -n $Namespace -c research-engine -- python -c $pythonLauncher 2>&1
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    $result | Out-Host

    if ($exitCode -eq 0) {
        Set-Content -Path $OpportunityBootstrapMarkerFile -Value (Get-Date).ToString("o") -Encoding ASCII
        Write-Host "Initial opportunity cycle submitted after canonical universe readiness. Future cycles are scheduler-owned." -ForegroundColor Green
        return
    }

    if ($exitCode -eq 10) {
        # Another production cycle became active only after canonical-universe
        # readiness. Never submit a duplicate.
        Set-Content -Path $OpportunityBootstrapMarkerFile -Value (Get-Date).ToString("o") -Encoding ASCII
        Write-Host "An opportunity cycle is already active after canonical universe readiness; no duplicate was submitted. Future cycles are scheduler-owned." -ForegroundColor Yellow
        return
    }

    throw "Unable to submit the initial opportunity cycle after canonical universe readiness. The bootstrap marker was not written, so a later full 'up' can retry safely."
}

function Invoke-FirstAdminBootstrapIfNeeded {
    $script:FirstAdminCreatedThisStartup = $false
    # Feature: automatic first-ADMIN initialization.
    #
    # Safe to call on every `up`: the decision (create vs. skip vs. fail
    # closed) is made inside one locked transaction in the auth-service
    # "first-admin-bootstrap" Spring profile (FirstAdminBootstrapService),
    # using the same auth.role_admin_lock row RoleAdminService already uses.
    # This function only has to (a) run that profile once as a disposable
    # Job cloned from the already-deployed auth-service container spec, so
    # it inherits the exact running image and database/JWT configuration
    # without this script re-deriving or guessing any of it, and (b) supply
    # a freshly generated password through a short-lived Secret, never as a
    # literal value or command-line argument.
    #
    # Every outcome other than a confirmed CREATED or a confirmed
    # ADMIN_ALREADY_PRESENT is a hard failure: missing auth-service
    # resources, a Job that never completes, an unrecognized exit code, and
    # even exit code 0 without a recognized outcome string in the logs must
    # never be silently treated as a successful bootstrap.
    Write-Step "Checking for first-ADMIN bootstrap"

    $deploymentJsonRaw = kubectl get deployment auth-service -n $Namespace --ignore-not-found -o json 2>$null
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace(($deploymentJsonRaw | Out-String).Trim())) {
        throw "First-ADMIN bootstrap cannot proceed: the auth-service Deployment was not found in namespace '$Namespace'. Refusing to continue '.\platform.ps1 up' with an unknown ADMIN-provisioning outcome."
    }

    $deployment = $deploymentJsonRaw | ConvertFrom-Json
    $container = @($deployment.spec.template.spec.containers | Where-Object { $_.name -eq "auth-service" })[0]
    if (-not $container) {
        throw "First-ADMIN bootstrap cannot proceed: no 'auth-service' container was found in the auth-service Deployment spec. Refusing to continue '.\platform.ps1 up' with an unknown ADMIN-provisioning outcome."
    }

    $email = if ($FirstAdminEmail) { $FirstAdminEmail }
             elseif ($env:AIP_FIRST_ADMIN_EMAIL) { $env:AIP_FIRST_ADMIN_EMAIL }
             else { "prakhar.gitlab@gmail.com" }

    # Generated locally, kept only in memory, never written to a persistent
    # file and never embedded as a literal in the submitted Job manifest or
    # on any command line (New-OrReplaceSecretFromDataValues builds the
    # Secret's base64 "data" directly and applies it via a temp-file
    # manifest, exactly like New-OrReplaceLiteralSecret does for its own
    # rendered manifest -- it just never puts the value on a kubectl
    # argument in the first place).
    $passwordBytes = New-Object byte[] 24
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($passwordBytes)
    $generatedPassword = ([Convert]::ToBase64String($passwordBytes) -replace '[+/=]', '9') + "Aa1!"

    $secretName = "first-admin-bootstrap-credentials"
    $jobName = "first-admin-bootstrap"
    $jobFile = Join-Path $RuntimeDir "first-admin-bootstrap-job.json"
    $secretCreated = $false
    $jobSubmitted = $false

    try {
        New-OrReplaceSecretFromDataValues $secretName @{
            FIRST_ADMIN_BOOTSTRAP_EMAIL    = $email
            FIRST_ADMIN_BOOTSTRAP_PASSWORD = $generatedPassword
        }
        $secretCreated = $true

        kubectl delete job $jobName -n $Namespace --ignore-not-found | Out-Null

        $jobEnv = @($container.env | Where-Object { $_.name -notin @("FIRST_ADMIN_BOOTSTRAP_EMAIL", "FIRST_ADMIN_BOOTSTRAP_PASSWORD") })
        $jobEnv += [ordered]@{ name = "FIRST_ADMIN_BOOTSTRAP_EMAIL"; valueFrom = @{ secretKeyRef = @{ name = $secretName; key = "FIRST_ADMIN_BOOTSTRAP_EMAIL" } } }
        $jobEnv += [ordered]@{ name = "FIRST_ADMIN_BOOTSTRAP_PASSWORD"; valueFrom = @{ secretKeyRef = @{ name = $secretName; key = "FIRST_ADMIN_BOOTSTRAP_PASSWORD" } } }

        $job = [ordered]@{
            apiVersion = "batch/v1"
            kind       = "Job"
            metadata   = @{ name = $jobName; namespace = $Namespace }
            spec       = @{
                backoffLimit            = 0
                ttlSecondsAfterFinished = 300
                activeDeadlineSeconds   = 120
                template                = @{
                    spec = @{
                        restartPolicy = "Never"
                        containers    = @(
                            @{
                                name            = "first-admin-bootstrap"
                                image           = $container.image
                                imagePullPolicy = $container.imagePullPolicy
                                command         = @("/bin/sh", "-c")
                                args            = @("exec java -jar /app/app.jar --spring.profiles.active=first-admin-bootstrap --spring.main.web-application-type=none")
                                env             = $jobEnv
                            }
                        )
                    }
                }
            }
        }

        try {
            $job | ConvertTo-Json -Depth 20 | Set-Content -Path $jobFile -Encoding UTF8
            kubectl apply -f $jobFile | Out-Host
            if ($LASTEXITCODE -ne 0) { throw "Unable to submit the first-ADMIN bootstrap Job." }
            $jobSubmitted = $true
        }
        finally {
            Remove-Item $jobFile -Force -ErrorAction SilentlyContinue
        }

        kubectl wait --for=condition=complete --timeout=90s "job/$jobName" -n $Namespace 2>$null | Out-Null

        $podName = (kubectl get pods -n $Namespace -l "job-name=$jobName" -o jsonpath='{.items[0].metadata.name}' 2>$null)
        if ([string]::IsNullOrWhiteSpace($podName)) {
            throw "First-ADMIN bootstrap Job '$jobName' produced no pod. The ADMIN-provisioning outcome is unknown; refusing to treat this as success."
        }

        $logs = kubectl logs $podName -n $Namespace 2>$null
        $podExitCode = kubectl get pod $podName -n $Namespace -o jsonpath='{.status.containerStatuses[0].state.terminated.exitCode}' 2>$null
        if ([string]::IsNullOrWhiteSpace($podExitCode)) {
            throw "First-ADMIN bootstrap Job '$jobName' pod '$podName' has no terminated exit code (it may still be running or was never scheduled). The ADMIN-provisioning outcome is unknown; refusing to treat this as success."
        }

        switch ($podExitCode) {
            "0" {
                if ($logs -match "first_admin_bootstrap_outcome=CREATED") {
                    $script:FirstAdminCreatedThisStartup = $true
                    Write-Host ""
                    Write-Host "============================================================" -ForegroundColor Yellow
                    Write-Host " FIRST ADMIN ACCOUNT CREATED - SAVE THIS PASSWORD NOW" -ForegroundColor Yellow
                    Write-Host "============================================================" -ForegroundColor Yellow
                    Write-Host " Email    : $email"
                    Write-Host " Password : $generatedPassword"
                    Write-Host " This password will not be shown again. Only its hash is stored." -ForegroundColor Yellow
                    Write-Host "============================================================" -ForegroundColor Yellow
                    Write-Host ""
                }
                elseif ($logs -match "first_admin_bootstrap_outcome=ADMIN_ALREADY_PRESENT") {
                    Write-Host "An ADMIN account already exists; first-ADMIN bootstrap made no changes." -ForegroundColor DarkGray
                }
                else {
                    throw "First-ADMIN bootstrap Job exited 0 but its logs contain neither 'first_admin_bootstrap_outcome=CREATED' nor 'first_admin_bootstrap_outcome=ADMIN_ALREADY_PRESENT'. The ADMIN-provisioning outcome is ambiguous; refusing to treat exit code 0 alone as proof of success."
                }
            }
            "3" {
                throw "Accounts exist but none holds ADMIN. Automatic first-ADMIN bootstrap refused to act (fail-closed). Use the role-admin-cli profile after explicit authorized review (see docs/auth/admin-role-provisioning.md), then re-run '.\platform.ps1 up'."
            }
            default {
                throw "First-ADMIN bootstrap did not complete cleanly (pod exit code: '$podExitCode'). Refusing to treat this as success; review the output above, resolve the underlying issue, and retry with '.\platform.ps1 up'."
            }
        }
    }
    finally {
        if ($jobSubmitted -or (kubectl get job $jobName -n $Namespace --ignore-not-found -o name 2>$null)) {
            kubectl delete job $jobName -n $Namespace --ignore-not-found | Out-Null
        }
        if ($secretCreated -or (kubectl get secret $secretName -n $Namespace --ignore-not-found -o name 2>$null)) {
            kubectl delete secret $secretName -n $Namespace --ignore-not-found | Out-Null
        }
        $generatedPassword = $null
    }
}

function Stop-LegacyFrontendPortForward {
    # Cleanup only. New platform runs never create a direct frontend port-forward
    # because it bypasses the API Gateway and breaks same-origin /api requests.
    if (Test-Path $LegacyPortForwardPidFile) {
        $rawPid = (Get-Content -Raw $LegacyPortForwardPidFile).Trim()
        if ($rawPid -match '^\d+$') {
            $process = Get-Process -Id ([int]$rawPid) -ErrorAction SilentlyContinue
            if ($process) {
                Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
            }
        }
        Remove-Item $LegacyPortForwardPidFile -Force -ErrorAction SilentlyContinue
    }
}

function Test-HttpUrl {
    param([string]$Url, [int]$TimeoutSeconds = 5)
    try {
        $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec $TimeoutSeconds
        return ($response.StatusCode -ge 200 -and $response.StatusCode -lt 500)
    }
    catch {
        # 401/403/404 are still proof that the HTTP endpoint is reachable, but
        # Resolve-ApplicationUrl tests the root UI path and expects a normal 2xx.
        return $false
    }
}

function Resolve-ApplicationUrl {
    Write-Step "Verifying gateway application URL"

    Stop-LegacyFrontendPortForward

    $deadline = (Get-Date).AddSeconds(120)
    do {
        if (Test-HttpUrl $IngressApplicationUrl) {
            Set-Content -Path $ApplicationUrlFile -Value $IngressApplicationUrl -Encoding ASCII
            return $IngressApplicationUrl
        }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)

    Write-FailureDiagnostics
    throw "Gateway application URL '$IngressApplicationUrl' did not become reachable. Direct frontend port-forward fallback is intentionally disabled because it bypasses API Gateway same-origin routing."
}

function Show-ApplicationUrl {
    if (Test-Path $ApplicationUrlFile) {
        $url = (Get-Content -Raw $ApplicationUrlFile).Trim()
        if ($url) {
            Write-Host ""
            Write-Host "Application URL: $url" -ForegroundColor Green
            return $url
        }
    }
    Write-Host "No saved application URL exists. Run '.\platform.ps1 up' first." -ForegroundColor Yellow
    return $null
}

function Show-Status {
    Require-Command "k3d"; Require-Command "kubectl"; Require-Command "helm"
    if (-not (Test-ClusterExists)) {
        Write-Host "Local cluster '$ClusterName' does not exist. Run '.\platform.ps1 up'." -ForegroundColor Yellow
        return
    }

    Select-ClusterContext
    Write-Step "Cluster nodes"; kubectl get nodes -o wide
    Write-Step "Application pods"; kubectl get pods -n $Namespace -o wide
    Write-Step "Application services"; kubectl get svc -n $Namespace
    Write-Step "Helm release"; helm list -n $Namespace
    Show-ApplicationUrl | Out-Null
}

function Start-Platform {
    Assert-LocalPrerequisites
    Ensure-RuntimeDirectory
    Initialize-ImageTag
    Ensure-Cluster
    Ensure-DevSecrets
    Validate-Helm

    if ($Component -or $Profile) {
        throw "-Component/-Profile are only valid with the 'deploy' command. Use '.\platform.ps1 deploy -Profile research-ui'."
    }
    $selected = @((Get-ImageDefinitions).Name)

    if (-not $SkipBuild) {
        Assert-BuildPrerequisites
        Build-Images -Names $selected
    }
    else {
        Write-Step "Skipping image build by request"
    }

    Import-ImagesIntoK3d -Names $selected
    Deploy-Helm
    Set-ImmutableDeploymentImages -Names $selected
    Apply-LocalRuntimeNormalization
    Wait-ForDeployments -AllEnabled
    Assert-ImmutableDeploymentImages -Names $selected
    Invoke-FirstAdminBootstrapIfNeeded
    Invoke-InitialOpportunityCycleAfterCleanDeploy

    $url = Resolve-ApplicationUrl
    Save-SuccessfulFullImageTag
    Remove-OldApplicationImages -Names $selected

    Write-Host ""
    Write-Host "============================================================" -ForegroundColor Green
    Write-Host " AI Investment Platform is ready" -ForegroundColor Green
    Write-Host "============================================================" -ForegroundColor Green
    Write-Host "Application URL : $url" -ForegroundColor Green
    Write-Host "Namespace       : $Namespace"
    Write-Host "Cluster         : $ClusterName"
    Write-Host "Image tag       : $ImageTag"
    Write-Host "Build cache     : $(if ($NoCache) { 'disabled' } else { 'enabled (source changes still rebuild)' })"
    Write-Host "Old images      : $(if ($KeepOldImages) { 'kept' } else { 'cleaned after successful rollout' })"
    Write-Host "Yahoo MCP       : enabled by DEV Helm values"
    Write-Host "IBKR runtime    : $(if ($EnableIbkr) { 'enabled' } else { 'disabled for normal LOCAL development' })"
    Write-Host ""
    Write-Host "Component deploy: .\\platform.ps1 deploy -Profile research-ui"
    Write-Host "Status command  : .\\platform.ps1 status"
    Write-Host "Stop command    : .\\platform.ps1 down"
    Write-Host ""

    if (-not $NoBrowser) {
        try { Start-Process $url | Out-Null }
        catch { Write-Warning "Unable to open the browser automatically. Open $url manually." }
    }
}

function Deploy-SelectedComponents {
    Assert-LocalPrerequisites
    Ensure-RuntimeDirectory

    if (-not (Test-ClusterExists)) {
        throw "Component deployment requires an existing local cluster. Run '.\\platform.ps1 up' once to create the full stack."
    }

    Ensure-Cluster
    Ensure-Namespace

    $release = helm status $ReleaseName -n $Namespace -o json 2>$null
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace(($release | Out-String).Trim())) {
        throw "Component deployment requires an existing Helm release. Run '.\\platform.ps1 up' once first."
    }

    if ($SkipBuild) {
        throw "-SkipBuild is intentionally disabled for component deploys. Component deploys always build a fresh image from the current working tree."
    }

    Initialize-ImageTag
    $selected = @(Resolve-SelectedComponents -RequireSelection)

    Write-Host "Selected components: $($selected -join ', ')" -ForegroundColor Cyan

    if (@($selected | Where-Object { $_ -in $JavaServices }).Count -gt 0) { Assert-BuildPrerequisites }
    Build-Images -Names $selected

    Import-ImagesIntoK3d -Names $selected
    Set-ImmutableDeploymentImages -Names $selected

    # Only normalize selected DB-backed Java services. Avoid touching/restarting
    # unrelated Deployments during a component-only rollout.
    foreach ($service in @($selected | Where-Object { $_ -in $DatabaseJavaServices })) {
        Set-DeploymentEnvIfExists $service @(
            "DB_POOL_MAXIMUM_SIZE=8",
            "DB_POOL_MINIMUM_IDLE=1",
            "DB_POOL_CONNECTION_TIMEOUT_MS=10000",
            "DB_POOL_IDLE_TIMEOUT_MS=600000",
            "DB_POOL_MAX_LIFETIME_MS=1800000"
        )
    }
    if ($selected -contains "broker-service") {
        Set-DeploymentEnvIfExists "broker-service" @("IBKR_ENABLED=$(if ($EnableIbkr) { 'true' } else { 'false' })")
    }

    Wait-ForDeployments -Names $selected
    Assert-ImmutableDeploymentImages -Names $selected
    $url = Resolve-ApplicationUrl
    Remove-OldApplicationImages -Names $selected

    Write-Host ""
    Write-Host "============================================================" -ForegroundColor Green
    Write-Host " Component deployment completed" -ForegroundColor Green
    Write-Host "============================================================" -ForegroundColor Green
    Write-Host "Components      : $($selected -join ', ')"
    Write-Host "Image tag       : $ImageTag"
    Write-Host "Application URL : $url"
    Write-Host "Old images      : $(if ($KeepOldImages) { 'kept' } else { 'cleaned for selected components' })"
    Write-Host ""

    if (-not $NoBrowser) {
        try { Start-Process $url | Out-Null } catch {}
    }
}

function Stop-Platform {
    Require-Command "k3d"
    Stop-LegacyFrontendPortForward

    if (Test-ClusterExists) {
        Write-Step "Stopping local k3d cluster '$ClusterName' (preserving PostgreSQL/PVC state)"
        k3d cluster stop $ClusterName
        if ($LASTEXITCODE -ne 0) { throw "Failed to stop k3d cluster '$ClusterName'." }
        Write-Host "Local platform stopped. Cluster and persistent state were preserved." -ForegroundColor Green
    }
    else {
        Write-Host "Local cluster '$ClusterName' is already absent." -ForegroundColor Yellow
    }

    Remove-Item $ApplicationUrlFile -Force -ErrorAction SilentlyContinue
}

function Remove-AllHostApplicationImages {
    Require-Command "docker"

    Write-Step "Removing all local Docker application image tags for AI Investment Platform"

    $previousTag = $script:ImageTag
    if (-not $script:ImageTag) { $script:ImageTag = "clean-preview" }
    try {
        $componentNames = @((Get-ImageDefinitions).Name)
        $rows = @(docker images --format "{{.Repository}}|{{.Tag}}" 2>$null)
        if ($LASTEXITCODE -ne 0) {
            throw "Unable to enumerate local Docker images during clean."
        }

        foreach ($row in $rows) {
            $parts = $row -split '\|', 2
            if ($parts.Count -ne 2) { continue }
            $repository = $parts[0]
            $tag = $parts[1]

            $matchesPlatform = $false
            foreach ($name in $componentNames) {
                if ($repository -eq "ai-investment/$name" -or
                    $repository -eq "$ImageRegistry/ai-investment/$name" -or
                    $repository.EndsWith("/ai-investment/$name")) {
                    $matchesPlatform = $true
                    break
                }
            }

            if (-not $matchesPlatform) { continue }
            if ($tag -eq "<none>") { continue }

            $ref = "$repository`:$tag"
            Write-Host "Removing platform image $ref" -ForegroundColor DarkGray
            docker image rm -f $ref *> $null
            if ($LASTEXITCODE -ne 0) {
                Write-Warning "Unable to remove platform image '$ref'."
            }
        }
    }
    finally {
        $script:ImageTag = $previousTag
    }
}

function Clean-Platform {
    Assert-LocalPrerequisites
    Stop-LegacyFrontendPortForward

    if (-not $ForceClean) {
        Write-Host ""
        Write-Host "WARNING: CLEAN IS DESTRUCTIVE" -ForegroundColor Red
        Write-Host "This will delete k3d cluster '$ClusterName' and its LOCAL Kubernetes/Helm/PVC state." -ForegroundColor Yellow
        Write-Host "It will also remove all local Docker image tags belonging to this platform." -ForegroundColor Yellow
        Write-Host "PostgreSQL data stored in the local cluster/PVC will be deleted." -ForegroundColor Yellow
        Write-Host "Source code is NOT deleted." -ForegroundColor Green
        Write-Host ""
        $confirmation = Read-Host "Type CLEAN to continue"
        if ($confirmation -cne "CLEAN") {
            Write-Host "Clean cancelled; no destructive action was taken." -ForegroundColor Yellow
            return
        }
    }

    if (Test-ClusterExists) {
        Write-Step "Deleting local k3d cluster '$ClusterName'"
        k3d cluster delete $ClusterName
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to delete k3d cluster '$ClusterName'."
        }
    }
    else {
        Write-Host "Local cluster '$ClusterName' is already absent." -ForegroundColor Yellow
    }

    Remove-AllHostApplicationImages

    Write-Step "Removing local deployment runtime markers"
    Remove-Item $ApplicationUrlFile -Force -ErrorAction SilentlyContinue
    Remove-Item $ImageTagFile -Force -ErrorAction SilentlyContinue
    Remove-Item $LegacyPortForwardPidFile -Force -ErrorAction SilentlyContinue
    Remove-Item $OpportunityBootstrapMarkerFile -Force -ErrorAction SilentlyContinue

    Write-Host ""
    Write-Host "============================================================" -ForegroundColor Green
    Write-Host " AI Investment Platform local clean completed" -ForegroundColor Green
    Write-Host "============================================================" -ForegroundColor Green
    Write-Host "Cluster/runtime state : deleted"
    Write-Host "Platform image tags   : deleted from host Docker"
    Write-Host "Source code            : preserved"
    Write-Host ""
    Write-Host "Fresh install command : .\platform.ps1 up -NoCache -NoBrowser" -ForegroundColor Cyan
    Write-Host ""
}

switch ($Command) {
    "up"            { Start-Platform }
    "deploy"        { Deploy-SelectedComponents }
    "components"    { Show-Components }
    "down"          { Stop-Platform }
    "clean"         { Clean-Platform }
    "status"        { Show-Status }
    "url"           { Show-ApplicationUrl | Out-Null }
    "backup"        {
        Invoke-PlatformBackup -ProjectRoot $ProjectRoot -Namespace $Namespace -ClusterName $ClusterName -DatabaseConfig $DatabaseConfig | Out-Null
    }
    "backups"       {
        Get-PlatformBackups -ProjectRoot $ProjectRoot
    }
    "backup-verify" {
        if (-not $Backup) { throw "Specify -Backup latest or -Backup <id>." }
        $ok = Invoke-PlatformBackupVerify -ProjectRoot $ProjectRoot -Namespace $Namespace -Backup $Backup
        if (-not $ok) { exit 1 }
    }
    "restore"       {
        if (-not $Backup) { throw "Specify -Backup latest or -Backup <id>." }
        # research-engine writes to its own "research" schema in the same
        # database and must be scaled down/up alongside the Java services
        # that write to it -- there is no separate special case for it.
        $restoreDatabaseWriterServices = @($DatabaseJavaServices) + @("research-engine")
        Invoke-PlatformRestore -ProjectRoot $ProjectRoot -Namespace $Namespace -ClusterName $ClusterName `
            -DatabaseConfig $DatabaseConfig -Backup $Backup -DatabaseWriterServices $restoreDatabaseWriterServices `
            -TargetDatabase $TargetDatabase -AuthorizeDestructiveRestore:$AuthorizeDestructiveRestore `
            -SkipSafetyBackup:$SkipSafetyBackup -ConfirmSafetyBackup:$ConfirmSafetyBackup
    }
}
