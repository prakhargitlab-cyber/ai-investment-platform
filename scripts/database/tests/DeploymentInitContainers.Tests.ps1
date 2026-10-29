# Regression test: Get-DeploymentInitContainers (platform.ps1) must never throw
# under Set-StrictMode for a Deployment JSON that has no initContainers key at
# all (mcp-gateway, yahoo-finance-mcp), and must still return the real init
# containers when present (research-engine).
#
# The function is extracted directly from platform.ps1 by source text rather
# than dot-sourcing the whole script, so this test needs no live cluster,
# Docker, or kubectl, and cannot be affected by unrelated top-level script
# behavior.

BeforeAll {
    $script:PlatformPath = Join-Path $PSScriptRoot '..\..\..\platform.ps1'
    $text = Get-Content -LiteralPath $script:PlatformPath -Raw
    $match = [regex]::Match($text, '(?s)function Get-DeploymentInitContainers \{.*?\r?\n\}')
    if (-not $match.Success) { throw "Get-DeploymentInitContainers not found in platform.ps1 -- test cannot run." }
    Invoke-Expression $match.Value
}

Describe 'Get-DeploymentInitContainers' {
    It 'returns an empty array under strict mode when initContainers is absent entirely (mcp-gateway/yahoo-finance-mcp shape)' {
        Set-StrictMode -Version Latest
        $deploymentJson = '{"spec":{"template":{"spec":{"containers":[{"name":"mcp-gateway"}]}}}}'
        $deployment = $deploymentJson | ConvertFrom-Json
        { Get-DeploymentInitContainers $deployment } | Should -Not -Throw
        @(Get-DeploymentInitContainers $deployment).Count | Should -Be 0
    }

    It 'returns the real init containers under strict mode when present (research-engine shape)' {
        Set-StrictMode -Version Latest
        $deploymentJson = '{"spec":{"template":{"spec":{"containers":[{"name":"research-engine"}],"initContainers":[{"name":"wait-for-research-migrations","image":"x:1"}]}}}}'
        $deployment = $deploymentJson | ConvertFrom-Json
        $result = @(Get-DeploymentInitContainers $deployment)
        $result.Count | Should -Be 1
        $result[0].name | Should -Be 'wait-for-research-migrations'
    }

    It 'returns an empty array when initContainers is present but null' {
        Set-StrictMode -Version Latest
        $deploymentJson = '{"spec":{"template":{"spec":{"containers":[{"name":"x"}],"initContainers":null}}}}'
        $deployment = $deploymentJson | ConvertFrom-Json
        @(Get-DeploymentInitContainers $deployment).Count | Should -Be 0
    }
}
