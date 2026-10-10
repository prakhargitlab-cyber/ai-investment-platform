function Resolve-OpportunityRadarBootstrapEligibility {
    param(
        [bool]$FreshProvisioningClaimed,
        [bool]$FirstAdminCreatedThisStartup,
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

    if ($UserRowCount -gt 0 -and -not ($UserRowCount -eq 1 -and $FreshProvisioningClaimed -and $FirstAdminCreatedThisStartup)) {
        return [ordered]@{
            Eligible    = $false
            Decision    = "SKIP_EXISTING_APPLICATION_DATA_FOUND"
            WriteMarker = $true
            Reason      = "$UserRowCount existing user account(s) were found. The database already has application data and is not genuinely fresh."
        }
    }

    if (-not $FreshProvisioningClaimed -or $CycleRowCount -lt 0 -or $UserRowCount -lt 0) {
        return [ordered]@{
            Eligible = $false
            Decision = "SKIP_NO_FRESH_PROVISIONING_PROOF"
            WriteMarker = $false
            Reason = "No newly initialized database provisioning token was claimed, or counts were invalid."
        }
    }

    return [ordered]@{
        Eligible    = $true
        Decision    = "ELIGIBLE_GENUINELY_FRESH"
        WriteMarker = $false
        Reason      = "A new provisioning token was claimed; the cycle table is empty and users are empty or contain only the first admin just created by this startup."
    }
}

Export-ModuleMember -Function Resolve-OpportunityRadarBootstrapEligibility
