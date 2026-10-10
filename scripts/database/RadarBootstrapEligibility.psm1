# Pure, side-effect-free eligibility decision for the one-time initial
# Opportunity Radar cycle (platform.ps1's Invoke-InitialOpportunityCycleAfterCleanDeploy).
#
# This module intentionally contains NO database/network I/O so it can be
# unit tested (see scripts/database/tests/RadarBootstrapEligibility.Tests.ps1)
# without a live PostgreSQL connection. The caller is responsible for
# reliably gathering the input signals (table existence + row counts for
# both the Radar cycle-run table and the auth user table) and for acting on
# the returned decision.
#
# Positive-freshness requirement: the initial cycle is eligible to run ONLY
# when BOTH of the following were reliably, positively confirmed:
#   1. <research-schema>.global_opportunity_cycle_run exists and has zero
#      rows (no Radar cycle of ANY status -- ACCEPTED/RUNNING/PUBLISHED/
#      CANCEL_REQUESTED/COMPLETED/FAILED/CANCELLED -- has ever been
#      attempted).
#   2. auth.app_users exists and has zero rows (no user account -- and
#      therefore no first-ADMIN -- has ever been created; this is the same
#      "has this system ever been used" signal FirstAdminBootstrapService
#      itself relies on for its own freshness decision).
# Any of the following makes the database NOT positively fresh, and the
# initial cycle is skipped: either table missing, either row count
# unreadable, OR either row count greater than zero. A missing schema/table
# is never interpreted as evidence of freshness -- it is treated the same
# as "cannot determine," because by the time this runs, research-service's
# and auth-service's own Flyway migrations are expected to have already
# created both tables; a missing table at this point means something is
# uncertain, not that the database is new.

function Resolve-OpportunityRadarBootstrapEligibility {
    param(
        [Parameter(Mandatory = $true)][bool]$CycleTableProbeSucceeded,
        [bool]$CycleTableExists,
        [bool]$CycleRowCountProbeSucceeded,
        [AllowNull()][Nullable[long]]$CycleRowCount,

        [Parameter(Mandatory = $true)][bool]$UserTableProbeSucceeded,
        [bool]$UserTableExists,
        [bool]$UserRowCountProbeSucceeded,
        [AllowNull()][Nullable[long]]$UserRowCount
    )

    if (-not $CycleTableProbeSucceeded -or -not $UserTableProbeSucceeded) {
        return [ordered]@{
            Eligible    = $false
            Decision    = "SKIP_UNABLE_TO_DETERMINE"
            WriteMarker = $false
            Reason      = "Unable to reliably query the database to check whether the Radar cycle-run table and auth.app_users exist. Database state is unknown; skipping the automatic initial cycle."
        }
    }

    if (-not $CycleTableExists -or -not $UserTableExists) {
        $missing = @()
        if (-not $CycleTableExists) { $missing += "the Radar cycle-run table" }
        if (-not $UserTableExists) { $missing += "the auth user table" }
        return [ordered]@{
            Eligible    = $false
            Decision    = "SKIP_SCHEMA_OR_TABLE_MISSING"
            WriteMarker = $false
            Reason      = "$($missing -join ' and ') does not exist. A missing table is not treated as proof of a fresh database; skipping the automatic initial cycle until this can be confirmed."
        }
    }

    if (-not $CycleRowCountProbeSucceeded -or -not $UserRowCountProbeSucceeded -or $null -eq $CycleRowCount -or $null -eq $UserRowCount) {
        return [ordered]@{
            Eligible    = $false
            Decision    = "SKIP_UNABLE_TO_DETERMINE"
            WriteMarker = $false
            Reason      = "Both tables exist, but their row counts could not be reliably read. Database state is unknown; skipping the automatic initial cycle."
        }
    }

    if ($CycleRowCount -gt 0) {
        return [ordered]@{
            Eligible    = $false
            Decision    = "SKIP_EXISTING_CYCLE_FOUND"
            WriteMarker = $true
            Reason      = "$CycleRowCount existing Radar cycle run row(s) were found (any status). The database is not genuinely fresh."
        }
    }

    if ($UserRowCount -gt 0) {
        return [ordered]@{
            Eligible    = $false
            Decision    = "SKIP_EXISTING_APPLICATION_DATA_FOUND"
            WriteMarker = $true
            Reason      = "$UserRowCount existing user account(s) were found. The database already has application data and is not genuinely fresh."
        }
    }

    return [ordered]@{
        Eligible    = $true
        Decision    = "ELIGIBLE_GENUINELY_FRESH"
        WriteMarker = $false
        Reason      = "Both the Radar cycle-run table and the auth user table exist and are empty. The database is positively confirmed genuinely fresh."
    }
}
