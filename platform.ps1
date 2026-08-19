param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet("up", "down", "status")]
    [string]$Command,

    [Parameter(Mandatory = $true, Position = 1)]
    [ValidateSet("DEV", "PRD")]
    [string]$Environment
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$HelmChart = Join-Path $ProjectRoot "infrastructure\helm\ai-investment-platform"
$K3dConfig = Join-Path $ProjectRoot "infrastructure\k3d\cluster-dev.yaml"
$TerraformPrd = Join-Path $ProjectRoot "infrastructure\terraform\environments\prd"
$AzureConfigPath = Join-Path $ProjectRoot "config\prd\azure.json"
$AzureConfig = Get-Content -Raw $AzureConfigPath | ConvertFrom-Json

function Require-Command {
    param([string]$Name)
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required command '$Name' was not found on PATH."
    }
}

function Assert-AzureSubscription {
    Require-Command "az"
    $account = az account show --query "{subscriptionId:id, name:name, tenantId:tenantId}" -o json | ConvertFrom-Json
    if ($account.subscriptionId -ne $AzureConfig.subscriptionId) {
        throw "Azure CLI is using subscription '$($account.subscriptionId)' ('$($account.name)'). Expected '$($AzureConfig.subscriptionId)'. Run: az account set --subscription `"$($AzureConfig.subscriptionId)`""
    }
}

function Up-Dev {
    Require-Command "k3d"
    Require-Command "kubectl"
    Require-Command "helm"

    k3d cluster create --config $K3dConfig
    helm upgrade --install ai-investment-platform $HelmChart --namespace ai-investment --create-namespace --values (Join-Path $HelmChart "values-dev.yaml")
}

function Down-Dev {
    Require-Command "k3d"
    k3d cluster delete ai-investment-dev
}

function Status-Dev {
    Require-Command "kubectl"
    kubectl get nodes
    kubectl get pods -n ai-investment
    kubectl get svc -n ai-investment
}

function Up-Prd {
    Assert-AzureSubscription
    Require-Command "terraform"
    Require-Command "helm"
    Push-Location $TerraformPrd
    try {
        terraform init
        terraform plan -out tfplan `
            -var "subscription_id=$($AzureConfig.subscriptionId)" `
            -var "tenant_id=$($AzureConfig.tenantId)" `
            -var "location=$($AzureConfig.location)"
        Write-Host "Review the Terraform plan before running: terraform apply tfplan"
        Write-Host "This script intentionally stops before apply in the foundation iteration."
    }
    finally {
        Pop-Location
    }
}

function Down-Prd {
    Assert-AzureSubscription
    Require-Command "terraform"
    Write-Warning "This will run terraform destroy for the PRD Terraform state in '$TerraformPrd'."
    Write-Warning "Only resources managed by this project's Terraform state should be destroyed."
    $confirmation = Read-Host "Type DESTROY ai-investment-platform PRD to continue"
    if ($confirmation -ne "DESTROY ai-investment-platform PRD") {
        Write-Host "PRD destroy cancelled."
        return
    }
    Push-Location $TerraformPrd
    try {
        terraform init
        terraform destroy `
            -var "subscription_id=$($AzureConfig.subscriptionId)" `
            -var "tenant_id=$($AzureConfig.tenantId)" `
            -var "location=$($AzureConfig.location)"
        Write-Host "Audit remaining resources with:"
        Write-Host "az resource list --tag project=ai-investment-platform --output table"
    }
    finally {
        Pop-Location
    }
}

function Status-Prd {
    Assert-AzureSubscription
    Require-Command "terraform"
    Push-Location $TerraformPrd
    try {
        terraform state list
        terraform output
    }
    finally {
        Pop-Location
    }
}

switch ("$Command-$Environment") {
    "up-DEV" { Up-Dev }
    "down-DEV" { Down-Dev }
    "status-DEV" { Status-Dev }
    "up-PRD" { Up-Prd }
    "down-PRD" { Down-Prd }
    "status-PRD" { Status-Prd }
}
