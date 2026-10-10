# Pester v5 tests for Resolve-OpportunityRadarBootstrapEligibility
# (scripts/database/RadarBootstrapEligibility.psm1).
#
# These are pure unit tests: no database, pod, or Kubernetes access is
# required or performed. They were written but NOT executed in this
# environment -- no PowerShell interpreter (pwsh/powershell) is available
# anywhere this work was done. Run with: Invoke-Pester -Path <this file>
# on a Windows machine with Pester 5+ installed before relying on this.

BeforeAll {
    $modulePath = Join-Path $PSScriptRoot "..\RadarBootstrapEligibility.psm1"
    . $modulePath
}

Describe "Resolve-OpportunityRadarBootstrapEligibility" {

    Context "Genuinely fresh database" {
        It "is eligible when both tables exist and both row counts are zero" {
            $result = Resolve-OpportunityRadarBootstrapEligibility `
                -CycleTableProbeSucceeded $true -CycleTableExists $true `
                -CycleRowCountProbeSucceeded $true -CycleRowCount 0 `
                -UserTableProbeSucceeded $true -UserTableExists $true `
                -UserRowCountProbeSucceeded $true -UserRowCount 0

            $result.Eligible | Should -BeTrue
            $result.Decision | Should -Be "ELIGIBLE_GENUINELY_FRESH"
            $result.WriteMarker | Should -BeFalse
        }
    }

    Context "Restored / populated database (existing Radar cycle)" {
        It "skips and writes the marker when any cycle row exists, regardless of status" {
            $result = Resolve-OpportunityRadarBootstrapEligibility `
                -CycleTableProbeSucceeded $true -CycleTableExists $true `
                -CycleRowCountProbeSucceeded $true -CycleRowCount 3 `
                -UserTableProbeSucceeded $true -UserTableExists $true `
                -UserRowCountProbeSucceeded $true -UserRowCount 0

            $result.Eligible | Should -BeFalse
            $result.Decision | Should -Be "SKIP_EXISTING_CYCLE_FOUND"
            $result.WriteMarker | Should -BeTrue
        }
    }

    Context "Populated database without any completed/existing cycle row (existing application data only)" {
        It "skips and writes the marker when a user account exists even with zero cycle rows" {
            $result = Resolve-OpportunityRadarBootstrapEligibility `
                -CycleTableProbeSucceeded $true -CycleTableExists $true `
                -CycleRowCountProbeSucceeded $true -CycleRowCount 0 `
                -UserTableProbeSucceeded $true -UserTableExists $true `
                -UserRowCountProbeSucceeded $true -UserRowCount 1

            $result.Eligible | Should -BeFalse
            $result.Decision | Should -Be "SKIP_EXISTING_APPLICATION_DATA_FOUND"
            $result.WriteMarker | Should -BeTrue
        }
    }

    Context "Query failure (indeterminate database state)" {
        It "skips WITHOUT writing the marker when the existence probe itself failed" {
            $result = Resolve-OpportunityRadarBootstrapEligibility `
                -CycleTableProbeSucceeded $false -CycleTableExists $false `
                -CycleRowCountProbeSucceeded $false -CycleRowCount $null `
                -UserTableProbeSucceeded $false -UserTableExists $false `
                -UserRowCountProbeSucceeded $false -UserRowCount $null

            $result.Eligible | Should -BeFalse
            $result.Decision | Should -Be "SKIP_UNABLE_TO_DETERMINE"
            $result.WriteMarker | Should -BeFalse
        }

        It "skips WITHOUT writing the marker when tables exist but the row-count probe failed" {
            $result = Resolve-OpportunityRadarBootstrapEligibility `
                -CycleTableProbeSucceeded $true -CycleTableExists $true `
                -CycleRowCountProbeSucceeded $false -CycleRowCount $null `
                -UserTableProbeSucceeded $true -UserTableExists $true `
                -UserRowCountProbeSucceeded $true -UserRowCount 0

            $result.Eligible | Should -BeFalse
            $result.Decision | Should -Be "SKIP_UNABLE_TO_DETERMINE"
            $result.WriteMarker | Should -BeFalse
        }
    }

    Context "Schema/table missing" {
        It "skips WITHOUT writing the marker when the Radar cycle table does not exist, and does NOT treat this as fresh" {
            $result = Resolve-OpportunityRadarBootstrapEligibility `
                -CycleTableProbeSucceeded $true -CycleTableExists $false `
                -CycleRowCountProbeSucceeded $false -CycleRowCount $null `
                -UserTableProbeSucceeded $true -UserTableExists $true `
                -UserRowCountProbeSucceeded $true -UserRowCount 0

            $result.Eligible | Should -BeFalse
            $result.Decision | Should -Be "SKIP_SCHEMA_OR_TABLE_MISSING"
            $result.WriteMarker | Should -BeFalse
        }

        It "skips WITHOUT writing the marker when the auth user table does not exist" {
            $result = Resolve-OpportunityRadarBootstrapEligibility `
                -CycleTableProbeSucceeded $true -CycleTableExists $true `
                -CycleRowCountProbeSucceeded $false -CycleRowCount $null `
                -UserTableProbeSucceeded $true -UserTableExists $false `
                -UserRowCountProbeSucceeded $false -UserRowCount $null

            $result.Eligible | Should -BeFalse
            $result.Decision | Should -Be "SKIP_SCHEMA_OR_TABLE_MISSING"
            $result.WriteMarker | Should -BeFalse
        }

        It "never returns Eligible = true merely because a table is absent" {
            # Defends specifically against "a missing research schema must
            # not automatically be interpreted as a fresh database."
            $result = Resolve-OpportunityRadarBootstrapEligibility `
                -CycleTableProbeSucceeded $true -CycleTableExists $false `
                -CycleRowCountProbeSucceeded $false -CycleRowCount $null `
                -UserTableProbeSucceeded $true -UserTableExists $false `
                -UserRowCountProbeSucceeded $false -UserRowCount $null

            $result.Eligible | Should -BeFalse
        }
    }
}
