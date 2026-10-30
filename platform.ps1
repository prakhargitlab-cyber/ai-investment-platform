param(
    [ValidateSet("up", "deploy", "down", "clean", "status", "url", "components", "backup", "backups", "backup-verify", "restore", "reconcile-rollback-baseline", "approve-rollback-baseline", "recover-interrupted-promotion", "validate-research-migration")]
    [string]$Command = "up",

    # For `deploy`, choose individual components. Example:
    #   .\platform.ps1 deploy -Component frontend,research-engine
    [string[]]$Component,

    # Optional named deployment profile. Supported values are printed by
    # `.\platform.ps1 components`.
    [string]$Profile,

    [switch]$DryRun,
    [switch]$AcceptBootstrapRisk,
    [switch]$ConfirmCallerFreeze,
    # Resume only the explicitly reviewed, still-isolated LOCAL deployment.
    [string]$ResumeAuditTag,
    # Recovery-only research-engine deployment while exactly this one lapsed
    # manual Equity cycle remains RUNNING/slotted. Both are required; see
    # Get-InterruptedCycleRecoveryRequest. Never valid for other deployments.
    [string]$RecoverInterruptedCycleId,
    [switch]$AuthorizeInterruptedCycleRecovery,
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
    [switch]$ConfirmSafetyBackup,

    # Rollback-baseline reconciliation and approval.
    [string[]]$ReconcileComponent,
    [string]$ReconciliationReason,
    [switch]$AuthorizeRollbackBaselineReconciliation,
    [string]$RollbackBaselineCandidatePath,
    [switch]$AuthorizeRollbackBaselineApproval,

    # One-time interrupted-promotion recovery (never deploys/builds/patches;
    # see Invoke-InterruptedPromotionRecovery in rollback-baseline.ps1).
    [string]$RecoveryTag,
    [string[]]$RecoveryComponent,
    [switch]$AuthorizeRecovery,

    # Research migration validation.
    [switch]$AuthorizeMigrationCleanup
)

$ErrorActionPreference = "Stop"
if (($RecoverInterruptedCycleId -or $AuthorizeInterruptedCycleRecovery) -and $Command -ne 'deploy') {
    throw 'Interrupted-cycle recovery parameters are valid only for: deploy -Component research-engine'
}

# StrictMode safety net: this script is sometimes invoked (via '&') from a
# caller that has already set Set-StrictMode -Version Latest in its own
# scope -- that setting cascades into this script's execution. A handful of
# existing guards below (e.g. "if ($script:DeploymentRun)",
# "if (-not $script:VerifiedDeploymentDigests)") rely on PowerShell's normal,
# non-strict treatment of an unset variable as falsy/empty. Initializing
# them here makes those same guards StrictMode-safe without changing their
# behavior under ordinary (non-strict) execution and without altering any
# risk-classification decision logic.
$script:DeploymentRun = $null
$script:VerifiedDeploymentDigests = @{}
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
# - up imports images into k3d; component deploy pushes through the existing registry.
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
. (Join-Path $ProjectRoot "scripts\database\rollback-baseline.ps1")
. (Join-Path $ProjectRoot "scripts\database\validate-research-migration.ps1")

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

$QuietPeriodSeconds = 600

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
    if ($Command -ne "deploy") { Require-Command "k3d" }
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
        # --kubeconfig-merge-default is REQUIRED so k3d writes the live API endpoint
        # (including the current host port for the load balancer) into the default
        # kubeconfig file ($HOME/.kube/config). Without it, k3d v5.x silently writes
        # only to its own ~/.config/k3d/kubeconfig-<cluster>.yaml and exits 0,
        # leaving a stale ~/.kube/config server URL that causes "connection refused"
        # on cluster restarts that change the LB host port.
        k3d kubeconfig merge $ClusterName --kubeconfig-merge-default --kubeconfig-switch-context
    }
}

function Ensure-Cluster {
    # Reuses Test-ClusterExists instead of re-running its own
    # k3d-cluster-list-and-match check (the two were previously
    # independent copies of the same validation).
    #
    # Also records whether the cluster was just created vs. already
    # existed, so Invoke-FirstAdminBootstrapIfNeeded can tell a genuinely
    # fresh install apart from a routine re-run of `up` without
    # duplicating auth-service's own CREATED/ADMIN_ALREADY_PRESENT logic.
    $script:ClusterWasJustCreated = $false
    if (Test-ClusterExists) {
        Invoke-Checked "Starting existing k3d cluster '$ClusterName'" {
            k3d cluster start $ClusterName
        }
    }
    else {
        Invoke-Checked "Creating k3d cluster '$ClusterName'" {
            k3d cluster create --config $K3dConfig
        }
        $script:ClusterWasJustCreated = $true
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

# Strict-mode-safe init-container lookup. Under Set-StrictMode, a direct
# '.initContainers' property access THROWS "The property 'initContainers'
# cannot be found on this object" for any Deployment JSON that has no
# initContainers key at all (true for mcp-gateway and yahoo-finance-mcp --
# only research-engine has one). .psobject.Properties[...] lookup is always
# safe, strict mode or not, and returns an empty array rather than throwing
# when the property is absent.
function Get-DeploymentInitContainers {
    param($Deployment)
    $specNode = $Deployment.spec.template.spec
    $prop = $specNode.psobject.Properties['initContainers']
    if ($null -eq $prop -or $null -eq $prop.Value) { return @() }
    return @($prop.Value)
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
            if ($container.name -eq $deploymentName -or $container.image -like "$($image.Repo):*" -or $container.image -like "$($image.Repo.Replace('localhost:5001/', 'ai-investment-registry:5000/')):*") {
                $containerPatch += @{ name = $container.name; image = (Get-DeploymentImage $image) }
            }
        }
        foreach ($container in (Get-DeploymentInitContainers $deployment)) {
            if ($null -ne $container -and ($container.name -eq $deploymentName -or $container.image -like "$($image.Repo):*" -or $container.image -like "$($image.Repo.Replace('localhost:5001/', 'ai-investment-registry:5000/')):*")) {
                $initContainerPatch += @{ name = $container.name; image = (Get-DeploymentImage $image) }
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
            if ($container.name -eq $image.Name -or $container.image -like "$($image.Repo):*" -or $container.image -like "$($image.Repo.Replace('localhost:5001/', 'ai-investment-registry:5000/')):*") { $matchingImages += $container.image }
        }
        foreach ($container in (Get-DeploymentInitContainers $deployment)) {
            if ($null -ne $container -and ($container.name -eq $image.Name -or $container.image -like "$($image.Repo):*" -or $container.image -like "$($image.Repo.Replace('localhost:5001/', 'ai-investment-registry:5000/')):*")) { $matchingImages += $container.image }
        }

        $wrong = @($matchingImages | Where-Object { $_ -ne (Get-DeploymentImage $image) })
        if ($matchingImages.Count -eq 0) { $failures += "$($image.Name): no matching container image found" }
        elseif ($wrong.Count -gt 0) { $failures += "$($image.Name): expected '$(Get-DeploymentImage $image)', found '$($wrong -join ', ')'" }
        else { Write-Host "$($image.Name) -> $(Get-DeploymentImage $image)" -ForegroundColor DarkGray }
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
        # Stage 2E: Never trigger automatic Radar for a restored database.
        # Check restore history before claiming the fresh provisioning marker.
        if (Test-DatabaseRestoreRecorded `
                -Namespace $Namespace `
                -PodName $radarCheckPodName `
                -DatabaseUser $DatabaseConfig.Username `
                -DatabaseName $DatabaseConfig.Name) {

            Write-Host "Initial Radar skipped: database has a durable restore record." -ForegroundColor Yellow
            return
        }
        $freshProvisioningClaimed = Use-FreshProvisioningMarker -Namespace $Namespace -PodName $radarCheckPodName -DatabaseUser $DatabaseConfig.Username -DatabaseName $DatabaseConfig.Name
        $researchSchema = Resolve-ResearchDatabaseSchemaName -Namespace $Namespace

        # One round trip: positively confirm existence of BOTH the Radar
        # cycle-run table and the auth user table before trusting either.
        $existsResult = Invoke-PodExec -Namespace $Namespace -PodName $radarCheckPodName -Arguments @(
            "psql", "-U", $DatabaseConfig.Username, "-d", $DatabaseConfig.Name, "-At", "-c",
            ("select (select exists (select 1 from information_schema.tables where table_schema = '$researchSchema' and table_name = 'global_opportunity_cycle_run'))::text || '|' || " +
            "(select exists (select 1 from information_schema.tables where table_schema = 'auth' and table_name = 'app_users'))::text;")
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

    Write-Host "New provisioning marker claimed, no existing Radar cycle, and user state verified." -ForegroundColor Green

    # research-engine requires authenticated caller identity (the same
    # X-AIP-User-* contract portfolio-service enforces) on this endpoint, and
    # correctly rejects an unauthenticated POST with HTTP 401 ("Market data
    # population access denied"). platform.ps1 has no ADMIN/user credential to
    # present here, and must never fabricate or inject one -- doing so would
    # weaken backend authentication rather than work within it. So `up` no
    # longer submits an initial Radar cycle automatically; it only records that
    # the one-time bootstrap window was observed and handled, and leaves
    # submitting the first cycle to an authenticated ADMIN/user workflow
    # (UI action or an authenticated API call) after setup completes. The
    # research-engine scheduler and recovery configuration are unchanged and
    # continue to own subsequent Radar executions exactly as before.
    Set-Content -Path $OpportunityBootstrapMarkerFile -Value (Get-Date).ToString("o") -Encoding ASCII
    Write-Host "Initial Radar cycle was NOT auto-submitted (requires an authenticated ADMIN/user request)." -ForegroundColor Yellow
    Write-Host "Submit the first Equity/ETF Radar cycle via an authenticated workflow once setup finishes; the scheduler owns all cycles after that." -ForegroundColor DarkGray
}

function Invoke-FirstAdminBootstrapIfNeeded {
    $script:FirstAdminCreatedThisStartup = $false
    $bootstrapPod = Get-PostgresPodName -Namespace $Namespace
    if (Test-DatabaseRestoreRecorded -Namespace $Namespace -PodName $bootstrapPod -DatabaseUser $DatabaseConfig.Username -DatabaseName $DatabaseConfig.Name) {
        Write-Host "Automatic first-ADMIN bootstrap skipped: this database has a durable restore record." -ForegroundColor Yellow
        return
    }
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

    # The ADMIN password is either entered securely by the operator (masked,
    # with confirmation) or, if left blank, generated locally. Either way it
    # is kept only in memory, never written to a persistent file, never
    # logged, and never embedded as a literal in the submitted Job manifest
    # or on any command line (New-OrReplaceSecretFromDataValues builds the
    # Secret's base64 "data" directly and applies it via a temp-file
    # manifest, exactly like New-OrReplaceLiteralSecret does for its own
    # rendered manifest -- it just never puts the value on a kubectl
    # argument in the first place). This prompt runs before the bootstrap
    # attempt itself decides CREATED vs. ADMIN_ALREADY_PRESENT, so it has no
    # effect -- and the password is never used -- when an ADMIN already exists.
    # Only prompt on a genuinely fresh install (this `up` just created the
    # k3d cluster). On a routine re-run against an already-existing cluster,
    # an ADMIN almost certainly already exists, and this call is a no-op
    # (see ADMIN_ALREADY_PRESENT below) -- prompting there would block every
    # ordinary `up` for no reason. If that heuristic is ever wrong (e.g. the
    # auth database was reset on an existing cluster), this falls back to
    # the same auto-generated-password behavior this script already had.
    $enteredAdminPassword = $null
    if ($script:ClusterWasJustCreated) {
        $enteredAdminPassword = Convert-SecureStringToPlainText (Read-Host "ADMIN password (leave blank to auto-generate)" -AsSecureString)
    }
    $adminBootstrapPassword = $null
    $adminPasswordWasGenerated = $false
    if ([string]::IsNullOrEmpty($enteredAdminPassword)) {
        $passwordBytes = New-Object byte[] 24
        [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($passwordBytes)
        $adminBootstrapPassword = ([Convert]::ToBase64String($passwordBytes) -replace '[+/=]', '9') + "Aa1!"
        $adminPasswordWasGenerated = $true
    }
    else {
        $confirmAdminPassword = Convert-SecureStringToPlainText (Read-Host "Confirm ADMIN password" -AsSecureString)
        if ($confirmAdminPassword -cne $enteredAdminPassword) {
            $enteredAdminPassword = $null
            $confirmAdminPassword = $null
            throw "ADMIN password entries did not match. Re-run '.\platform.ps1 up' and enter the same password twice, or leave both blank to auto-generate one."
        }
        $confirmAdminPassword = $null
        $adminBootstrapPassword = $enteredAdminPassword
        $enteredAdminPassword = $null
    }

    $secretName = "first-admin-bootstrap-credentials"
    $jobName = "first-admin-bootstrap"
    $jobFile = Join-Path $RuntimeDir "first-admin-bootstrap-job.json"
    $secretCreated = $false
    $jobSubmitted = $false

    try {
        New-OrReplaceSecretFromDataValues $secretName @{
            FIRST_ADMIN_BOOTSTRAP_EMAIL    = $email
            FIRST_ADMIN_BOOTSTRAP_PASSWORD = $adminBootstrapPassword
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
                    Write-Host " FIRST ADMIN ACCOUNT CREATED" -ForegroundColor Yellow
                    Write-Host "============================================================" -ForegroundColor Yellow
                    Write-Host " Email    : $email"
                    if ($adminPasswordWasGenerated) {
                        Write-Host " Password : $adminBootstrapPassword"
                        Write-Host " This password will not be shown again. Only its hash is stored." -ForegroundColor Yellow
                    }
                    else {
                        Write-Host " Password : (the one you entered)"
                        Write-Host " It will not be echoed again. Only its hash is stored." -ForegroundColor Yellow
                    }
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
        $adminBootstrapPassword = $null
        $enteredAdminPassword = $null
        $confirmAdminPassword = $null
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

function Assert-ExistingCluster {
    $expected = "k3d-$ClusterName"
    $context = kubectl config current-context
    if ($LASTEXITCODE -ne 0 -or ($context | Out-String).Trim() -ne $expected) {
        throw "Select the existing context first: kubectl config use-context $expected"
    }
    $ready = kubectl --context $expected get --raw=/readyz --request-timeout=10s
    if ($LASTEXITCODE -ne 0 -or ($ready | Out-String).Trim() -ne "ok") {
        throw "Existing cluster '$expected' is unavailable; deployment will not create or start a cluster."
    }
    $nodes = kubectl --context $expected get nodes -o json --request-timeout=10s | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0 -or -not $nodes.items) { throw "Cannot validate existing cluster nodes." }
    foreach ($node in $nodes.items) {
        if (-not @($node.status.conditions | Where-Object { $_.type -eq "Ready" -and $_.status -eq "True" }).Count) {
            throw "Cluster node '$($node.metadata.name)' is not Ready."
        }
    }
}

function Get-DeploymentImage {
    param($Image)
    if ($Command -eq "deploy") { return $Image.Image.Replace("localhost:5001/", "ai-investment-registry:5000/") }
    return $Image.Image
}

function Push-SelectedImages {
    param([string[]]$Names)
    foreach ($image in Get-ImageDefinitions -Names $Names) {
        Invoke-Checked "Pushing $($image.Image)" { docker push $image.Image }
    }
}

# This narrow path deliberately leaves other component/up workflows unchanged.
function ConvertTo-DeploymentArgument {
    param([AllowEmptyString()][string]$Value)
    # Windows CommandLineToArgvW/CRT quoting, including empty args and trailing slashes.
    if ($Value.Length -gt 0 -and $Value -notmatch '[\s"]') { return $Value }
    $out='"'; $slashes=0
    foreach($char in $Value.ToCharArray()) {
        if ($char -eq '\') { $slashes++; continue }
        if ($char -eq '"') { $out+=('\' * ($slashes*2+1))+'"' }
        else { $out+=('\' * $slashes)+$char }
        $slashes=0
    }
    return $out+('\' * ($slashes*2))+'"'
}

function Invoke-DeploymentNativeCommand {
    param([string]$Program,[string[]]$Arguments)
    $file=(Get-Command $Program -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
    $info=New-Object Diagnostics.ProcessStartInfo
    $info.FileName=$file
    $quoted=(@($Arguments | ForEach-Object { ConvertTo-DeploymentArgument $_ }) -join ' ')
    if ([IO.Path]::GetExtension($file) -in @('.cmd','.bat')) {
        # Maven's Windows launcher needs cmd.exe. Reject shell expansion rather
        # than silently reinterpret an argument. Current scoped Maven args qualify.
        if ($file -match '["&|<>^%!\r\n]' -or @($Arguments | Where-Object {$_ -match '["&|<>^%!\r\n]'}).Count) { throw 'Unsafe batch launcher argument' }
        $info.FileName=$env:ComSpec
        $info.Arguments='/d /s /c ""'+$file+'" '+$quoted+'"'
    } else { $info.Arguments=$quoted }
    $info.UseShellExecute=$false; $info.CreateNoWindow=$true
    $info.RedirectStandardOutput=$true; $info.RedirectStandardError=$true
    $process=New-Object Diagnostics.Process
    $process.StartInfo=$info
    try {
        $null=$process.Start()
        $stdout=$process.StandardOutput.ReadToEndAsync()
        $stderr=$process.StandardError.ReadToEndAsync()
        $process.WaitForExit()
        return [pscustomobject]@{ExitCode=$process.ExitCode;StdOut=$stdout.Result;StdErr=$stderr.Result}
    } finally { $process.Dispose() }
}

function Set-DeploymentPhase {
    param([string]$Name)
    if ($script:DeploymentRun) { $script:DeploymentRun.Phase=$Name }
    Write-Host "DEPLOYMENT PHASE: $Name"
}

function Initialize-DeploymentDiagnostics {
    param([string[]]$Names)
    $script:DeploymentRun=@{Phase='ARTIFACT_PREFLIGHT';Names=$Names;Built=@();Pushed=@();PatchAttempts=@();Patched=@();Verified=@();BaselineImages=@{};LastCommand=$null;Failure=$null}
}

function Invoke-DeploymentCommand {
    param([string]$Program, [string[]]$Arguments)
    $command=$Program+' '+(@($Arguments | ForEach-Object {ConvertTo-DeploymentArgument $_}) -join ' ')
    if ($script:DeploymentRun) {
        $script:DeploymentRun.LastCommand=$command
        if ($Program -eq 'kubectl' -and $Arguments -contains 'patch') {
            $at=[Array]::IndexOf($Arguments,'patch'); $script:DeploymentRun.PatchAttempts+= $Arguments[$at+2]
        }
    }
    $result=Invoke-DeploymentNativeCommand $Program $Arguments
    # Native stderr must not become a PowerShell terminating NativeCommandError
    # before stdout and the real exit code have been captured.
    if ($result.StdErr) { Write-Host $result.StdErr }
    if ($result.ExitCode -ne 0) {
        $phase=if($script:DeploymentRun){$script:DeploymentRun.Phase}else{'UNSPECIFIED'}
        $failure=@{Phase=$phase;Command=$command;ExitCode=$result.ExitCode;StdOut=$result.StdOut;StdErr=$result.StdErr}
        if ($script:DeploymentRun) { $script:DeploymentRun.Failure=$failure }
        Write-Host "FAILED PHASE: $phase"
        Write-Host "FAILED COMMAND: $command"
        if ($result.StdOut) { Write-Host "Original stdout: $($result.StdOut)" }
        Write-Host "Original stderr: $($result.StdErr)"
        throw "$Program failed (exit $($result.ExitCode)) in $phase. Deployment stopped; no automatic rollback."
    }
    if ($script:DeploymentRun) {
        if ($Program -eq 'docker' -and $Arguments[0] -eq 'build') { $script:DeploymentRun.Built+=$Arguments[[Array]::IndexOf($Arguments,'-t')+1] }
        if ($Program -eq 'docker' -and $Arguments[0] -eq 'push') { $script:DeploymentRun.Pushed+=$Arguments[1] }
        if ($Program -eq 'kubectl' -and $Arguments -contains 'patch') { $at=[Array]::IndexOf($Arguments,'patch');$script:DeploymentRun.Patched+=$Arguments[$at+2] }
    }
    return $result.StdOut
}

function Get-DeploymentFailureState {
    $state=@{Verified=$false;Deployments=@();Isolation='UNKNOWN';ResearchRecovery='UNKNOWN';Scheduler='UNKNOWN';ImagesMatchBaseline=$false}
    try {
        $prefix=@('--context','k3d-ai-investment-dev','-n','ai-investment','--request-timeout=10s')
        $d=Invoke-DeploymentNativeCommand kubectl ($prefix+@('get','deployments','-o','json'))
        $p=Invoke-DeploymentNativeCommand kubectl ($prefix+@('get','networkpolicies','-o','json'))
        if ($d.ExitCode -ne 0 -or $p.ExitCode -ne 0) { return $state }
        $deployments=($d.StdOut | ConvertFrom-Json).items
        $policies=($p.StdOut | ConvertFrom-Json).items
        $state.Isolation=if(@($policies | Where-Object {$_.metadata.name -eq 'research-bootstrap-isolation'}).Count){'ACTIVE'}else{'ABSENT'}
        foreach($name in @('api-gateway','research-engine','portfolio-service','frontend')) {
            $item=@($deployments | Where-Object {$_.metadata.name -eq $name})
            if ($item.Count -ne 1) { return $state }
            $main=@($item[0].spec.template.spec.containers | Where-Object name -eq $name)
            if ($main.Count -ne 1) { return $state }
            $state.Deployments+=@{Name=$name;Image=$main[0].image;Ready=$item[0].status.readyReplicas;Desired=$item[0].spec.replicas;Generation=$item[0].metadata.generation;ObservedGeneration=$item[0].status.observedGeneration}
            if ($name -eq 'research-engine') {
                $state.ResearchRecovery=($main[0].env | Where-Object name -eq 'AIP_RESEARCH_OPPORTUNITY_RECOVERY_PAUSED').value
                $state.Scheduler=($main[0].env | Where-Object name -eq 'AIP_RESEARCH_OPPORTUNITY_SCHEDULER_ENABLED').value
            }
        }
        $state.ImagesMatchBaseline=$true
        foreach($name in $script:DeploymentRun.Names) {
            if (-not $script:DeploymentRun.BaselineImages.ContainsKey($name) -or ($state.Deployments | Where-Object Name -eq $name).Image -ne $script:DeploymentRun.BaselineImages[$name]) { $state.ImagesMatchBaseline=$false }
        }
        $state.Verified=$true
    } catch { $state.Verified=$false }
    return $state
}

function Show-DeploymentFailure {
    param($ErrorRecord)
    $state=Get-DeploymentFailureState
    $run=$script:DeploymentRun
    Write-Host "DEPLOYMENT STOPPED: phase=$($run.Phase); error=$($ErrorRecord.Exception.Message)"
    if (-not $run.Failure) { Write-Host "Last command/operation: $($run.LastCommand); native exit code: not available for this validation failure" }
    Write-Host ('Progress: '+(@{Built=$run.Built;Pushed=$run.Pushed;PatchAttempts=$run.PatchAttempts;Patched=$run.Patched;Verified=$run.Verified} | ConvertTo-Json -Compress))
    Write-Host ('Current read-only state: '+($state | ConvertTo-Json -Depth 8 -Compress))
    $ready=@($state.Deployments | Where-Object {$_.Ready -lt 1 -or $_.Ready -ne $_.Desired -or $_.Generation -ne $_.ObservedGeneration}).Count -eq 0
    if ($state.Verified -and $state.ImagesMatchBaseline -and $state.Isolation -eq 'ABSENT' -and $ready -and $run.PatchAttempts.Count -eq 0) {
        Write-Host 'Fresh attempt: CONDITIONAL, from the beginning only after the reported cause is fixed and deployment is approved. All mandatory checks will rerun.'
    } else {
        Write-Host 'Fresh attempt: BLOCKED. Inspect/reconcile isolation and workload state with the saved baseline; do not automatically retry or roll back research.'
    }
    Write-Host 'No cleanup, retry, network-policy change or rollback was performed by failure diagnostics.'
}

function Invoke-DeploymentKubectl {
    param([string[]]$Arguments)
    Invoke-DeploymentCommand 'kubectl' (@('--context', 'k3d-ai-investment-dev', '-n', 'ai-investment') + $Arguments)
}

function Get-VerifiedRegistryImage {
    param([ValidateSet('api-gateway','research-engine','portfolio-service','frontend')][string]$Name, [string]$Reference)
    if ($Reference -notmatch '^(sha256:[a-f0-9]{64}|[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})$') { throw 'Invalid image reference' }
    $base = "http://localhost:5001/v2/ai-investment/$Name"
    $accept = 'application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json'
    $pending = New-Object 'System.Collections.Generic.Queue[string]'
    $pending.Enqueue($Reference)
    $seen = @{}
    $rootDigest = $null
    $runnable = $false
    $verifiedConfigDigests = @()
    while ($pending.Count -gt 0) {
        $ref = $pending.Dequeue()
        if ($seen.ContainsKey($ref)) { continue }
        if ($seen.Count -ge 32) { throw 'Unexpectedly large image index' }
        $seen[$ref] = $true
        if ($script:DeploymentRun) { $script:DeploymentRun.LastCommand="HTTP GET $base/manifests/$ref" }
        $response = Invoke-WebRequest -Uri "$base/manifests/$ref" -Headers @{ Accept=$accept } -UseBasicParsing -TimeoutSec 30 -MaximumRedirection 0
        if ($response.StatusCode -ne 200) { throw 'Manifest unavailable' }
        $bytes = if ($response.Content -is [byte[]]) { $response.Content } else { [Text.Encoding]::UTF8.GetBytes([string]$response.Content) }
        $sha = [Security.Cryptography.SHA256]::Create()
        try { $digest = 'sha256:' + ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-','').ToLowerInvariant() } finally { $sha.Dispose() }
        if ($response.Headers['Docker-Content-Digest'] -ne $digest -or ($ref.StartsWith('sha256:') -and $ref -ne $digest)) { throw 'Manifest digest mismatch' }
        if (-not $rootDigest) { $rootDigest = $digest }
        $manifest = [Text.Encoding]::UTF8.GetString($bytes) | ConvertFrom-Json
        if ($manifest.schemaVersion -ne 2) { throw 'Unsupported manifest schema' }
        if ($manifest.mediaType -in @('application/vnd.oci.image.index.v1+json','application/vnd.docker.distribution.manifest.list.v2+json')) {
            if (-not $manifest.manifests) { throw 'Empty image index' }
            foreach ($child in $manifest.manifests) {
                if ($child.digest -notmatch '^sha256:[a-f0-9]{64}$') { throw 'Invalid child digest' }
                if ($child.platform.os -eq 'linux' -and $child.platform.architecture -eq 'amd64') {
                    $pending.Enqueue($child.digest)
                }
            }
        } elseif ($manifest.mediaType -in @('application/vnd.oci.image.manifest.v1+json','application/vnd.docker.distribution.manifest.v2+json')) {
            if (-not $manifest.config -or -not $manifest.layers) { throw 'Incomplete image manifest' }
            foreach ($blob in @($manifest.config) + @($manifest.layers)) {
                if ($blob.digest -notmatch '^sha256:[a-f0-9]{64}$') { throw 'Invalid blob digest' }
                if ($script:DeploymentRun) { $script:DeploymentRun.LastCommand="HTTP HEAD $base/blobs/$($blob.digest)" }
                $head = Invoke-WebRequest -Uri "$base/blobs/$($blob.digest)" -Method Head -UseBasicParsing -TimeoutSec 30 -MaximumRedirection 0
                if ($head.StatusCode -ne 200 -or [long]$head.Headers['Content-Length'] -ne [long]$blob.size) { throw 'Missing or incomplete image blob' }
            }
            # Every image manifest (direct or via OCI index) must have its
            # configuration blob downloaded and SHA-256 verified against the
            # manifest's config.digest, then parsed to confirm os=linux and
            # architecture=amd64.
            if ($script:DeploymentRun) { $script:DeploymentRun.LastCommand="HTTP GET $base/blobs/$($manifest.config.digest)" }
            $configResponse = Invoke-WebRequest -Uri "$base/blobs/$($manifest.config.digest)" -UseBasicParsing -TimeoutSec 30 -MaximumRedirection 0
            if ($configResponse.StatusCode -ne 200) { throw 'Config digest mismatch' }
            $configBytes = if ($configResponse.Content -is [byte[]]) { $configResponse.Content } else { [Text.Encoding]::UTF8.GetBytes([string]$configResponse.Content) }
            $hash = [Security.Cryptography.SHA256]::Create()
            try { $configDigest = 'sha256:' + ([BitConverter]::ToString($hash.ComputeHash($configBytes))).Replace('-','').ToLowerInvariant() } finally { $hash.Dispose() }
            if ($configDigest -ne $manifest.config.digest) { throw 'Config digest mismatch' }
            $config = [Text.Encoding]::UTF8.GetString($configBytes) | ConvertFrom-Json
            if ($config.os -eq 'linux' -and $config.architecture -eq 'amd64') {
                $runnable = $true
                if (-not $verifiedConfigDigests) { $verifiedConfigDigests = @() }
                $verifiedConfigDigests += $configDigest
            }
        } else { throw 'Unsupported manifest media type' }
    }
    if (-not $runnable) { throw 'No linux/amd64 image verified' }
    $imageReference="ai-investment-registry:5000/ai-investment/${Name}@$rootDigest"
    if (-not $script:VerifiedDeploymentDigests) { $script:VerifiedDeploymentDigests=@{} }
    $script:VerifiedDeploymentDigests[$imageReference]=@($rootDigest)+@($verifiedConfigDigests)+@($seen.Keys | Where-Object { $_ -match '^sha256:[a-f0-9]{64}$' })
    return $imageReference
}

# Targeted deployments do not invoke Helm upgrade. Read the explicitly selected
# LOCAL values file instead of hardcoding the Full ETF feature flag.
# Fail closed on missing or ambiguous configuration; do not silently enable it.
function Get-LocalFullEtfExecutionFlag {
    if (-not (Test-Path -LiteralPath $HelmValues -PathType Leaf)) { throw 'Missing LOCAL Helm values' }
    $lines = @(Get-Content -LiteralPath $HelmValues -ErrorAction Stop)
    $matches = @($lines | Where-Object { $_ -match '^  etfFullUniverseExecutionEnabled:\s*(true|false)\s*(?:#.*)?$' })
    if ($matches.Count -ne 1) { throw 'LOCAL Full ETF feature flag missing or ambiguous' }
    if ($matches[0] -notmatch '^  etfFullUniverseExecutionEnabled:\s*(true|false)\s*(?:#.*)?$') { throw 'Invalid LOCAL Full ETF feature flag' }
    return $Matches[1].ToLowerInvariant()
}

function New-VerifiedDeploymentPatch {
    param($Deployment, [string]$Name, [string]$Image)
    if ($Name -notin @('api-gateway','research-engine','portfolio-service','frontend') -or $Deployment.metadata.name -ne $Name -or $Deployment.metadata.namespace -ne 'ai-investment') { throw 'Unexpected deployment identity' }
    if ($Image -notmatch "^ai-investment-registry:5000/ai-investment/$Name@sha256:[a-f0-9]{64}$") { throw 'Deployment requires verified digest' }
    if (@($Deployment.spec.template.spec.containers | Where-Object { $_.name -eq $Name }).Count -ne 1) { throw "Missing/duplicate container: $Name" }
    $initCount=@($Deployment.spec.template.spec.initContainers | Where-Object {$null -ne $_}).Count
    if (($Name -eq 'research-engine' -and $initCount -ne 1) -or ($Name -ne 'research-engine' -and $initCount -ne 0)) { throw "Unexpected init container layout: $Name ($initCount)" }
    if (-not $Deployment.metadata.resourceVersion) { throw 'Missing deployment resource version' }
    $container = @{ name=$Name; image=$Image }
    $spec = @{ containers=@($container) }
    if ($Name -eq 'research-engine') {
        if (@($Deployment.spec.template.spec.initContainers | Where-Object { $_.name -eq 'wait-for-research-migrations' }).Count -ne 1) { throw 'Missing migration-wait init container' }
        $spec.initContainers = @(@{ name='wait-for-research-migrations'; image=$Image })
        $container.env = @(
            @{ name='AIP_RESEARCH_OPPORTUNITY_RECOVERY_PAUSED'; value='true'; valueFrom=$null },
            @{ name='AIP_RESEARCH_OPPORTUNITY_SCHEDULER_ENABLED'; value='false'; valueFrom=$null },
            @{ name='AIP_RESEARCH_ETF_FULL_UNIVERSE_EXECUTION_ENABLED'; value=(Get-LocalFullEtfExecutionFlag); valueFrom=$null }
        )
    }
    return @{ metadata=@{resourceVersion=$Deployment.metadata.resourceVersion}; spec=@{template=@{spec=$spec}} }
}

# Definitions only. Loaded by platform.ps1; every external boundary is mockable.
function Invoke-DeploymentObservation {
    param($Gate)
    $source=Get-Content -Raw (Join-Path $ProjectRoot 'scripts\database\deployment_observation.py')
    $expected=if($Gate.Migrations){$Gate.Migrations | ConvertTo-Json -Compress}else{'null'}
    if ($Gate.Recovery) {
        # Values were strictly validated (UUID/market allow-list); no quoting is possible.
        $recovery=[ordered]@{cycle_id=[string]$Gate.Recovery.cycle_id;market=[string]$Gate.Recovery.market} | ConvertTo-Json -Compress
        if ($recovery -notmatch '^\{"cycle_id":"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}","market":"NSE"\}$') { throw 'Invalid interrupted-cycle recovery request' }
        $source += "`nraise SystemExit(main(json.loads('$expected'), json.loads('$recovery')))`n"
    } else {
        $source += "`nraise SystemExit(main(json.loads('$expected')))`n"
    }
    $encoded=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($source))
    $result=Invoke-DeploymentKubectl @('exec',"pod/$($Gate.ResearchPod)",'-c','research-engine','--','python','-c',"import base64;exec(base64.b64decode('$encoded'))")
    $value=($result | Out-String) | ConvertFrom-Json
    if ($value.fingerprint -notmatch '^[a-f0-9]{64}$') { throw 'Database observation missing fingerprint' }
    if ($value.status -eq 'UNKNOWN') { throw 'Database observation UNKNOWN' }
    if ($Gate.Recovery) { return (Assert-RecoveryObservation $Gate $value) }
    # Incomplete refresh runs have no authoritative worker-termination proof.
    # Pausing admission, isolation, stable pod identity, quiet logs and unchanged
    # fingerprints cannot exclude a silent worker. Preserve every BLOCKED result.
    if ($value.status -ne 'QUIET_SNAPSHOT') { throw 'Database observation UNKNOWN/BLOCKED' }
    return $value
}

function Get-InterruptedCycleRecoveryRequest {
    # Explicit operator authorization for a recovery-only research-engine
    # deployment. Returns $null (ordinary gate, unchanged) unless requested.
    param([string[]]$Names)
    if (-not $RecoverInterruptedCycleId -and -not $AuthorizeInterruptedCycleRecovery) { return $null }
    if (-not $RecoverInterruptedCycleId -or -not $AuthorizeInterruptedCycleRecovery) { throw 'Interrupted-cycle recovery requires both -RecoverInterruptedCycleId and -AuthorizeInterruptedCycleRecovery' }
    if ($RecoverInterruptedCycleId -cnotmatch '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$') { throw 'Invalid interrupted-cycle id' }
    if (@($Names).Count -ne 1 -or $Names[0] -ne 'research-engine') { throw 'Interrupted-cycle recovery deploys research-engine alone' }
    if ($DryRun -or $ResumeAuditTag -or $Profile) { throw 'Interrupted-cycle recovery is incompatible with -DryRun, -ResumeAuditTag and -Profile' }
    if (-not $AcceptBootstrapRisk -or -not $ConfirmCallerFreeze) { throw 'Explicit bootstrap risk acceptance and caller freeze are required' }
    return @{cycle_id=$RecoverInterruptedCycleId;market='NSE'}
}

function Assert-RecoveryObservation {
    # Every recovery observation must be a RECOVERY_SNAPSHOT for exactly the
    # authorized cycle, and must equal the first one in ownership, lease,
    # resume count, update time, error code, slot and checkpoint.
    param($Gate,$Value)
    if ((Get-DeploymentOptionalProperty $Value 'status') -ne 'RECOVERY_SNAPSHOT' -or @(Get-DeploymentOptionalProperty $Value 'reasons' | Where-Object { $_ }).Count) { throw 'Recovery observation UNKNOWN/BLOCKED' }
    $r=Get-DeploymentOptionalProperty $Value 'recovery'
    $field=@{}
    foreach ($name in @('cycle_id','market','status','owner_id','lease_expires_at','lease_expired','resume_count','updated_at','error_code','checkpoint_rows','checkpoint_fingerprint')) { $field[$name]=Get-DeploymentOptionalProperty $r $name }
    if ($null -eq $r -or $field.cycle_id -cne $Gate.Recovery.cycle_id -or $field.market -cne $Gate.Recovery.market -or $field.status -cne 'RUNNING' -or
        [string]::IsNullOrWhiteSpace([string]$field.owner_id) -or $field.lease_expired -isnot [bool] -or -not $field.lease_expired -or
        [string]::IsNullOrWhiteSpace([string]$field.lease_expires_at) -or [string]$field.checkpoint_fingerprint -notmatch '^[a-f0-9]{64}$') { throw 'Recovery invariants not satisfied' }
    $pin=[ordered]@{owner_id=[string]$field.owner_id;lease_expires_at=[string]$field.lease_expires_at;resume_count=[string]$field.resume_count;updated_at=[string]$field.updated_at;error_code=[string]$field.error_code;slot=(Get-DeploymentOptionalProperty $r 'slot' | ConvertTo-Json -Compress -Depth 5);checkpoint_rows=[string]$field.checkpoint_rows;checkpoint_fingerprint=[string]$field.checkpoint_fingerprint} | ConvertTo-Json -Compress
    if (-not $Gate.RecoveryPin) {
        $Gate.RecoveryPin=$pin
        $Gate.RecoveryOwner=[string]$field.owner_id
    } elseif ($pin -cne $Gate.RecoveryPin) { throw 'Interrupted cycle ownership/lease/checkpoint/slot changed' }
    return $Value
}

function Assert-RecoveryWorkerExited {
    # The old worker must have logged loss of ownership for exactly the pinned
    # owner in the CURRENT container (restarts are excluded by pod identity),
    # and nothing may have claimed, resumed or touched the cycle since.
    param($Gate)
    if (-not $Gate.Recovery -or -not $Gate.RecoveryOwner) { throw 'Recovery evidence unavailable' }
    $id=[regex]::Escape($Gate.Recovery.cycle_id)
    $owner=[regex]::Escape($Gate.RecoveryOwner)
    $logs=@(Invoke-DeploymentKubectl @('logs',$Gate.ResearchPod,'-c','research-engine','--timestamps'))
    $lost=-1
    for ($i=0; $i -lt $logs.Count; $i++) {
        if ([string]$logs[$i] -cmatch "opportunity_cycle_ownership_lost cycleId=$id owner=$owner(\s|$)") { $lost=$i; break }
    }
    if ($lost -lt 0) { throw 'Old worker exit not evidenced: no ownership-lost record for the interrupted cycle owner' }
    for ($i=$lost+1; $i -lt $logs.Count; $i++) {
        $line=[string]$logs[$i]
        if ($line -match 'opportunity_cycle_(claimed|claim_skipped|resume_enqueued|operator_resume_admitted|cancel_drain_takeover|owned_elsewhere|lease_renew)|takeover|acquisition|Traceback' -or
            ($line -match $id -and $line -cnotmatch "opportunity_cycle_status_suppressed cycleId=$id ")) { throw 'Interrupted cycle activity after ownership loss' }
    }
}

function Assert-RecoveryNotResumed {
    # Post-rollout: the replacement pod (recovery paused) must not have
    # claimed, resumed or referenced the interrupted cycle.
    param($Gate)
    $id=[regex]::Escape($Gate.Recovery.cycle_id)
    foreach ($line in @(Invoke-DeploymentKubectl @('logs',$Gate.ResearchPod,'-c','research-engine','--timestamps'))) {
        if ([string]$line -match $id -or [string]$line -match 'opportunity_cycle_(claimed|resume_enqueued|operator_resume_admitted|cancel_drain_takeover)|takeover') { throw 'Replacement worker touched the interrupted cycle' }
    }
}

function Get-DeploymentInventory {
    $pods=Invoke-DeploymentKubectl @('get','pods','--all-namespaces','-o','json') | ConvertFrom-Json
    if (@($pods.items | Where-Object {$_.metadata.namespace -notin @('ai-investment','kube-system')}).Count) { throw 'Unreviewed cross-namespace callers' }
    $jobs=Invoke-DeploymentKubectl @('get','jobs,cronjobs','--all-namespaces','-o','json') | ConvertFrom-Json
    # Strict-mode-safe: a Kubernetes Job's status.active is 'omitempty' --
    # a Job with zero currently-active pods has NO 'active' key in its JSON
    # at all, so a direct '.status.active' access throws under
    # Set-StrictMode instead of evaluating as zero. Missing is the
    # documented meaning of zero active pods and is genuinely safe to treat
    # as not-active; anything present but not a clean non-negative integer
    # is treated as active (fail-closed) rather than silently passed, so an
    # ambiguous Job status still blocks this gate instead of being read as
    # quiet. CronJob's spec.suspend is left exactly as-is: unlike status.active
    # it is not reproducing any failure here.
    if (@($jobs.items | Where-Object {
        if ($_.kind -eq 'CronJob') { -not $_.spec.suspend }
        elseif ($_.kind -eq 'Job') {
            $activeRaw = Get-DeploymentOptionalProperty (Get-DeploymentOptionalProperty $_ 'status') 'active'
            if ($null -eq $activeRaw) { $false }
            else {
                $parsed = 0L
                if (-not [int64]::TryParse([string]$activeRaw, [ref]$parsed)) { $true }
                else { $parsed -gt 0 }
            }
        } else { $false }
    }).Count) { throw 'Scheduled/internal work is not frozen' }
    # Strict-mode-safe: PodSpec.hostNetwork is 'omitempty' -- the vast
    # majority of pods (hostNetwork=false, the safe default) omit the key
    # entirely, so a direct '.spec.hostNetwork' access throws under
    # Set-StrictMode. Missing is the documented meaning of false/not-host-
    # network and is genuinely safe; anything present but not a real
    # boolean is treated as host-network (fail-closed) rather than
    # silently passed.
    if (@($pods.items | Where-Object {
        $hostNetworkRaw = Get-DeploymentOptionalProperty $_.spec 'hostNetwork'
        $isHostNetwork = if ($null -eq $hostNetworkRaw) { $false } elseif ($hostNetworkRaw -is [bool]) { $hostNetworkRaw } else { $true }
        $isHostNetwork -and $_.metadata.namespace -ne 'kube-system'
    }).Count) { throw 'Host-network caller cannot be isolated' }
    # Do not terminate unrelated PIDs. Require operators to close API-server tunnels.
    $tunnels=@(Get-CimInstance Win32_Process -Filter "Name = 'kubectl.exe'" | Where-Object { $_.CommandLine -match '\b(port-forward|proxy)\b' })
    if ($tunnels.Count) { throw 'Operator kubectl tunnels must be closed before deployment' }
    return @{items=@($pods.items | Where-Object {$_.metadata.namespace -eq 'ai-investment'})}
}

# A K3s kube-router denial is an ICMP rejection, not timeout(124).
# Only accept that outcome when the matching policy chain and terminal
# REJECT rule exist on the node hosting this exact research pod, AND its
# packet counter increases during the probe. This is corroborating evidence,
# not per-connection attribution; require a frozen caller set. No generic exit-code-1 bypass.
function Get-VerifiedResearchRejectCounter {
    param($Gate)
    if (-not $Gate -or $Gate.PodIP -notmatch '^10\.42\.\d+\.\d+$') { throw 'Invalid isolated research pod address' }
    $pod = Invoke-DeploymentKubectl @('get','pod',$Gate.ResearchPod,'-o','json') | ConvertFrom-Json
    if ($pod.metadata.uid -ne $Gate.ResearchUID -or $pod.status.podIP -ne $Gate.PodIP) { throw 'Research pod identity drift' }
    $node = [string]$pod.spec.nodeName
    if ($node -notmatch '^k3d-ai-investment-dev-(agent-[0-9]+|server-0)$') { throw 'Unexpected research node' }
    $saved = Invoke-DeploymentNativeCommand docker @('exec',$node,'iptables-save','-t','filter')
    if ($saved.ExitCode -ne 0) { throw 'Cannot verify node firewall' }
    $ip = [regex]::Escape($Gate.PodIP)
    $podJumps = @($saved.StdOut -split "`n" | Where-Object { $_ -match "^-A KUBE-ROUTER-FORWARD -d $ip/32 .* -j (KUBE-POD-FW-[A-Z0-9]+)" })
    if ($podJumps.Count -lt 1) { throw 'Research firewall routing chain absent' }
    $null = $podJumps[0] -match '-j (KUBE-POD-FW-[A-Z0-9]+)'
    $chain = $Matches[1]
    $jump = @($saved.StdOut -split "`n" | Where-Object { $_ -match "^-A $chain -d $ip/32 .*research-bootstrap-isolation.* -j KUBE-NWPLCY-[A-Z0-9]+" })
    if ($jump.Count -ne 1) { throw 'Isolation policy firewall jump missing' }
    if (-not ($saved.StdOut -split "`n" | Where-Object { $_ -match "^-A $chain .* -j REJECT --reject-with icmp-port-unreachable" })) { throw 'Expected kube-router reject rule absent' }
    $counterResult = Invoke-DeploymentNativeCommand docker @('exec',$node,'iptables','-t','filter','-L',$chain,'-n','-v','-x')
    if ($counterResult.ExitCode -ne 0) { throw 'Cannot inspect firewall packet counter' }
    $reject = @($counterResult.StdOut -split "`n" | Where-Object { $_ -match '^\s*(\d+)\s+\d+\s+REJECT\s+' })
    if ($reject.Count -ne 1 -or $reject[0] -notmatch '^\s*(\d+)\s+\d+\s+REJECT\s+') { throw 'Ambiguous reject counter' }
    return [long]$Matches[1]
}

function Invoke-IsolationProbe {
    param([string]$Pod,[string]$HostName,[int]$Port,[switch]$Denied)
    if ($Pod -notmatch '^[a-z0-9.-]+$' -or $HostName -notmatch '^[a-z0-9.-]+$' -or $Port -notin @(80,8000)) { throw 'Invalid probe address' }
    # timeout 124 specifically distinguishes an isolation timeout from a missing
    # binary, refused connection, DNS failure, or unhealthy HTTP response.
    $probe="timeout 3 bash -c 'exec 3<>/dev/tcp/$HostName/$Port; printf `"GET /health HTTP/1.0\r\nHost: research-engine\r\n\r\n`" >&3; head -n 1 <&3'"
    if ($HostName -eq 'research-service') { $probe=$probe.Replace('GET /health ','GET /actuator/health/readiness ') }
    $encoded=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($probe))
    if ($Denied) {
        if (-not $script:ActiveIsolationGate) { throw 'Missing isolation gate for denied probe' }
        $gate=$script:ActiveIsolationGate
        if ($Pod -notin $gate.Callers -or
            -not (($HostName -eq $gate.ServiceIP -and $Port -eq 80) -or
                   ($HostName -eq $gate.PodIP -and $Port -eq 8000))) {
            throw 'Denied probe does not target the reviewed research service/pod'
        }
        $caller=Invoke-DeploymentKubectl @('get','pod',$Pod,'-o','json') | ConvertFrom-Json
        if ($caller.status.phase -ne 'Running' -or -not $caller.status.podIP) { throw 'Denied probe caller unavailable' }
        $before=Get-VerifiedResearchRejectCounter $gate
        $result=Invoke-DeploymentNativeCommand kubectl (@('--context','k3d-ai-investment-dev','-n','ai-investment','exec',"pod/$Pod",'--','bash','-c',"echo '$encoded' | base64 -d | bash"))
        $after=Get-VerifiedResearchRejectCounter $gate
        $refused=($result.StdErr -match 'Connection refused' -and $result.StdErr -match 'command terminated with exit code 1|/dev/tcp/')
        if ($result.ExitCode -ne 1 -or -not $refused -or $after -le $before) {
            throw "Research ingress isolation REJECT proof failed: caller=$Pod target=${HostName}:$Port exit=$($result.ExitCode) refused=$refused counter=$before->$after"
        }
        return
    }
    $out=(Invoke-DeploymentKubectl @('exec',"pod/$Pod",'--','bash','-c',"echo '$encoded' | base64 -d | bash") | Out-String).Trim()
    if ($out -notmatch '^HTTP/1\.[01] 200\b' -or $out -match 'ISOLATED') { throw 'Probe control or baseline unavailable' }
}

function Assert-DeploymentIsolation {
    param($Gate,[switch]$Probe)
    $null=Get-DeploymentInventory
    $policies=Invoke-DeploymentKubectl @('get','networkpolicies','-o','json') | ConvertFrom-Json
    if (@($policies.items).Count -ne 1 -or $policies.items[0].metadata.name -ne 'research-bootstrap-isolation') { throw 'Unexpected additive network policies' }
    $spec=$policies.items[0].spec
    # Strict-mode-safe: NetworkPolicySpec.ingress is 'omitempty' on the server's
    # response -- the deny-all policy this script itself creates has ZERO
    # ingress rules (an empty list), and the API server omits an empty-slice
    # field entirely when serializing 'kubectl get -o json', even though the
    # client-submitted manifest explicitly had 'ingress: []'. A direct
    # '.spec.ingress' access throws under Set-StrictMode on exactly the
    # policy this check exists to approve. Missing is the correct reading of
    # zero rules and must still pass; any ACTUAL rule present (non-empty,
    # non-null) remains drift exactly as before -- this only changes how an
    # omitted field is read, never what counts as safe.
    $ingressRaw = Get-DeploymentOptionalProperty $spec 'ingress'
    if (@($ingressRaw | Where-Object {$null -ne $_}).Count -ne 0 -or @($spec.policyTypes).Count -ne 1 -or $spec.policyTypes[0] -ne 'Ingress' -or ($spec.podSelector | ConvertTo-Json -Compress -Depth 8) -ne ($Gate.Selector | ConvertTo-Json -Compress -Depth 8)) { throw 'Isolation policy drift' }
    if ($Probe) {
        $script:ActiveIsolationGate=$Gate
        foreach($pod in $Gate.Callers) {
            Invoke-IsolationProbe $pod 'research-service' 80
            Invoke-IsolationProbe $pod $Gate.ServiceIP 80 -Denied
            Invoke-IsolationProbe $pod $Gate.PodIP 8000 -Denied
        }
    }
}

function Initialize-DeploymentSafety {
    param([string[]]$Names,[switch]$ExistingIsolation,$Recovery)
    $pods=Get-DeploymentInventory
    # Strict-mode-safe: ObjectMeta.Labels is 'omitempty' -- any pod in this
    # namespace that happens to carry no 'app.kubernetes.io/component' label
    # (e.g. a one-off job pod) throws under Set-StrictMode on direct access,
    # exactly like the fix already used for this same lookup elsewhere
    # (Get-DeploymentRiskLevel). A pod lacking the label is correctly excluded
    # by this filter either way -- missing just stops it from crashing.
    $research=@($pods.items | Where-Object { (Get-DeploymentOptionalProperty (Get-DeploymentOptionalProperty $_.metadata 'labels') 'app.kubernetes.io/component') -eq 'research-engine' })
    if ($research.Count -ne 1 -or $research[0].status.phase -ne 'Running') { throw 'Ambiguous research pod' }
    $r=$research[0]
    if (@($r.status.containerStatuses | Where-Object {-not $_.ready}).Count) { throw 'Research not ready' }
    $gate=@{ResearchPod=$r.metadata.name;ResearchUID=$r.metadata.uid;PodIP=$r.status.podIP;Names=$Names;Migrations=$null}
    $gate.PodIdentity=($r.status.containerStatuses | Select-Object name,restartCount,containerID | ConvertTo-Json -Compress)
    $deployment=Invoke-DeploymentKubectl @('get','deployment','research-engine','-o','json') | ConvertFrom-Json
    $envs=($deployment.spec.template.spec.containers | Where-Object name -eq 'research-engine').env
    if (($envs | Where-Object name -eq 'AIP_RESEARCH_OPPORTUNITY_SCHEDULER_ENABLED').value -ne 'false') { throw 'Old scheduler state unsafe/unknown' }
    if ('research-engine' -notin $Names -and ($envs | Where-Object name -eq 'AIP_RESEARCH_OPPORTUNITY_RECOVERY_PAUSED').value -ne 'true') { throw 'Research replacement or existing recovery pause required' }
    if ($Recovery -and ($envs | Where-Object name -eq 'AIP_RESEARCH_OPPORTUNITY_RECOVERY_PAUSED').value -ne 'true') { throw 'Interrupted-cycle recovery requires the running worker to be recovery-paused' }
    $gate.Selector=$deployment.spec.selector
    $labels=$gate.Selector.matchLabels
    if ($labels.'app.kubernetes.io/component' -ne 'research-engine' -or $labels.'app.kubernetes.io/instance' -ne 'ai-investment-platform' -or $labels.'app.kubernetes.io/name' -ne 'ai-investment-platform' -or @($labels.PSObject.Properties).Count -ne 3) { throw 'Unexpected research selector' }
    $service=Invoke-DeploymentKubectl @('get','service','research-engine','-o','json') | ConvertFrom-Json
    $gate.ServiceIP=$service.spec.clusterIP
    $gate.Callers=@()
    foreach($name in @('api-gateway','portfolio-service','mcp-gateway')) {
        $caller=@($pods.items | Where-Object { (Get-DeploymentOptionalProperty (Get-DeploymentOptionalProperty $_.metadata 'labels') 'app.kubernetes.io/component') -eq $name })
        if ($caller.Count -ne 1) { throw 'Ambiguous internal caller' }
        $gate.Callers+= $caller[0].metadata.name
    }
    $policies=Invoke-DeploymentKubectl @('get','networkpolicies','-o','json') | ConvertFrom-Json
    if ($ExistingIsolation) {
        # Explicit interrupted-run path: never remove or recreate the old isolation policy.
        Assert-DeploymentIsolation $gate -Probe
    } else {
        if (@($policies.items).Count) { throw 'Existing network policies require separate review' }
        foreach($pod in $gate.Callers) {
            Invoke-IsolationProbe $pod 'research-service' 80
            Invoke-IsolationProbe $pod $gate.ServiceIP 80
            Invoke-IsolationProbe $pod $gate.PodIP 8000
        }
    }
    if ($Names -contains 'portfolio-service') {
        # Flyway's SQL checksum is CRC32 over UTF-8 lines, excluding BOM/newlines.
        $code="import pathlib,zlib,json; print(json.dumps({p.name:((zlib.crc32(''.join(p.read_text(encoding='utf-8-sig').splitlines()).encode())+2**31)%2**32)-2**31 for p in pathlib.Path('services/portfolio-service/src/main/resources/db/migration').glob('V*__*.sql')}))"
        $gate.Migrations=(Invoke-DeploymentCommand python @('-c',$code) | Out-String) | ConvertFrom-Json
        if (-not $gate.Migrations -or @($gate.Migrations.PSObject.Properties).Count -eq 0) { throw 'Missing migration manifest' }
    }
    if ($Names -contains 'frontend') {
        $code='const fs=require("fs"),p=require("path"),v=new Set();function scan(d){for(const f of fs.readdirSync(d,{withFileTypes:true})){const n=p.join(d,f.name);if(f.isDirectory())scan(n);else if(n.endsWith(".js")){for(const m of fs.readFileSync(n,"utf8").matchAll(/apiBaseUrl:"([^"]*)",authDevLoginEnabled:(![01]|true|false)/g))v.add(JSON.stringify({apiBaseUrl:m[1],authDevLoginEnabled:["!0","true"].includes(m[2])?"true":"false"}));}}}scan("/app/.next");if(v.size!==1)process.exit(2);console.log([...v][0]);'
        $encoded=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($code))
        $gate.Frontend=(Invoke-DeploymentKubectl @('exec','deployment/frontend','--','node','-e',"eval(Buffer.from('$encoded','base64').toString())") | Out-String) | ConvertFrom-Json
        if ($gate.Frontend.authDevLoginEnabled -notin @('true','false') -or $null -eq $gate.Frontend.apiBaseUrl) { throw 'Frontend build configuration unknown' }
    }
    Set-DeploymentPhase 'INITIAL_POSTGRES_OBSERVATION'
    if ($Recovery) { $gate.Recovery=$Recovery }
    $gate.Baseline=Invoke-DeploymentObservation $gate
    if ($Recovery) { Assert-RecoveryWorkerExited $gate }
    $gate.Workloads=Invoke-DeploymentKubectl @('get','deployments,statefulsets','-o','json') | ConvertFrom-Json
    return $gate
}

function Save-DeploymentBaseline {
    param($Tag,$Deployments,$Gate)
    $path=Join-Path $RuntimeDir $Tag
    New-Item -ItemType Directory -Path $path -ErrorAction Stop | Out-Null
    $identity=[Security.Principal.WindowsIdentity]::GetCurrent().Name
    Invoke-DeploymentCommand icacls @($path,'/inheritance:r','/grant:r',"${identity}:(OI)(CI)F") | Out-Null
    $Gate.AuditPath=$path
    $Deployments | ConvertTo-Json -Depth 100 | Set-Content -LiteralPath (Join-Path $path 'original-deployments.json') -Encoding UTF8
    $Gate.Workloads | ConvertTo-Json -Depth 100 | Set-Content -LiteralPath (Join-Path $path 'original-workloads.json') -Encoding UTF8
}

function Start-DeploymentIsolation {
    param($Gate)
    $policy=@{apiVersion='networking.k8s.io/v1';kind='NetworkPolicy';metadata=@{name='research-bootstrap-isolation';namespace='ai-investment'};spec=@{podSelector=$Gate.Selector;policyTypes=@('Ingress');ingress=@()}}
    $path=Join-Path $Gate.AuditPath 'isolation.json'
    $policy | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $path -Encoding UTF8
    Invoke-DeploymentKubectl @('create','--dry-run=server','-f',$path) | Out-Null
    Invoke-DeploymentKubectl @('create','-f',$path) | Out-Null
    # On any subsequent failure leave isolation in place; no automatic rollback.
    $Gate.Isolated=$true
    $Gate.Started=[DateTime]::UtcNow.ToString('o')
    Assert-DeploymentIsolation $Gate -Probe
}

function Assert-DeploymentQuietSample {
    param($Gate,[switch]$Probe)
    # Recheck policy/inventory each sample. Bounded network enforcement probes
    # bracket the quiet window, rather than consume most of every sample period.
    Assert-DeploymentIsolation $Gate -Probe:$Probe
    Assert-DeploymentWorkloads $Gate @()
    $pod=Invoke-DeploymentKubectl @('get','pod',$Gate.ResearchPod,'-o','json') | ConvertFrom-Json
    $identity=$pod.status.containerStatuses | Select-Object name,restartCount,containerID | ConvertTo-Json -Compress
    if ($pod.metadata.uid -ne $Gate.ResearchUID -or $identity -ne $Gate.PodIdentity) { throw 'Research pod changed during observation' }
    if (@($pod.status.containerStatuses | Where-Object {-not $_.ready}).Count) { throw 'Research became unready during observation' }
    $logs=Invoke-DeploymentKubectl @('logs',$Gate.ResearchPod,'--timestamps',"--since-time=$($Gate.Started)")
    foreach($line in $logs) {
        if (($line -match '"(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS) ' -and $line -notmatch '"GET /health(/live)? HTTP/1\.[01]" 200') -or $line -match 'ERROR|Traceback|takeover|acquisition|cycle.*(start|resum)') { throw 'Unexpected research traffic/activity' }
    }
    $sample=Invoke-DeploymentObservation $Gate
    if ($sample.fingerprint -ne $Gate.Baseline.fingerprint) { throw 'Durable state changed during observation' }
    if ($Gate.Recovery) { Assert-RecoveryWorkerExited $Gate }
    $sample | ConvertTo-Json -Depth 10 -Compress | Add-Content -LiteralPath (Join-Path $Gate.AuditPath 'observations.jsonl') -Encoding UTF8
}

function Wait-DeploymentQuiet {
    param($Gate)
    $started=Get-DeploymentMonotonicSeconds
    $next=$started
    do {
        Assert-DeploymentQuietSample $Gate
        $now=Get-DeploymentMonotonicSeconds
        $elapsed=$now-$started
        Write-Host ('Quiet observation: {0:N0}/{1:N0} seconds elapsed.' -f $elapsed, $QuietPeriodSeconds)
        if ($elapsed -gt ($QuietPeriodSeconds + 60)) { throw 'Quiet observation exceeded bounded window' }
        if ($now-$next -gt 30) { throw 'Observation cannot sustain 30-second sampling' }
        if ($elapsed -ge $QuietPeriodSeconds) { break }
        $next+=30
        $sleep=[Math]::Ceiling($next-$now)
        if ($sleep -gt 0) { Start-Sleep -Seconds $sleep }
    } while ($true)
}

function Complete-DeploymentSafety {
    param($Gate,[string[]]$Names,$Images)
    $pods=Invoke-DeploymentKubectl @('get','pods','-o','json') | ConvertFrom-Json
    foreach($name in $Names) {
        $items=@($pods.items | Where-Object {(Get-DeploymentOptionalProperty (Get-DeploymentOptionalProperty $_.metadata 'labels') 'app.kubernetes.io/component') -eq $name})
        if ($items.Count -ne 1 -or (Get-DeploymentOptionalProperty $items[0].metadata 'deletionTimestamp')) { throw 'Old/new pod overlap remains' }
        $status=@($items[0].status.containerStatuses | Where-Object name -eq $name)
        if ($status.Count -ne 1 -or -not $status[0].ready) { throw 'Pod image/health verification failed' }
        Assert-VerifiedPodImage $Images[$name] $status[0].imageID
        if ($name -eq 'research-engine') {
            $init=@($items[0].status.initContainerStatuses | Where-Object name -eq 'wait-for-research-migrations')
            if ($init.Count -ne 1 -or $null -eq $init[0].state.terminated -or $init[0].state.terminated.exitCode -ne 0) { throw 'Migration-wait init did not complete' }
            Assert-VerifiedPodImage $Images[$name] $init[0].imageID
        }
    }
    Assert-DeploymentWorkloads $Gate $Names
    # DB state must also remain unchanged through rollout. Repoint only to new research pod.
    $Gate.ResearchPod=($pods.items | Where-Object {(Get-DeploymentOptionalProperty (Get-DeploymentOptionalProperty $_.metadata 'labels') 'app.kubernetes.io/component') -eq 'research-engine'}).metadata.name
    $sample=Invoke-DeploymentObservation $Gate
    if ($sample.fingerprint -ne $Gate.Baseline.fingerprint) { throw 'Durable state changed during rollout' }
    if ($Gate.Recovery) { Assert-RecoveryNotResumed $Gate }
    $release=Invoke-DeploymentCommand helm @('--kube-context','k3d-ai-investment-dev','-n','ai-investment','status','ai-investment-platform','-o','json') | ConvertFrom-Json
    if ($release.version -ne $Gate.HelmRevision) { throw 'Helm revision changed' }
    Invoke-DeploymentKubectl @('delete','networkpolicy','research-bootstrap-isolation','--wait=true') | Out-Null
    Write-Host 'Isolation removed after verification. Keep caller freeze; recovery stays paused. No ETF operations authorized.'
}

function Assert-VerifiedPodImage {
    param([string]$Expected,[string]$Actual)
    if ($Actual -notmatch '(sha256:[a-f0-9]{64})$' -or -not $script:VerifiedDeploymentDigests -or $Matches[1] -notin $script:VerifiedDeploymentDigests[$Expected]) { throw 'Pod digest not in verified OCI image graph' }
}

function Get-DeploymentMonotonicSeconds { return [Diagnostics.Stopwatch]::GetTimestamp()/[double][Diagnostics.Stopwatch]::Frequency }

function Assert-DeploymentWorkloads {
    param($Gate,[string[]]$Except)
    $actual=Invoke-DeploymentKubectl @('get','deployments,statefulsets','-o','json') | ConvertFrom-Json
    $before=@($Gate.Workloads.items | Where-Object {$_.metadata.name -notin $Except})
    $after=@($actual.items | Where-Object {$_.metadata.name -notin $Except})
    if ($before.Count -ne $after.Count) { throw 'Unrelated workload set changed' }
    foreach($item in $before) {
        $other=@($after | Where-Object {$_.kind -eq $item.kind -and $_.metadata.name -eq $item.metadata.name})
        if ($other.Count -ne 1 -or ($item.spec | ConvertTo-Json -Depth 100 -Compress) -ne ($other[0].spec | ConvertTo-Json -Depth 100 -Compress)) { throw 'Unrelated workload changed' }
    }
}

# -----------------------------------------------------------------------------
# Fresh DEV deployment path (code-only changes).
#
# Deploy-VerifiedRadarComponentsCore above is the HIGH-RISK path: mandatory
# caller freeze, 600s quiet-period, ingress isolation, and a manually
# pre-approved rollback baseline. It remains unchanged and is still required
# for schema migrations, infrastructure/Helm changes, and worker-state
# transitions.
#
# The functions below add a separate, additive LOW-RISK path for ordinary
# code-only changes to the same four protected components: build, push an
# immutable uniquely-tagged image, verify its registry digest, patch the
# Deployment to that digest-pinned image, let Kubernetes roll the pod over
# normally, verify the result, and record an automatic rollback reference.
# No isolation policy is created, no quiet period is observed, and no manual
# rollback-baseline reconciliation/approval is required. Classification below
# fails closed to HIGH_RISK (and therefore to the existing verified path)
# whenever a signal cannot be positively verified.
# -----------------------------------------------------------------------------

function Get-DeploymentMigrationFingerprint {
    param([ValidateSet('api-gateway','research-engine','portfolio-service','frontend')][string]$Name)
    $paths = @{
        'portfolio-service' = 'services/portfolio-service/src/main/resources/db/migration'
        'research-engine'   = 'ai/research-engine/db/migration'
    }
    if (-not $paths.ContainsKey($Name)) { return [pscustomobject]@{} }
    # Flyway's SQL checksum is CRC32 over UTF-8 lines, excluding BOM/newlines --
    # identical computation already used for portfolio-service in Initialize-DeploymentSafety.
    $code = "import pathlib,zlib,json; print(json.dumps({p.name:((zlib.crc32(''.join(p.read_text(encoding='utf-8-sig').splitlines()).encode())+2**31)%2**32)-2**31 for p in pathlib.Path('$($paths[$Name])').glob('V*__*.sql')}))"
    $result = (Invoke-DeploymentCommand python @('-c', $code) | Out-String) | ConvertFrom-Json
    if ($null -eq $result) { throw "Unable to compute migration fingerprint for $Name" }
    return $result
}

function Get-DeploymentHelmFingerprint {
    $files = @(Get-ChildItem -LiteralPath $HelmChart -Recurse -File) + @(Get-Item -LiteralPath $HelmValues)
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $entries = @($files | Sort-Object FullName | ForEach-Object {
            $relative = $_.FullName.Substring($ProjectRoot.Length).Replace('\', '/')
            $bytes = [IO.File]::ReadAllBytes($_.FullName)
            "$relative=" + ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '')
        })
    }
    finally { $sha.Dispose() }
    $combined = [Text.Encoding]::UTF8.GetBytes(($entries -join "`n"))
    $sha2 = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha2.ComputeHash($combined))).Replace('-', '').ToLowerInvariant() }
    finally { $sha2.Dispose() }
}

function Get-DeploymentFreshBaselinePath {
    param([string]$Name)
    Join-Path (Join-Path $RuntimeDir 'fresh-deploy-state') "$Name.json"
}

function Get-DeploymentFreshBaseline {
    param([string]$Name)
    $path = Get-DeploymentFreshBaselinePath $Name
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return $null }
    try { return (Get-Content -LiteralPath $path -Raw | ConvertFrom-Json) } catch { return $null }
}

function Save-DeploymentFreshBaseline {
    param([string]$Name, [string]$Image, $MigrationFingerprint, [string]$HelmFingerprint)
    $dir = Join-Path $RuntimeDir 'fresh-deploy-state'
    New-Item -ItemType Directory -Path $dir -Force -ErrorAction Stop | Out-Null
    $record = @{
        image                = $Image
        migrationFingerprint = $MigrationFingerprint
        helmFingerprint      = $HelmFingerprint
        recordedAtUtc        = [DateTime]::UtcNow.ToString('o')
    }
    $record | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Get-DeploymentFreshBaselinePath $Name) -Encoding ASCII
}

# Approves an intentional, already-APPLIED Helm configuration change for a
# single component, separately from any image build/deploy. Deliberately
# split into three non-conflatable steps:
#   1) Approval evidence  -- explicit switch + mandatory human-readable reason.
#   2) Verification only  -- re-reads the LIVE deployment and refuses unless
#      the running config already matches the new local values-dev.yaml.
#      This function NEVER patches the cluster itself.
#   3) Recording only      -- re-saves the existing fresh-deploy baseline for
#      this component with ONLY helmFingerprint advanced; image and
#      migrationFingerprint are carried over unchanged from the existing
#      record, so this call cannot be used to also approve an image change.
function Approve-DeploymentHelmConfigurationChange {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Reason,
        [switch]$AuthorizeHelmConfigurationReconciliation
    )
    if (-not $AuthorizeHelmConfigurationReconciliation -or [string]::IsNullOrWhiteSpace($Reason)) {
        throw "Specify -AuthorizeHelmConfigurationReconciliation and -Reason '<why this Helm change is approved>'."
    }
    $existing = Get-DeploymentFreshBaseline -Name $Name
    if (-not $existing) {
        throw "No existing fresh-deploy baseline for '$Name' -- this function reconciles an existing record, it does not create one."
    }
    $deployment = Invoke-DeploymentKubectl @('get', 'deployment', $Name, '-o', 'json') | ConvertFrom-Json
    $container = $deployment.spec.template.spec.containers | Where-Object { $_.name -eq $Name }
    $liveFlag = ($container.env | Where-Object { $_.name -eq 'AIP_RESEARCH_ETF_FULL_UNIVERSE_EXECUTION_ENABLED' }).value
    $localFlag = (Get-LocalFullEtfExecutionFlag).ToString().ToLowerInvariant()
    if ($liveFlag -ne $localFlag) {
        throw "Running configuration ($liveFlag) does not yet match local values-dev.yaml ($localFlag) -- apply the approved correction first. This function only records an already-applied, already-verified state; it never writes to the cluster."
    }
    $newHelmFingerprint = Get-DeploymentHelmFingerprint
    Save-DeploymentFreshBaseline -Name $Name -Image $existing.image -MigrationFingerprint $existing.migrationFingerprint -HelmFingerprint $newHelmFingerprint
    Write-Host "Helm configuration baseline for '$Name' advanced to fingerprint $newHelmFingerprint. Reason: $Reason"
}

# StrictMode-safe optional property lookup for dynamic Kubernetes JSON
# objects (ConvertFrom-Json PSCustomObjects). An absent key here is a
# legitimate, expected shape -- a pod with no app.kubernetes.io/* labels
# (e.g. postgres-0), or a container/init-container state that is
# "running"/"waiting" rather than "terminated" -- not a schema violation,
# so this must not throw the way a direct dotted property access does
# under Set-StrictMode. Returns $null for a missing property or a $null
# object; never masks any validation performed on the returned value.
function Get-DeploymentOptionalProperty {
    param($Object, [string]$Name)
    if ($null -eq $Object) { return $null }
    # IDictionary (e.g. a Hashtable built with @{}) does not expose its
    # dynamic keys through .PSObject.Properties, so it needs its own path;
    # ConvertFrom-Json always produces PSCustomObject, never a Hashtable.
    if ($Object -is [System.Collections.IDictionary]) {
        if ($Object.Contains($Name)) { return $Object[$Name] }
        return $null
    }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) { return $null }
    return $property.Value
}

# Testable, fail-closed risk classifier. Returns LOW_RISK only when every
# signal below was positively verified and showed no schema, infrastructure,
# or worker-state change; any unverifiable signal, query failure, or detected
# change returns HIGH_RISK with the specific reasons.
function Get-DeploymentRiskLevel {
    param([string[]]$Names)
    $reasons = @()
    try {
        $helmFingerprint = Get-DeploymentHelmFingerprint
        if ($Names -contains 'research-engine') {
            $deployment = Invoke-DeploymentKubectl @('get', 'deployment', 'research-engine', '-o', 'json') | ConvertFrom-Json
            $envs = ($deployment.spec.template.spec.containers | Where-Object name -eq 'research-engine').env
            $pausedOk = ($envs | Where-Object name -eq 'AIP_RESEARCH_OPPORTUNITY_RECOVERY_PAUSED').value -eq 'true'
            $schedulerOk = ($envs | Where-Object name -eq 'AIP_RESEARCH_OPPORTUNITY_SCHEDULER_ENABLED').value -eq 'false'
            if (-not $pausedOk -or -not $schedulerOk) { $reasons += 'research-engine worker state is not in the known-safe paused/disabled configuration' }
            $pods = @((Invoke-DeploymentKubectl @('get', 'pods', '-o', 'json') | ConvertFrom-Json).items | Where-Object {
                (Get-DeploymentOptionalProperty (Get-DeploymentOptionalProperty $_.metadata 'labels') 'app.kubernetes.io/component') -eq 'research-engine'
            })
            foreach ($pod in $pods) {
                $init = @($pod.status.initContainerStatuses | Where-Object name -eq 'wait-for-research-migrations')
                if ($init.Count -ne 1 -or $null -eq (Get-DeploymentOptionalProperty $init[0].state 'terminated') -or (Get-DeploymentOptionalProperty $init[0].state 'terminated').exitCode -ne 0) {
                    $reasons += 'research-engine migration/startup init container has not cleanly completed'
                }
            }
        }
        foreach ($name in @($Names | Where-Object { $_ -in @('research-engine', 'portfolio-service') })) {
            $currentFingerprint = Get-DeploymentMigrationFingerprint -Name $name
            $baseline = Get-DeploymentFreshBaseline -Name $name
            if ($null -eq $baseline) {
                # No prior fresh-deploy record for this component: this is a
                # first-time bootstrap. Live worker-state being safe is NOT
                # sufficient on its own to accept the migration/Helm
                # configuration as trustworthy -- that would classify LOW_RISK
                # merely because nothing has been recorded yet to disagree
                # with. Instead, require independently verified proof that
                # the CURRENTLY RUNNING image for this component is exactly
                # the image recorded in the separately-approved, versioned
                # rollback-baseline manifest (the same trust anchor the
                # verified/recovery path relies on, re-resolved here against
                # the registry rather than trusted as a string). Only when
                # that holds do we treat the live fingerprint reading as a
                # legitimate first baseline. Any failure to establish it --
                # missing/invalid approved baseline, registry verification
                # failure, or a running image that does not match -- fails
                # closed to HIGH_RISK rather than silently bootstrapping trust.
                try {
                    $approvedBaseline = Get-ApprovedRollbackBaseline -ProjectRoot $ProjectRoot
                    $approvedImage = Get-VerifiedRegistryImage $name $approvedBaseline.Components[$name]
                    $bootstrapDeployment = Invoke-DeploymentKubectl @('get', 'deployment', $name, '-o', 'json') | ConvertFrom-Json
                    $bootstrapContainer = $bootstrapDeployment.spec.template.spec.containers | Where-Object { $_.name -eq $name }
                    $bootstrapAllowedRepositories = @("ai-investment-registry:5000/ai-investment/$name", "localhost:5001/ai-investment/$name")
                    $bootstrapReference = $null
                    foreach ($repository in $bootstrapAllowedRepositories) {
                        $imagePattern = '^' + [regex]::Escape($repository) + '[:@](.+)$'
                        if ($bootstrapContainer.image -match $imagePattern) { $bootstrapReference = $Matches[1]; break }
                    }
                    if (-not $bootstrapReference) { throw "Unexpected running image repository for $name" }
                    $runningImage = Get-VerifiedRegistryImage $name $bootstrapReference
                    if ($runningImage -ne $approvedImage) {
                        $reasons += "first-time fresh-deploy bootstrap for ${name}: running image is not the independently-approved rollback baseline image; no trusted comparison available for migration/Helm configuration"
                    }
                }
                catch {
                    $reasons += "first-time fresh-deploy bootstrap for ${name}: could not independently verify the running image against the approved rollback baseline ($($_.Exception.Message))"
                }
                continue
            }
            if (($currentFingerprint | ConvertTo-Json -Compress) -ne ($baseline.migrationFingerprint | ConvertTo-Json -Compress)) {
                $reasons += "schema migration change detected for $name since the last recorded fresh deployment"
            }
            if ($helmFingerprint -ne $baseline.helmFingerprint) {
                $reasons += 'infrastructure (Helm chart/values) change detected since the last recorded fresh deployment'
            }
        }
    }
    catch {
        return [pscustomobject]@{ Level = 'HIGH_RISK'; Reasons = @("risk classification could not be completed: $($_.Exception.Message)") }
    }
    if ($reasons.Count -gt 0) { return [pscustomobject]@{ Level = 'HIGH_RISK'; Reasons = @($reasons | Select-Object -Unique) } }
    return [pscustomobject]@{ Level = 'LOW_RISK'; Reasons = @() }
}

# Handles a stale deployment-owned 'research-bootstrap-isolation' NetworkPolicy
# left over from a prior interrupted high-risk attempt. Verifies the exact
# namespace, ownership/selector shape, and policy purpose before touching it,
# confirms no deployment or migration is actively running, and only then
# restores normal research ingress -- as part of this explicitly authorized
# fresh deployment. Never deletes a policy that does not match exactly, and
# is a no-op when no such policy exists.
function Resolve-DeploymentStaleIsolation {
    param([string[]]$Names)
    $policies = Invoke-DeploymentKubectl @('get', 'networkpolicies', '-o', 'json') | ConvertFrom-Json
    $matches = @($policies.items | Where-Object { $_.metadata.name -eq 'research-bootstrap-isolation' })
    if ($matches.Count -eq 0) { return }
    if ($matches.Count -gt 1 -or @($policies.items).Count -ne 1) { throw 'Unexpected additional network policies alongside research-bootstrap-isolation; refusing to modify any of them automatically' }
    $policy = $matches[0]
    if ($policy.metadata.namespace -ne 'ai-investment') { throw 'research-bootstrap-isolation found outside the expected namespace; refusing to modify it automatically' }
    $deployment = Invoke-DeploymentKubectl @('get', 'deployment', 'research-engine', '-o', 'json') | ConvertFrom-Json
    $expectedSelector = $deployment.spec.selector
    $labels = $expectedSelector.matchLabels
    if ($labels.'app.kubernetes.io/component' -ne 'research-engine' -or $labels.'app.kubernetes.io/instance' -ne 'ai-investment-platform' -or $labels.'app.kubernetes.io/name' -ne 'ai-investment-platform' -or @($labels.PSObject.Properties).Count -ne 3) {
        throw 'Unexpected research-engine selector; refusing to touch research-bootstrap-isolation automatically'
    }
    # Strict-mode-safe: same omitted-empty-ingress risk as Assert-DeploymentIsolation
    # above -- this reads a live NetworkPolicy via kubectl, so the server can omit
    # an empty 'ingress' list here too. Missing still means zero rules; a real,
    # non-null rule still refuses this policy exactly as before.
    $policyIngressRaw = Get-DeploymentOptionalProperty $policy.spec 'ingress'
    if (($policy.spec.podSelector | ConvertTo-Json -Depth 20 -Compress) -ne ($expectedSelector | ConvertTo-Json -Depth 20 -Compress) -or
        @($policy.spec.policyTypes).Count -ne 1 -or $policy.spec.policyTypes[0] -ne 'Ingress' -or
        @($policyIngressRaw | Where-Object { $null -ne $_ }).Count -ne 0) {
        throw 'research-bootstrap-isolation does not match the expected deployment-owned shape; refusing to modify it automatically'
    }
    # Confirmed: exactly this one policy, right namespace, exact
    # deployment-owned selector/ingress shape. Before removing it, confirm no
    # deployment or migration is actively running.
    $jobs = Invoke-DeploymentKubectl @('get', 'jobs,cronjobs', '-o', 'json') | ConvertFrom-Json
    if (@($jobs.items | Where-Object { ($_.kind -eq 'CronJob' -and -not $_.spec.suspend) -or ($_.kind -eq 'Job' -and $_.status.active -gt 0) }).Count) {
        throw 'Scheduled/internal work is active; refusing to restore research ingress now'
    }
    $researchPods = @((Invoke-DeploymentKubectl @('get', 'pods', '-o', 'json') | ConvertFrom-Json).items | Where-Object { $_.metadata.labels.'app.kubernetes.io/component' -eq 'research-engine' })
    foreach ($pod in $researchPods) {
        $init = @($pod.status.initContainerStatuses | Where-Object name -eq 'wait-for-research-migrations')
        if ($init.Count -eq 1 -and ($null -eq $init[0].state.terminated -or $init[0].state.terminated.exitCode -ne 0)) {
            throw 'A research-engine migration/startup init container has not cleanly completed; refusing to restore research ingress now'
        }
    }
    Write-Host 'Detected stale research-bootstrap-isolation left by a prior interrupted deployment attempt. Verified exact namespace, ownership, selector shape, and that no deployment/migration is active.'
    Invoke-DeploymentKubectl @('delete', 'networkpolicy', 'research-bootstrap-isolation', '--wait=true') | Out-Null
    Write-Host 'Stale research-bootstrap-isolation removed; normal research ingress restored as part of this authorized fresh deployment.'
}

# Reuses the verified path's existing inline frontend-build-config inspection
# (Initialize-DeploymentSafety), extracted so the fresh path can reuse it
# without duplicating behavior-affecting logic inline.
function Get-DeploymentFrontendBuildConfig {
    $code = 'const fs=require("fs"),p=require("path"),v=new Set();function scan(d){for(const f of fs.readdirSync(d,{withFileTypes:true})){const n=p.join(d,f.name);if(f.isDirectory())scan(n);else if(n.endsWith(".js")){for(const m of fs.readFileSync(n,"utf8").matchAll(/apiBaseUrl:"([^"]*)",authDevLoginEnabled:(![01]|true|false)/g))v.add(JSON.stringify({apiBaseUrl:m[1],authDevLoginEnabled:["!0","true"].includes(m[2])?"true":"false"}));}}}scan("/app/.next");if(v.size!==1)process.exit(2);console.log([...v][0]);'
    $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($code))
    $config = (Invoke-DeploymentKubectl @('exec', 'deployment/frontend', '--', 'node', '-e', "eval(Buffer.from('$encoded','base64').toString())") | Out-String) | ConvertFrom-Json
    if ($config.authDevLoginEnabled -notin @('true', 'false') -or $null -eq $config.apiBaseUrl) { throw 'Frontend build configuration unknown' }
    return $config
}

function Start-DeploymentFreshRollout {
    param([string[]]$Names)
    if (-not $Names -or @($Names | Where-Object { $_ -notin @('api-gateway', 'research-engine', 'portfolio-service', 'frontend') }).Count) { throw 'Invalid fresh deployment selection' }
    if ($SkipBuild) { throw 'SkipBuild is not supported for fresh deployment' }
    Assert-LocalPrerequisites
    $ready = Invoke-DeploymentKubectl @('get', '--raw=/readyz', '--request-timeout=10s')
    if (($ready | Out-String).Trim() -ne 'ok') { throw 'Cluster is not ready' }
    $release = Invoke-DeploymentCommand 'helm' @('--kube-context', 'k3d-ai-investment-dev', '-n', 'ai-investment', 'status', 'ai-investment-platform', '-o', 'json')
    if (-not ($release | ConvertFrom-Json).name) { throw 'Missing Helm release' }

    $deployments = @{}; $oldImages = @{}; $oldReferences = @{}
    foreach ($name in $Names) {
        $deployment = Invoke-DeploymentKubectl @('get', 'deployment', $name, '-o', 'json') | ConvertFrom-Json
        $containers = @($deployment.spec.template.spec.containers | Where-Object { $_.name -eq $name })
        foreach ($container in $containers) {
            $allowedRepositories = @("ai-investment-registry:5000/ai-investment/$name", "localhost:5001/ai-investment/$name")
            $reference = $null
            foreach ($repository in $allowedRepositories) {
                $imagePattern = '^' + [regex]::Escape($repository) + '[:@](.+)$'
                if ($container.image -match $imagePattern) { $reference = $Matches[1]; break }
            }
            if (-not $reference) { throw "Unexpected running image repository for $name" }
            $oldReferences[$name] = $reference
            $oldImages[$name] = Get-VerifiedRegistryImage $name $reference
        }
        $deployments[$name] = $deployment
    }

    $frontendConfig = $null
    if ('frontend' -in $Names) { $frontendConfig = Get-DeploymentFrontendBuildConfig }

    if ($DryRun) { Write-Host "Fresh DEV preflight only: $($Names -join ', '). No build, push, or patch performed."; return }

    $risk = Get-DeploymentRiskLevel -Names $Names
    if ($risk.Level -ne 'LOW_RISK') {
        throw "Fresh DEV deployment refused: $($risk.Reasons -join '; '). This change requires the verified deployment path (-AcceptBootstrapRisk -ConfirmCallerFreeze)."
    }

    # Stale-isolation restoration is deliberately NOT done here. Removing it
    # before the rollout has actually succeeded would expose research
    # ingress on the strength of the OLD deployment merely looking
    # quiescent, even if THIS rollout then fails partway through (build,
    # push, patch, or rollout-status failure) -- leaving a half-updated
    # workload with no isolation and nothing to diagnose. It is restored
    # further down, only after every component's image/init verification
    # and readiness wait has succeeded.
    Ensure-RuntimeDirectory
    $tag = 'fresh-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + ([guid]::NewGuid().ToString('N').Substring(0, 12))
    $rollbackPath = Join-Path $RuntimeDir "rollback-$tag.json"
    $oldImages | ConvertTo-Json | Set-Content -LiteralPath $rollbackPath -Encoding ASCII

    $java = @($Names | Where-Object { $_ -in @('api-gateway', 'portfolio-service') })
    if ($java.Count) {
        Assert-BuildPrerequisites
        Invoke-DeploymentCommand 'mvn' @('-f', (Join-Path $ProjectRoot 'services/pom.xml'), '-pl', ($java -join ','), '-am', 'package', '-DskipTests') | Out-Host
    }

    $images = @{}
    $contexts = @{'api-gateway' = 'services/api-gateway'; 'portfolio-service' = 'services/portfolio-service'; 'research-engine' = 'ai/research-engine'; 'frontend' = 'frontend' }
    foreach ($name in $Names) {
        $image = "localhost:5001/ai-investment/${name}:$tag"
        $context = Join-Path $ProjectRoot $contexts[$name]
        $buildArgs = @('build', '-t', $image)
        if ($name -eq 'frontend') { $buildArgs += @('--build-arg', "NEXT_PUBLIC_API_BASE_URL=$($frontendConfig.apiBaseUrl)", '--build-arg', "NEXT_PUBLIC_AUTH_DEV_LOGIN_ENABLED=$($frontendConfig.authDevLoginEnabled)") }
        if ($NoCache) { $buildArgs += '--no-cache' }
        Set-DeploymentPhase "FRESH_BUILD/$name"
        Invoke-DeploymentCommand 'docker' ($buildArgs + @($context)) | Out-Host
        Set-DeploymentPhase "FRESH_PUSH/$name"
        Invoke-DeploymentCommand 'docker' @('push', $image) | Out-Host
        $images[$name] = Get-VerifiedRegistryImage $name $tag
    }

    foreach ($name in $Names) {
        # Builds may be lengthy; recheck rollback availability immediately before rollout.
        if ((Get-VerifiedRegistryImage $name $oldReferences[$name]) -ne $oldImages[$name]) { throw 'Rollback reference verification changed' }
        $fresh = Invoke-DeploymentKubectl @('get', 'deployment', $name, '-o', 'json') | ConvertFrom-Json
        if ($fresh.metadata.resourceVersion -ne $deployments[$name].metadata.resourceVersion) { throw 'Deployment drift during build' }
        $patch = New-VerifiedDeploymentPatch $fresh $name $images[$name]
        $patchPath = Join-Path $RuntimeDir "$name-$tag.patch.json"
        $patch | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $patchPath -Encoding ASCII
        Set-DeploymentPhase "FRESH_ROLLOUT/$name"
        Invoke-DeploymentKubectl @('patch', 'deployment', $name, '--type=strategic', '--patch-file', $patchPath) | Out-Host
        Invoke-DeploymentKubectl @('rollout', 'status', "deployment/$name", '--timeout=420s') | Out-Host
        $actual = Invoke-DeploymentKubectl @('get', 'deployment', $name, '-o', 'json') | ConvertFrom-Json
        if ((($actual.spec.template.spec.containers | Where-Object { $_.name -eq $name }).image) -ne $images[$name]) { throw 'Deployment image verification failed' }
        if ($name -eq 'research-engine' -and (($actual.spec.template.spec.initContainers | Where-Object { $_.name -eq 'wait-for-research-migrations' }).image) -ne $images[$name]) { throw 'Init image verification failed' }
        Save-DeploymentFreshBaseline -Name $name -Image $images[$name] -MigrationFingerprint (Get-DeploymentMigrationFingerprint -Name $name) -HelmFingerprint (Get-DeploymentHelmFingerprint)
    }

    Wait-ForDeployments -Names $Names

    # Only now -- after every component's image and (for research-engine)
    # init-container identity has been verified against this rollout's own
    # freshly-built images, and readiness has been confirmed -- is it safe
    # to restore normal research ingress. Resolve-DeploymentStaleIsolation
    # independently re-verifies ownership, exact policy shape, and that no
    # migration/job is active before touching anything, and remains a no-op
    # (and never touches an unrelated policy) when no such policy exists.
    Resolve-DeploymentStaleIsolation -Names $Names

    $url = Resolve-ApplicationUrl
    Set-DeploymentPhase 'COMPLETE'
    Write-Host ""
    Write-Host "============================================================" -ForegroundColor Green
    Write-Host " Fresh DEV deployment completed" -ForegroundColor Green
    Write-Host "============================================================" -ForegroundColor Green
    Write-Host "Components      : $($Names -join ', ')"
    Write-Host "Image tag       : $tag"
    Write-Host "Application URL : $url"
    Write-Host "Rollback ref    : $rollbackPath"
    Write-Host ""
}

function Deploy-VerifiedRadarComponents {
    param([string[]]$Names)
    Initialize-DeploymentDiagnostics $Names
    try { Deploy-VerifiedRadarComponentsCore -Names $Names }
    catch { Show-DeploymentFailure $_; throw }
}

function Assert-ResumeAudit {
    # Generalized resume audit: accepts any well-formed, explicitly-
    # specified review tag and component set -- never a single
    # hardcoded tag or a fixed two-component pair. Every item below is
    # independently re-verified against saved, on-disk review evidence
    # and live cluster state; an operator-supplied tag or component list
    # is never trusted on its own to BE that evidence, and a saved
    # policy/evidence file is never treated as stale merely because of
    # its age -- only its exact recorded shape/content is checked.
    param([string]$Tag,[string[]]$Names,$Deployments,$Gate)
    if ($Tag -notmatch '^review-\d{8}-\d{6}-[0-9a-f]{32}$') { throw 'Resume tag is not a well-formed review tag' }
    if (@($Names).Count -eq 0) { throw 'Resume requires at least one selected component' }
    $dir=Join-Path $RuntimeDir $Tag
    foreach($file in @('original-deployments.json','original-workloads.json','isolation.json','observations.jsonl')) {
        if (-not (Test-Path -LiteralPath (Join-Path $dir $file) -PathType Leaf)) { throw "Missing recovery audit: $file" }
    }
    if (-not (Test-Path -LiteralPath (Join-Path $RuntimeDir "rollback-$Tag.json") -PathType Leaf)) { throw 'Missing rollback audit' }
    $record=Get-Content -LiteralPath (Join-Path $dir 'original-deployments.json') -Raw | ConvertFrom-Json
    # The resumed component selection must exactly match what the saved
    # review audit actually covers -- never a silently-accepted subset
    # or superset of the reviewed attempt.
    $recordedNames=@($record.PSObject.Properties.Name)
    $missingFromResume=@($recordedNames | Where-Object { $_ -notin $Names })
    $extraInResume=@($Names | Where-Object { $_ -notin $recordedNames })
    if ($missingFromResume.Count -or $extraInResume.Count) {
        throw "Resume component selection ($($Names -join ', ')) does not match the saved review audit's component set ($($recordedNames -join ', '))"
    }
    foreach($name in $Names) {
        $saved=$record.PSObject.Properties[$name].Value
        $current=$Deployments[$name]
        if (-not $saved -or $saved.metadata.uid -ne $current.metadata.uid -or
            $saved.metadata.resourceVersion -ne $current.metadata.resourceVersion -or
            ($saved.spec | ConvertTo-Json -Depth 100 -Compress) -cne ($current.spec | ConvertTo-Json -Depth 100 -Compress)) {
            throw "Deployment drift since failed attempt: $name"
        }
    }
    $auditPolicy=Get-Content -LiteralPath (Join-Path $dir 'isolation.json') -Raw | ConvertFrom-Json
    if ($auditPolicy.metadata.name -ne 'research-bootstrap-isolation' -or
        ($auditPolicy.spec.podSelector | ConvertTo-Json -Depth 20 -Compress) -cne ($Gate.Selector | ConvertTo-Json -Depth 20 -Compress) -or
        @($auditPolicy.spec.policyTypes).Count -ne 1 -or $auditPolicy.spec.policyTypes[0] -ne 'Ingress' -or
        @($auditPolicy.spec.ingress | Where-Object { $null -ne $_ }).Count -ne 0) { throw 'Saved isolation policy mismatch' }
    $Gate.Workloads=Get-Content -LiteralPath (Join-Path $dir 'original-workloads.json') -Raw | ConvertFrom-Json
    Assert-DeploymentWorkloads $Gate @()
    # No deployment or migration may be actively running before resuming
    # -- the same check already used before restoring research ingress
    # from a stale isolation policy (Resolve-DeploymentStaleIsolation).
    $jobs=Invoke-DeploymentKubectl @('get','jobs,cronjobs','-o','json') | ConvertFrom-Json
    if (@($jobs.items | Where-Object { ($_.kind -eq 'CronJob' -and -not $_.spec.suspend) -or ($_.kind -eq 'Job' -and $_.status.active -gt 0) }).Count) {
        throw 'Scheduled/internal work is active; refusing to resume now'
    }
    $snapshot=Get-Content -LiteralPath (Join-Path $RuntimeDir "rollback-$Tag.json") -Raw | ConvertFrom-Json
    foreach($name in $Names) {
        if ($snapshot.PSObject.Properties[$name].Value -ne $script:RecoveryOldImages[$name]) { throw "Rollback audit mismatch: $name" }
    }
    # Durable-state (PostgreSQL) consistency: the fresh observation
    # Initialize-DeploymentSafety already took for this resume
    # (Gate.Baseline) must match the LAST verified-quiet observation
    # actually recorded during the interrupted run's own quiet period --
    # never a freshly taken baseline substituted for history that was
    # never actually observed back then.
    $observationLines=@(Get-Content -LiteralPath (Join-Path $dir 'observations.jsonl') | Where-Object { $_.Trim().Length -gt 0 })
    if ($observationLines.Count -eq 0) { throw 'Saved review audit records no durable-state observation from the interrupted run; refusing to synthesize one' }
    $lastObservation=$observationLines[-1] | ConvertFrom-Json
    if ($lastObservation.fingerprint -notmatch '^[a-f0-9]{64}$') { throw 'Saved review audit has no valid durable-state fingerprint' }
    if ($Gate.Baseline.fingerprint -ne $lastObservation.fingerprint) {
        throw "Durable-state fingerprint changed since the interrupted attempt's own quiet period (then: $($lastObservation.fingerprint), now: $($Gate.Baseline.fingerprint))"
    }
    $Gate.AuditPath=$dir
    $Gate.Started=[DateTime]::UtcNow.ToString('o')
    Write-Host 'Verified failed-run audit, workload snapshots, rollback images, isolation policy, durable-state continuity, no active scheduled/internal work, and original deployment identity.'
}

function Deploy-VerifiedRadarComponentsCore {
    param([string[]]$Names)
    if (-not $Names -or @($Names | Where-Object { $_ -notin @('api-gateway','research-engine','portfolio-service','frontend') }).Count) { throw 'Invalid safe deployment selection' }
    if ($SkipBuild) { throw 'SkipBuild is not supported for verified deployment' }
    if ($ResumeAuditTag -and $NoCache) { throw 'NoCache is invalid for artifact-reuse recovery' }
    if ($ResumeAuditTag -and $ResumeAuditTag -notmatch '^review-\d{8}-\d{6}-[0-9a-f]{32}$') { throw 'Unreviewed recovery tag' }
    $cycleRecovery = Get-InterruptedCycleRecoveryRequest -Names $Names
    Assert-LocalPrerequisites
    # Every operation is pinned; never rely on mutable current-context state.
    $ready = Invoke-DeploymentKubectl @('get','--raw=/readyz','--request-timeout=10s')
    if (($ready | Out-String).Trim() -ne 'ok') { throw 'Cluster is not ready' }
    $release = Invoke-DeploymentCommand 'helm' @('--kube-context','k3d-ai-investment-dev','-n','ai-investment','status','ai-investment-platform','-o','json')
    if (-not ($release | ConvertFrom-Json).name -or ($release | ConvertFrom-Json).version -lt 1) { throw 'Missing Helm release/revision' }
    $approvedBaseline = Get-ApprovedRollbackBaseline -ProjectRoot $ProjectRoot
    $rollback = $approvedBaseline.Components
    $deployments = @{}
    $oldImages = @{}
    $oldInitImages = @{}
    foreach ($name in $Names) {
        $deployment = Invoke-DeploymentKubectl @('get','deployment',$name,'-o','json') | ConvertFrom-Json
        $containers = @($deployment.spec.template.spec.containers | Where-Object { $_.name -eq $name })
        $containerImages = @{}
        # Deployment-state model: the running image is trusted only when it
        # matches one of two INDEPENDENTLY VERIFIED sources -- never an
        # assumption that either equals the other, and never a mismatch
        # that auto-authorizes or auto-blocks on its own:
        #   1. the approved rollback baseline (explicitly reviewed recovery
        #      target -- Get-ApprovedRollbackBaseline).
        #   2. the most recent durable promotion-evidence candidate this
        #      same tool recorded after a fully-verified prior deployment
        #      (Save-RollbackBaselineApprovalCandidate), re-resolved against
        #      the registry right now by Get-LatestVerifiedPromotionEvidence
        #      -- never trusted from its recorded digest string alone.
        # A running image matching EITHER source is trusted; matching
        # neither fails closed before any mutation, naming both sources.
        $rollbackImage = Get-VerifiedRegistryImage $name $rollback[$name]
        $promotionEvidence = Get-LatestVerifiedPromotionEvidence -RuntimeDir $RuntimeDir -Name $name
        $initName = $null
        $initRollbackImage = $null
        $initPromotionImage = $null
        if ($name -eq 'research-engine') {
            # The running migration-wait image has its own approval. Never
            # infer approval from the main image, even when the digests agree.
            $initName = 'wait-for-research-migrations'
            $initDigest = if ($approvedBaseline.InitContainers) { $approvedBaseline.InitContainers[$initName] } else { $null }
            Assert-ValidRollbackDigest -Name "init container '$initName'" -Digest $initDigest
            $initRollbackImage = Get-VerifiedRegistryImage $name $initDigest
            if ($promotionEvidence -and $promotionEvidence.InitImage -and $promotionEvidence.InitImage.ContainerName -eq $initName) {
                $initPromotionImage = $promotionEvidence.InitImage.Image
            }
            $containers += @($deployment.spec.template.spec.initContainers | Where-Object { $_.name -eq $initName })
        }
        foreach ($container in $containers) {
            $allowedRepositories = @(
                "ai-investment-registry:5000/ai-investment/$name",
                "localhost:5001/ai-investment/$name"
            )
            $reference = $null
            foreach ($repository in $allowedRepositories) {
                $imagePattern = '^' + [regex]::Escape($repository) + '[:@](.+)$'
                if ($container.image -match $imagePattern) {
                    $reference = $Matches[1]
                    break
                }
            }
            if (-not $reference) { throw "Unexpected running image repository for $name" }
            $current = Get-VerifiedRegistryImage $name $reference
            $isInit = $container.name -ne $name
            $trustedRollback = if ($isInit) { $initRollbackImage } else { $rollbackImage }
            $trustedPromotion = if ($isInit) { $initPromotionImage } elseif ($promotionEvidence) { $promotionEvidence.Image } else { $null }
            if ($current -eq $trustedRollback) {
                $containerImages[$container.name] = $current
            } elseif ($trustedPromotion -and $current -eq $trustedPromotion) {
                $containerImages[$container.name] = $current
                Write-Host "Running image for $name/$($container.name) is trusted via durable promotion evidence (tag=$($promotionEvidence.Tag), recorded=$($promotionEvidence.RecordedAtUtc.ToString('o'))), not the approved rollback baseline. Baseline advancement remains a separate, explicitly-authorized action (approve-rollback-baseline)."
            } else {
                $evidenceNote = if ($promotionEvidence) { "the latest re-verified promotion evidence (tag=$($promotionEvidence.Tag)) also does not match" } else { 'no durable, re-verifiable promotion evidence was found for this component' }
                throw "Running image for $name/$($container.name) does not match the approved rollback baseline ($trustedRollback) and $evidenceNote (running: $current). Provenance is unestablished -- deployment stopped before any mutation. Review required: confirm the running image independently, then either 'approve-rollback-baseline' against a real promotion-evidence candidate, or 'reconcile-rollback-baseline' once the running image is otherwise confirmed trustworthy."
            }
        }
        $oldImages[$name] = $containerImages[$name]
        if ($initName) { $oldInitImages[$initName] = $containerImages[$initName] }
        $null = New-VerifiedDeploymentPatch $deployment $name $oldImages[$name]
        $deployments[$name] = $deployment
        $script:DeploymentRun.BaselineImages[$name]=($deployment.spec.template.spec.containers | Where-Object name -eq $name).image
    }
    if ($DryRun -and $ResumeAuditTag) { throw "Recovery cannot be certified by artifact-only DryRun" }
    if ($DryRun) { Write-Host "Verified artifact preflight: $($Names -join ', '). No build, push or patch. Database/isolation/quiet gates NOT executed; this is not deployment readiness."; return }
    if (-not $AcceptBootstrapRisk -or -not $ConfirmCallerFreeze) { throw 'Explicit bootstrap risk acceptance and caller freeze are required' }
    Set-DeploymentPhase 'READ_ONLY_PREFLIGHT'
    $gate = Initialize-DeploymentSafety -Names $Names -ExistingIsolation:([bool]$ResumeAuditTag) -Recovery $cycleRecovery
    $gate.HelmRevision=($release | ConvertFrom-Json).version
    $tag = if ($ResumeAuditTag) { $ResumeAuditTag } else { 'review-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N') }
    Ensure-RuntimeDirectory
    $rollbackPath = Join-Path $RuntimeDir "rollback-$tag.json"
    if ($ResumeAuditTag) {
        $script:RecoveryOldImages=$oldImages
        Assert-ResumeAudit -Tag $tag -Names $Names -Deployments $deployments -Gate $gate
    } else {
        $oldImages | ConvertTo-Json | Set-Content -LiteralPath $rollbackPath -Encoding ASCII
        Save-DeploymentBaseline -Tag $tag -Deployments $deployments -Gate $gate
    }
    $java = @($Names | Where-Object { $_ -in @('api-gateway','portfolio-service') })
    if ($java.Count) {
        Set-DeploymentPhase 'JAVA_PACKAGE'
        Assert-BuildPrerequisites
        Invoke-DeploymentCommand 'mvn' @('-f',(Join-Path $ProjectRoot 'services/pom.xml'),'-pl',($java -join ','),'-am','package','-DskipTests') | Out-Host
    }
    $images = @{}
    foreach ($name in $Names) {
        if ($ResumeAuditTag) {
            Set-DeploymentPhase "VERIFY_EXISTING_IMAGE/$name"
            $images[$name]=Get-VerifiedRegistryImage $name $tag
            # No rebuild, retag, or push: the registry digest is the immutable artifact identity.
            continue
        }
        $image = "localhost:5001/ai-investment/${name}:$tag"
        $contexts=@{'api-gateway'='services/api-gateway';'portfolio-service'='services/portfolio-service';'research-engine'='ai/research-engine';'frontend'='frontend'}
        $context=Join-Path $ProjectRoot $contexts[$name]
        $buildArgs = @('build','-t',$image)
        if ($name -eq 'frontend') { $buildArgs += @('--build-arg',"NEXT_PUBLIC_API_BASE_URL=$($gate.Frontend.apiBaseUrl)",'--build-arg',"NEXT_PUBLIC_AUTH_DEV_LOGIN_ENABLED=$($gate.Frontend.authDevLoginEnabled)") }
        if ($NoCache) { $buildArgs += '--no-cache' }
        Set-DeploymentPhase "BUILD/$name"
        Invoke-DeploymentCommand 'docker' ($buildArgs + @($context)) | Out-Host
        Set-DeploymentPhase "PUSH/$name"
        Invoke-DeploymentCommand 'docker' @('push',$image) | Out-Host
        $images[$name] = Get-VerifiedRegistryImage $name $tag
    }
    Set-DeploymentPhase 'ADMISSION_ISOLATION'
    if ($ResumeAuditTag) {
        Assert-DeploymentIsolation $gate -Probe
        # Initialize-DeploymentSafety takes a fresh DB observation; repeat the full quiet window.
    } else { Start-DeploymentIsolation -Gate $gate }
    Set-DeploymentPhase 'QUIET_PERIOD'
    Wait-DeploymentQuiet -Gate $gate
    # Verify every image and construct every patch before the first workload mutation.
    Set-DeploymentPhase 'FINAL_ARTIFACT_AND_PATCH_REVIEW'
    $patchPaths = @{}
    foreach ($name in $Names) {
        # Builds may be lengthy; recheck that the trusted image established
        # during ARTIFACT_PREFLIGHT (from either the approved rollback
        # baseline or re-verified promotion evidence -- never assumed to be
        # the rollback baseline specifically) is still intact and available
        # in the registry immediately before rollout.
        if ($oldImages[$name] -notmatch '@(sha256:[a-f0-9]{64})$' -or (Get-VerifiedRegistryImage $name $Matches[1]) -ne $oldImages[$name]) { throw 'Rollback verification changed' }
        if ($name -eq 'research-engine' -and
            ($oldInitImages['wait-for-research-migrations'] -notmatch '@(sha256:[a-f0-9]{64})$' -or (Get-VerifiedRegistryImage $name $Matches[1]) -ne $oldInitImages['wait-for-research-migrations'])) { throw 'Init rollback verification changed' }
        $fresh=Invoke-DeploymentKubectl @('get','deployment',$name,'-o','json') | ConvertFrom-Json
        if ($fresh.metadata.resourceVersion -ne $deployments[$name].metadata.resourceVersion) { throw 'Deployment drift during observation' }
        $patch = New-VerifiedDeploymentPatch $fresh $name $images[$name]
        $path = Join-Path $RuntimeDir "$name-$tag.patch.json"
        $patch | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $path -Encoding ASCII
        $patchPaths[$name] = $path
    }
    Assert-DeploymentQuietSample -Gate $gate -Probe
    foreach ($name in @('research-engine','api-gateway','portfolio-service','frontend') | Where-Object { $_ -in $Names }) {
        Set-DeploymentPhase "ROLLOUT/$name"
        Invoke-DeploymentKubectl @('patch','deployment',$name,'--type=strategic','--patch-file',$patchPaths[$name]) | Out-Host
        Invoke-DeploymentKubectl @('rollout','status',"deployment/$name",'--timeout=420s') | Out-Host
        $actual = Invoke-DeploymentKubectl @('get','deployment',$name,'-o','json') | ConvertFrom-Json
        $null = New-VerifiedDeploymentPatch $actual $name $images[$name]
        if (($actual.spec.template.spec.containers | Where-Object { $_.name -eq $name }).image -ne $images[$name]) { throw 'Deployment image verification failed' }
        if ($name -eq 'research-engine') {
            if (($actual.spec.template.spec.initContainers | Where-Object { $_.name -eq 'wait-for-research-migrations' }).image -ne $images[$name]) { throw 'Init image verification failed' }
            $envs = ($actual.spec.template.spec.containers | Where-Object { $_.name -eq $name }).env
            if (($envs | Where-Object name -eq 'AIP_RESEARCH_OPPORTUNITY_RECOVERY_PAUSED').value -ne 'true' -or ($envs | Where-Object name -eq 'AIP_RESEARCH_OPPORTUNITY_SCHEDULER_ENABLED').value -ne 'false') { throw 'Startup pause verification failed' }
            if (($envs | Where-Object name -eq 'AIP_RESEARCH_ETF_FULL_UNIVERSE_EXECUTION_ENABLED').value -ne (Get-LocalFullEtfExecutionFlag)) { throw 'Full ETF execution flag differs from LOCAL Helm values' }
        }
    }
    $script:DeploymentRun.Verified=@($Names)
    Set-DeploymentPhase 'POST_ROLLOUT_VERIFICATION'
    Complete-DeploymentSafety -Gate $gate -Names $Names -Images $images
    Set-DeploymentPhase 'COMPLETE'
    # Only reached once Complete-DeploymentSafety has fully succeeded for
    # every $Names component. Records durable, re-verifiable evidence that
    # THIS run's new images were genuinely promoted -- so a subsequent
    # normal deployment is not forced to treat a merely-newer-than-baseline
    # running image as unestablished provenance. This never advances the
    # approved rollback baseline itself; that remains a separate,
    # explicitly-authorized 'approve-rollback-baseline' action.
    $initImages = @{}
    if ('research-engine' -in $Names) {
        $initImages['wait-for-research-migrations'] = @{ component = 'research-engine'; image = $images['research-engine'] }
    }
    $candidatePath = Save-RollbackBaselineApprovalCandidate -ProjectRoot $ProjectRoot -RuntimeDir $RuntimeDir -Tag $tag -Names $Names -Images $images -InitImages $initImages
    Write-Host "Verified deployment completed. Old images retained; rollback references: $rollbackPath. Promotion evidence recorded: $candidatePath (not yet approved as rollback baseline -- run 'approve-rollback-baseline' explicitly if desired). Recovery remains paused. No automatic rollback."
}

function Deploy-SelectedComponents {
    $safeSelection = @(Resolve-SelectedComponents -RequireSelection)
    if (@($safeSelection | Where-Object { $_ -notin @('api-gateway', 'research-engine', 'portfolio-service', 'frontend') }).Count -eq 0) {
        # Explicitly accepting the high-risk gate always routes to the
        # existing verified/recovery path, regardless of classification --
        # more scrutiny is never refused. Otherwise, classify the change and
        # fail closed to the verified path whenever it is not confirmed
        # LOW_RISK (schema migration, infrastructure/Helm change, or
        # worker-state transition; or classification itself was unverifiable).
        if ($AcceptBootstrapRisk -or $ConfirmCallerFreeze -or $ResumeAuditTag -or $RecoverInterruptedCycleId -or $AuthorizeInterruptedCycleRecovery) {
            Deploy-VerifiedRadarComponents -Names $safeSelection
            return
        }
        $risk = Get-DeploymentRiskLevel -Names $safeSelection
        if ($risk.Level -ne 'LOW_RISK') {
            throw "Fresh DEV deployment refused: $($risk.Reasons -join '; '). This change requires the verified deployment path: re-run with -AcceptBootstrapRisk -ConfirmCallerFreeze."
        }
        Start-DeploymentFreshRollout -Names $safeSelection
        return
    }
    if (@($safeSelection | Where-Object { $_ -in @('api-gateway','research-engine','portfolio-service','frontend') }).Count) { throw 'Protected components cannot use the general deployment path' }
    if ($RecoverInterruptedCycleId -or $AuthorizeInterruptedCycleRecovery) { throw 'Interrupted-cycle recovery deploys research-engine alone' }
    Assert-LocalPrerequisites
    Assert-ExistingCluster

    $release = helm status $ReleaseName -n $Namespace -o json 2>$null
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace(($release | Out-String).Trim())) {
        throw "Component deployment requires an existing Helm release. Run '.\\platform.ps1 up' once first."
    }

    if ($SkipBuild) {
        throw "-SkipBuild is intentionally disabled for component deploys. Component deploys always build a fresh image from the current working tree."
    }

    $selected = @(Resolve-SelectedComponents -RequireSelection)
    if ($DryRun) {
        Write-Host "Dry run: build/push selected components via localhost:5001, deploy via ai-investment-registry:5000: $($selected -join ', ')"
        return
    }
    Initialize-ImageTag

    Write-Host "Selected components: $($selected -join ', ')" -ForegroundColor Cyan

    if (@($selected | Where-Object { $_ -in $JavaServices }).Count -gt 0) { Assert-BuildPrerequisites }
    Build-Images -Names $selected

    Push-SelectedImages -Names $selected
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

if ($DryRun -and $Command -ne "deploy") { throw "-DryRun is supported only for component deploy." }

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
    "reconcile-rollback-baseline" {
        Invoke-PlatformReconcileRollbackBaseline -ProjectRoot $ProjectRoot -ReconcileComponent $ReconcileComponent -KubeContext "k3d-ai-investment-dev" -KubeNamespace $Namespace -AuthorizeRollbackBaselineReconciliation:$AuthorizeRollbackBaselineReconciliation -RollbackBaselineReconciliationReason $ReconciliationReason
    }
    "approve-rollback-baseline" {
        Invoke-PlatformApproveRollbackBaseline -ProjectRoot $ProjectRoot -RollbackBaselineCandidatePath $RollbackBaselineCandidatePath -AuthorizeRollbackBaselineApproval:$AuthorizeRollbackBaselineApproval
    }
    "recover-interrupted-promotion" {
        Invoke-PlatformRecoverInterruptedPromotion -ProjectRoot $ProjectRoot -RuntimeDir $RuntimeDir -RecoveryTag $RecoveryTag -RecoveryComponent $RecoveryComponent -AuthorizeRecovery:$AuthorizeRecovery
    }
    "validate-research-migration" {
        Invoke-PlatformValidateResearchMigration -ProjectRoot $ProjectRoot -AuthorizeMigrationCleanup:$AuthorizeMigrationCleanup
    }
}
