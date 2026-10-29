# Explicitly reconcile an already-applied DEV configuration; never deploy it.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][ValidatePattern('^[a-f0-9]{64}$')]
    [string]$ExpectedRecordedHelmFingerprint,
    [Parameter(Mandatory = $true)][ValidatePattern('^[a-f0-9]{64}$')]
    [string]$ExpectedCurrentHelmFingerprint,
    [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Reason,
    [switch]$AuthorizeHelmConfigurationReconciliation
)

$ErrorActionPreference = 'Stop'
if (-not $AuthorizeHelmConfigurationReconciliation -or [string]::IsNullOrWhiteSpace($Reason)) {
    throw "Specify -AuthorizeHelmConfigurationReconciliation and -Reason '<why this Helm change is approved>'."
}

$ProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$RuntimeDir = Join-Path $ProjectRoot '.tmp'
$HelmChart = Join-Path $ProjectRoot 'infrastructure/helm/ai-investment-platform'
$HelmValues = Join-Path $HelmChart 'values-dev.yaml'
$script:DeploymentRun = $null

# platform.ps1 defaults to 'up' and dispatches at top level. Never dot-source it.
# Load only these top-level definitions, leaving imports and dispatch unevaluated.
$tokens = $null; $parseErrors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile(
    (Join-Path $ProjectRoot 'platform.ps1'), [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw "Cannot parse platform.ps1: $($parseErrors -join '; ')" }
$functionNames = @(
    'ConvertTo-DeploymentArgument', 'Invoke-DeploymentNativeCommand',
    'Invoke-DeploymentCommand', 'Invoke-DeploymentKubectl',
    'Get-DeploymentFreshBaselinePath', 'Get-DeploymentFreshBaseline',
    'Save-DeploymentFreshBaseline', 'Get-DeploymentHelmFingerprint',
    'Get-LocalFullEtfExecutionFlag', 'Approve-DeploymentHelmConfigurationChange'
)
foreach ($name in $functionNames) {
    $definitions = @($ast.EndBlock.Statements | Where-Object {
        $_ -is [Management.Automation.Language.FunctionDefinitionAst] -and $_.Name -eq $name
    })
    if ($definitions.Count -ne 1) { throw "Expected exactly one top-level function '$name' in platform.ps1." }
    . ([scriptblock]::Create($definitions[0].Extent.Text))
}

$baseline = Get-DeploymentFreshBaseline -Name 'research-engine'
if (-not $baseline) { throw 'No existing fresh-deploy baseline for research-engine.' }
if ($baseline.helmFingerprint -ne $ExpectedRecordedHelmFingerprint) {
    throw "Recorded Helm fingerprint changed: $($baseline.helmFingerprint). Review the baseline again."
}
$currentHelmFingerprint = Get-DeploymentHelmFingerprint
if ($currentHelmFingerprint -ne $ExpectedCurrentHelmFingerprint) {
    throw "Current Helm fingerprint changed: $currentHelmFingerprint. Review the chart again."
}

# Keep the existing authorization, baseline and live/local flag guards intact.
Approve-DeploymentHelmConfigurationChange -Name 'research-engine' -Reason $Reason `
    -AuthorizeHelmConfigurationReconciliation:$AuthorizeHelmConfigurationReconciliation
