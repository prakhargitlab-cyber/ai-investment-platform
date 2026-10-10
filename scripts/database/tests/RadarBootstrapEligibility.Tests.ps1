# Pester v5 tests for Resolve-OpportunityRadarBootstrapEligibility
# (scripts/database/RadarBootstrapEligibility.psm1).
#
# Pure eligibility tests; no database or Kubernetes access.

BeforeAll {
    $modulePath = Join-Path $PSScriptRoot "..\RadarBootstrapEligibility.psm1"
    Import-Module $modulePath -Force
}

Describe "Resolve-OpportunityRadarBootstrapEligibility" {

    It "denies empty tables without a provisioning marker" {
        $result = Resolve-OpportunityRadarBootstrapEligibility -CycleTableProbeSucceeded $true -CycleTableExists $true `
            -CycleRowCountProbeSucceeded $true -CycleRowCount 0 -UserTableProbeSucceeded $true -UserTableExists $true `
            -UserRowCountProbeSucceeded $true -UserRowCount 0
        $result.Eligible | Should -BeFalse
        $result.Decision | Should -Be 'SKIP_NO_FRESH_PROVISIONING_PROOF'
    }

    It "allows exactly the admin created by this startup with a new provisioning marker" {
        $result = Resolve-OpportunityRadarBootstrapEligibility -FreshProvisioningClaimed $true -FirstAdminCreatedThisStartup $true `
            -CycleTableProbeSucceeded $true -CycleTableExists $true -CycleRowCountProbeSucceeded $true -CycleRowCount 0 `
            -UserTableProbeSucceeded $true -UserTableExists $true -UserRowCountProbeSucceeded $true -UserRowCount 1
        $result.Eligible | Should -BeTrue
    }

    It "denies additional users even after first-admin creation" {
        $result = Resolve-OpportunityRadarBootstrapEligibility -FreshProvisioningClaimed $true -FirstAdminCreatedThisStartup $true `
            -CycleTableProbeSucceeded $true -CycleTableExists $true -CycleRowCountProbeSucceeded $true -CycleRowCount 0 `
            -UserTableProbeSucceeded $true -UserTableExists $true -UserRowCountProbeSucceeded $true -UserRowCount 2
        $result.Eligible | Should -BeFalse
    }

    It "denies negative counts" {
        $result = Resolve-OpportunityRadarBootstrapEligibility -FreshProvisioningClaimed $true `
            -CycleTableProbeSucceeded $true -CycleTableExists $true -CycleRowCountProbeSucceeded $true -CycleRowCount -1 `
            -UserTableProbeSucceeded $true -UserTableExists $true -UserRowCountProbeSucceeded $true -UserRowCount 0
        $result.Eligible | Should -BeFalse
    }

    Context "Genuinely fresh database" {
        It "is eligible when both tables exist and both row counts are zero" {
            $result = Resolve-OpportunityRadarBootstrapEligibility -FreshProvisioningClaimed $true `
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
            $result = Resolve-OpportunityRadarBootstrapEligibility -FreshProvisioningClaimed $true `
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
            $result = Resolve-OpportunityRadarBootstrapEligibility -FreshProvisioningClaimed $true `
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
            $result = Resolve-OpportunityRadarBootstrapEligibility -FreshProvisioningClaimed $true `
                -CycleTableProbeSucceeded $false -CycleTableExists $false `
                -CycleRowCountProbeSucceeded $false -CycleRowCount $null `
                -UserTableProbeSucceeded $false -UserTableExists $false `
                -UserRowCountProbeSucceeded $false -UserRowCount $null

            $result.Eligible | Should -BeFalse
            $result.Decision | Should -Be "SKIP_UNABLE_TO_DETERMINE"
            $result.WriteMarker | Should -BeFalse
        }

        It "skips WITHOUT writing the marker when tables exist but the row-count probe failed" {
            $result = Resolve-OpportunityRadarBootstrapEligibility -FreshProvisioningClaimed $true `
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
            $result = Resolve-OpportunityRadarBootstrapEligibility -FreshProvisioningClaimed $true `
                -CycleTableProbeSucceeded $true -CycleTableExists $false `
                -CycleRowCountProbeSucceeded $false -CycleRowCount $null `
                -UserTableProbeSucceeded $true -UserTableExists $true `
                -UserRowCountProbeSucceeded $true -UserRowCount 0

            $result.Eligible | Should -BeFalse
            $result.Decision | Should -Be "SKIP_SCHEMA_OR_TABLE_MISSING"
            $result.WriteMarker | Should -BeFalse
        }

        It "skips WITHOUT writing the marker when the auth user table does not exist" {
            $result = Resolve-OpportunityRadarBootstrapEligibility -FreshProvisioningClaimed $true `
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
            $result = Resolve-OpportunityRadarBootstrapEligibility -FreshProvisioningClaimed $true `
                -CycleTableProbeSucceeded $true -CycleTableExists $false `
                -CycleRowCountProbeSucceeded $false -CycleRowCount $null `
                -UserTableProbeSucceeded $true -UserTableExists $false `
                -UserRowCountProbeSucceeded $false -UserRowCount $null

            $result.Eligible | Should -BeFalse
        }
    }
}
