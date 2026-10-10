
# Stage 2E: Behavioral regression tests for automatic Radar protection.
# PowerShell 5.1 / Pester 5.x.
#
# Extracts only the Radar startup function from platform.ps1.
# Does NOT execute platform.ps1 or contact Kubernetes/PostgreSQL.

BeforeAll {
    $platformPath = Join-Path $PSScriptRoot '..\..\..\platform.ps1'

    $tokens = $null
    $parseErrors = $null

    $ast = [System.Management.Automation.Language.Parser]::ParseFile(
        $platformPath,
        [ref]$tokens,
        [ref]$parseErrors
    )

    if ($parseErrors.Count -gt 0) {
        throw "platform.ps1 contains parse errors"
    }

    $functionAst = $ast.Find({
        param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq 'Invoke-InitialOpportunityCycleAfterCleanDeploy'
    }, $true)

    if ($null -eq $functionAst) {
        throw "Radar startup function not found"
    }

    # Import only this function into the test scope.
    # The platform entry point is never executed.
    . ([scriptblock]::Create($functionAst.Extent.Text))

    # Test-only configuration.
    $script:Namespace = 'stage2e-isolated'
    $script:DatabaseConfig = @{
        Username = 'fixture_user'
        Name = 'fixture_database'
    }

    $script:FirstAdminCreatedThisStartup = $false

    # Existing local marker check must not short-circuit the test.
    $script:OpportunityBootstrapMarkerFile =
        Join-Path $TestDrive 'radar-bootstrap-marker.txt'

    # Catch any unexpected command execution.
    function Write-Step {
        param([string]$Message)
    }

    function Get-PostgresPodName {
        param([string]$Namespace)
        return 'fixture-postgres'
    }

    function Test-DatabaseRestoreRecorded {
        param(
            [string]$Namespace,
            [string]$PodName,
            [string]$DatabaseUser,
            [string]$DatabaseName
        )
        throw 'Restore ledger mock not configured'
    }

    function Use-FreshProvisioningMarker {
        throw 'Unexpected marker claim'
    }

    function Resolve-ResearchDatabaseSchemaName {
        throw 'Unexpected research schema lookup'
    }

    function Invoke-PodExec {
        throw 'Unexpected Kubernetes execution'
    }

    function Resolve-OpportunityRadarBootstrapEligibility {
        throw 'Unexpected eligibility evaluation'
    }
}

Describe 'Stage 2E Radar behavioral restore protection' {

    BeforeEach {
        Remove-Item $script:OpportunityBootstrapMarkerFile `
            -Force -ErrorAction SilentlyContinue
    }

    It 'skips restored databases without claiming the marker' {
        Mock Test-DatabaseRestoreRecorded { return $true }

        Mock Use-FreshProvisioningMarker {
            throw 'Marker must not be claimed'
        }

        Mock Resolve-ResearchDatabaseSchemaName {
            throw 'Schema must not be queried'
        }

        Mock Invoke-PodExec {
            throw 'No Kubernetes operation permitted'
        }

        Mock Resolve-OpportunityRadarBootstrapEligibility {
            throw 'Eligibility must not be evaluated'
        }

        Invoke-InitialOpportunityCycleAfterCleanDeploy

        Should -Invoke Test-DatabaseRestoreRecorded -Times 1 -Exactly
        Should -Invoke Use-FreshProvisioningMarker -Times 0 -Exactly
        Should -Invoke Resolve-ResearchDatabaseSchemaName -Times 0 -Exactly
        Should -Invoke Invoke-PodExec -Times 0 -Exactly
        Should -Invoke Resolve-OpportunityRadarBootstrapEligibility -Times 0 -Exactly

        Test-Path $script:OpportunityBootstrapMarkerFile |
            Should -BeFalse
    }

    It 'fails closed when restore ledger lookup throws' {
        Mock Test-DatabaseRestoreRecorded {
            throw 'Simulated restore ledger query failure'
        }

        Mock Use-FreshProvisioningMarker {
            throw 'Marker must not be claimed'
        }

        Mock Resolve-ResearchDatabaseSchemaName {
            throw 'Schema must not be queried'
        }

        Mock Invoke-PodExec {
            throw 'No Kubernetes operation permitted'
        }

        Mock Resolve-OpportunityRadarBootstrapEligibility {
            return [pscustomobject]@{
                Eligible = $false
                Decision = 'SKIP_UNABLE_TO_DETERMINE'
                Reason = 'Restore ledger unavailable'
                WriteMarker = $false
            }
        }

        Invoke-InitialOpportunityCycleAfterCleanDeploy

        Should -Invoke Test-DatabaseRestoreRecorded -Times 1 -Exactly
        Should -Invoke Use-FreshProvisioningMarker -Times 0 -Exactly
        Should -Invoke Resolve-ResearchDatabaseSchemaName -Times 0 -Exactly
        Should -Invoke Invoke-PodExec -Times 0 -Exactly
        Should -Invoke Resolve-OpportunityRadarBootstrapEligibility -Times 1 -Exactly

        Test-Path $script:OpportunityBootstrapMarkerFile |
            Should -BeFalse
    }

    It 'allows a fresh database to reach the existing freshness checks' {
        Mock Test-DatabaseRestoreRecorded { return $false }

        Mock Use-FreshProvisioningMarker {
            return $false
        }

        Mock Resolve-ResearchDatabaseSchemaName {
            return 'research'
        }

        # Simulate unavailable schema probes.
        # This forces the existing eligibility logic to skip Radar.
        Mock Invoke-PodExec {
            return [pscustomobject]@{
                ExitCode = 1
                Output = ''
            }
        }

        Mock Resolve-OpportunityRadarBootstrapEligibility {
            return [pscustomobject]@{
                Eligible = $false
                Decision = 'SKIP_UNABLE_TO_DETERMINE'
                Reason = 'Schema probe failed'
                WriteMarker = $false
            }
        }

        Invoke-InitialOpportunityCycleAfterCleanDeploy

        Should -Invoke Test-DatabaseRestoreRecorded -Times 1 -Exactly
        Should -Invoke Use-FreshProvisioningMarker -Times 1 -Exactly
        Should -Invoke Resolve-ResearchDatabaseSchemaName -Times 1 -Exactly
        Should -Invoke Invoke-PodExec -Times 1 -Exactly
        Should -Invoke Resolve-OpportunityRadarBootstrapEligibility -Times 1 -Exactly

        Test-Path $script:OpportunityBootstrapMarkerFile |
            Should -BeFalse
    }
}
