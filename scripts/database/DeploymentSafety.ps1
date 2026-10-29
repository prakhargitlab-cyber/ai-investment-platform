# Compatibility loader only. All orchestration and diagnostics live in platform.ps1.
# Extract definitions; never execute the platform entry point when loading this file.
$deploymentTokens=$null; $deploymentErrors=$null
$deploymentAst=[Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot '../../platform.ps1'),[ref]$deploymentTokens,[ref]$deploymentErrors)
if ($deploymentErrors.Count) { throw 'platform.ps1 parse failure' }
foreach($deploymentNode in $deploymentAst.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -match 'Deployment|IsolationProbe|VerifiedPodImage|VerifiedResearchRejectCounter'},$false)) {
    . ([scriptblock]::Create($deploymentNode.Extent.Text))
}
