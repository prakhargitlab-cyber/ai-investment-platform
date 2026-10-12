# Pester v5: Stage 2E Radar restore guard regression.
# Static AST validation only. Does not execute platform.ps1.

BeforeAll {
    $platformPath = Join-Path $PSScriptRoot '..\..\..\platform.ps1'

    $tokens = $null
    $parseErrors = $null

    $scriptAst = [System.Management.Automation.Language.Parser]::ParseFile(
        $platformPath,
        [ref]$tokens,
        [ref]$parseErrors
    )

    if ($parseErrors.Count -gt 0) {
        throw "platform.ps1 contains PowerShell parse errors"
    }

    $functionAst = $scriptAst.Find({
        param($node)

        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq 'Invoke-InitialOpportunityCycleAfterCleanDeploy'
    }, $true)

    if ($null -eq $functionAst) {
        throw "Radar startup function not found"
    }

    $functionText = $functionAst.Extent.Text
}

Describe 'Stage 2E Radar restore protection' {

    It 'checks the restore ledger before claiming the provisioning marker' {
        $ledgerPosition = $functionText.IndexOf('Test-DatabaseRestoreRecorded')
        $markerPosition = $functionText.IndexOf('Use-FreshProvisioningMarker')

        $ledgerPosition | Should -BeGreaterThan -1
        $markerPosition | Should -BeGreaterThan $ledgerPosition
    }

    It 'returns immediately when the restore ledger reports a restored database' {
        $guardPattern = '(?s)if\s*\(\s*Test-DatabaseRestoreRecorded\b.*?\)\s*\{(?:(?!\}).)*?\breturn\b'

        $functionText | Should -Match $guardPattern
    }

    It 'preserves the existing freshness eligibility gate' {
        $functionText | Should -Match 'Resolve-OpportunityRadarBootstrapEligibility'
        $functionText | Should -Match 'if\s*\(-not\s+\$eligibility\.Eligible\)'
    }
}