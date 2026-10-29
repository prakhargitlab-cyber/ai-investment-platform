# Offline only: real approval/baseline functions, an isolated root and a fake native boundary.
Describe 'Explicit DEV Research Engine Helm reconciliation entry point' {
    BeforeAll {
        $repositoryRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../..'))
        $entryPoint = Join-Path $repositoryRoot 'scripts/database/Approve-ResearchHelmConfiguration.ps1'
        $tokens = $null; $parseErrors = $null
        $platformAst = [Management.Automation.Language.Parser]::ParseFile(
            (Join-Path $repositoryRoot 'platform.ps1'), [ref]$tokens, [ref]$parseErrors)
        if ($parseErrors.Count) { throw ($parseErrors -join '; ') }
        $names = @(
            'ConvertTo-DeploymentArgument', 'Invoke-DeploymentCommand', 'Invoke-DeploymentKubectl',
            'Get-DeploymentFreshBaselinePath', 'Get-DeploymentFreshBaseline',
            'Save-DeploymentFreshBaseline', 'Get-DeploymentHelmFingerprint',
            'Get-LocalFullEtfExecutionFlag', 'Approve-DeploymentHelmConfigurationChange'
        )
        $definitions = foreach ($name in $names) {
            $node = @($platformAst.EndBlock.Statements | Where-Object {
                $_ -is [Management.Automation.Language.FunctionDefinitionAst] -and $_.Name -eq $name
            })
            if ($node.Count -ne 1) { throw "Missing or ambiguous production function: $name" }
            $node[0].Extent.Text
        }
        $fakeNativeBoundary = @'
function Invoke-DeploymentNativeCommand {
    param($Program, $Arguments)
    if ($Program -ne 'kubectl' -or ($Arguments -join ' ') -ne '--context k3d-ai-investment-dev -n ai-investment get deployment research-engine -o json') {
        throw 'Unexpected native command; only the DEV deployment GET is allowed.'
    }
    Add-Content -LiteralPath (Join-Path $ProjectRoot 'native-calls.txt') -Value "$Program $($Arguments -join ' ')"
    return [pscustomobject]@{
        ExitCode = [int](Get-Content -LiteralPath (Join-Path $ProjectRoot 'native-exit.txt'))
        StdOut = Get-Content -LiteralPath (Join-Path $ProjectRoot 'live-deployment.json') -Raw
        StdErr = ''
    }
}
'@
        # Any evaluation of platform's top level (including dot-sourcing) fails.
        $fixturePlatform = (@("throw 'Platform top-level dispatch must never execute.'") + $definitions + @($fakeNativeBoundary)) -join "`n"
        $hashDefinition = $platformAst.EndBlock.Statements | Where-Object {
            $_ -is [Management.Automation.Language.FunctionDefinitionAst] -and $_.Name -eq 'Get-DeploymentHelmFingerprint'
        }
        . ([scriptblock]::Create($hashDefinition.Extent.Text))
    }

    BeforeEach {
        $ProjectRoot = Join-Path $TestDrive ([guid]::NewGuid().ToString('N'))
        $HelmChart = Join-Path $ProjectRoot 'infrastructure/helm/ai-investment-platform'
        $HelmValues = Join-Path $HelmChart 'values-dev.yaml'
        $baselinePath = Join-Path $ProjectRoot '.tmp/fresh-deploy-state/research-engine.json'
        $wrapperPath = Join-Path $ProjectRoot 'scripts/database/Approve-ResearchHelmConfiguration.ps1'
        foreach ($directory in @($HelmChart, (Split-Path $baselinePath), (Split-Path $wrapperPath))) {
            New-Item -ItemType Directory -Path $directory -Force | Out-Null
        }
        Copy-Item -LiteralPath $entryPoint -Destination $wrapperPath
        Set-Content -LiteralPath (Join-Path $ProjectRoot 'platform.ps1') -Value $fixturePlatform
        Set-Content -LiteralPath $HelmValues -Value "research:`n  etfFullUniverseExecutionEnabled: false"
        Set-Content -LiteralPath (Join-Path $ProjectRoot 'native-exit.txt') -Value '0'
        $liveDeployment = @{ spec = @{ template = @{ spec = @{ containers = @(@{
            name = 'research-engine'
            env = @(@{ name = 'AIP_RESEARCH_ETF_FULL_UNIVERSE_EXECUTION_ENABLED'; value = 'false' })
        }) } } } }
        $livePath = Join-Path $ProjectRoot 'live-deployment.json'
        $liveDeployment | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $livePath
        $initialBaseline = @{
            image = 'registry/research-engine@sha256:existing-image'
            migrationFingerprint = @{ 'V1.sql' = 123 }
            helmFingerprint = ('a' * 64)
            recordedAtUtc = '2026-10-20T00:00:00Z'
        }
        $initialBaseline | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $baselinePath
        $baselineHash = (Get-FileHash -LiteralPath $baselinePath).Hash
        $arguments = @{
            ExpectedRecordedHelmFingerprint = $initialBaseline.helmFingerprint
            ExpectedCurrentHelmFingerprint = Get-DeploymentHelmFingerprint
            Reason = 'Approved already-applied DEV configuration.'
            AuthorizeHelmConfigurationReconciliation = $true
        }
    }

    It 'rejects <case> before any native command or baseline write' -ForEach @(
        @{ case = 'absent authorization'; field = 'AuthorizeHelmConfigurationReconciliation'; value = $false; expectedError = '*Specify -Authorize*' }
        @{ case = 'blank reason'; field = 'Reason'; value = ' '; expectedError = '*Specify -Authorize*' }
        @{ case = 'changed recorded fingerprint'; field = 'ExpectedRecordedHelmFingerprint'; value = ('b' * 64); expectedError = '*Recorded Helm fingerprint changed*' }
        @{ case = 'changed current fingerprint'; field = 'ExpectedCurrentHelmFingerprint'; value = ('b' * 64); expectedError = '*Current Helm fingerprint changed*' }
    ) {
        $arguments[$field] = $value
        { & $wrapperPath @arguments } | Should -Throw $expectedError
        (Get-FileHash -LiteralPath $baselinePath).Hash | Should -Be $baselineHash
        Test-Path -LiteralPath (Join-Path $ProjectRoot 'native-calls.txt') | Should -BeFalse
    }

    It 'does not create a missing baseline' {
        # Change the fixture name instead of deleting any file.
        Rename-Item -LiteralPath $baselinePath -NewName 'saved-baseline.json'
        { & $wrapperPath @arguments } | Should -Throw '*No existing fresh-deploy baseline*'
        Test-Path -LiteralPath $baselinePath | Should -BeFalse
        Test-Path -LiteralPath (Join-Path $ProjectRoot 'native-calls.txt') | Should -BeFalse
    }

    It 'preserves the existing live/local flag mismatch guard' {
        $liveDeployment.spec.template.spec.containers[0].env[0].value = 'true'
        $liveDeployment | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $livePath
        { & $wrapperPath @arguments } | Should -Throw '*does not yet match local values-dev.yaml*'
        (Get-FileHash -LiteralPath $baselinePath).Hash | Should -Be $baselineHash
    }

    It 'preserves the existing ambiguous local flag guard' {
        Add-Content -LiteralPath $HelmValues -Value '  etfFullUniverseExecutionEnabled: false'
        $arguments.ExpectedCurrentHelmFingerprint = Get-DeploymentHelmFingerprint
        { & $wrapperPath @arguments } | Should -Throw '*flag missing or ambiguous*'
        (Get-FileHash -LiteralPath $baselinePath).Hash | Should -Be $baselineHash
    }

    It 'fails closed on a native query error' {
        Set-Content -LiteralPath (Join-Path $ProjectRoot 'native-exit.txt') -Value '1'
        { & $wrapperPath @arguments } | Should -Throw '*kubectl failed (exit 1)*'
        (Get-FileHash -LiteralPath $baselinePath).Hash | Should -Be $baselineHash
    }

    It 'rejects malformed platform source without executing it' {
        Add-Content -LiteralPath (Join-Path $ProjectRoot 'platform.ps1') -Value 'function Broken {'
        { & $wrapperPath @arguments } | Should -Throw '*Cannot parse platform.ps1*'
        (Get-FileHash -LiteralPath $baselinePath).Hash | Should -Be $baselineHash
        Test-Path -LiteralPath (Join-Path $ProjectRoot 'native-calls.txt') | Should -BeFalse
    }

    It 'rejects ambiguous function definitions without executing platform dispatch' {
        Add-Content -LiteralPath (Join-Path $ProjectRoot 'platform.ps1') -Value 'function Get-LocalFullEtfExecutionFlag {}'
        { & $wrapperPath @arguments } | Should -Throw '*Expected exactly one top-level function*'
        (Get-FileHash -LiteralPath $baselinePath).Hash | Should -Be $baselineHash
        Test-Path -LiteralPath (Join-Path $ProjectRoot 'native-calls.txt') | Should -BeFalse
    }

    It 'uses the original approval function and only updates the isolated Helm baseline and timestamp' {
        & $wrapperPath @arguments
        $actual = Get-Content -LiteralPath $baselinePath -Raw | ConvertFrom-Json
        $actual.helmFingerprint | Should -Be $arguments.ExpectedCurrentHelmFingerprint
        $actual.image | Should -Be $initialBaseline.image
        $actual.migrationFingerprint.'V1.sql' | Should -Be 123
        $actual.recordedAtUtc | Should -Not -Be $initialBaseline.recordedAtUtc
        @($actual.PSObject.Properties).Count | Should -Be 4
        @(Get-Content -LiteralPath (Join-Path $ProjectRoot 'native-calls.txt')).Count | Should -Be 1
    }
}
