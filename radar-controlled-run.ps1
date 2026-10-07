# Controlled Radar runtime validation runner. Exactly one mutating request is made.
$ErrorActionPreference = "Continue"
$Namespace = "ai-investment"
$Deployment = "research-engine"
$Service = "research-engine"
$LocalPort = 18081
$BaseUrl = "http://127.0.0.1:$LocalPort"
$pf = $null
$cycleId = $null
$correlationId = $null
$podName = $null
$cycleLogs = @()

# How long this script will keep client-side polling a legitimately still-
# running backend cycle before giving up (display/monitoring-only limit --
# never a backend execution timeout). A full deep scan over the whole
# admitted candidate pool can run for hours, so this defaults high; the
# backend's own timeouts/retries/budgets are entirely unaffected by this
# value. Override by setting $env:RADAR_MAX_MONITOR_HOURS before running.
$MaxMonitorHours = if ($env:RADAR_MAX_MONITOR_HOURS) { [double]$env:RADAR_MAX_MONITOR_HOURS } else { 8.0 }
$PollIntervalSeconds = 10
$maxIterations = [int][Math]::Ceiling(([TimeSpan]::FromHours($MaxMonitorHours)).TotalSeconds / $PollIntervalSeconds)

# ---------------------------------------------------------------------------
# Live progress parsing: ONLY fields that genuinely exist today, taken
# verbatim from the structured log lines this backend already emits
# (app/global_scanner.py, app/global_opportunity_orchestration.py). No field
# is invented. Full lifecycle, in emission order for one cycle:
#
#  0. Universe discovery  (app/global_scanner.py GlobalScanner.scan, the
#     initial full-universe call -- progress_label defaults to "scan"):
#       scan_progress: completed=%d/%d failed=0 active=0/1 evidence_batch=%d
#     (logged every 50 candidates and once at completed==total; %2$d is the
#     universe size -- this is where OpportunityRanking.universe_count comes
#     from, so it is a stable value worth keeping on screen afterward too).
#
#  1. Baseline acquisition over the FULL eligible universe
#     (app/global_opportunity_orchestration.py _acquire_baseline_requirements,
#     called once before deep-candidate selection):
#       baseline_acquisition_start: eligible=%d concurrency=%d
#       baseline_acquisition_progress: completed=%d/%d failed=%d active=%d/%d
#       baseline_acquisition_complete: eligible=%d completed=%d failed=%d
#     NOTE: _acquire_baseline_requirements is called a SECOND time later,
#     scoped only to the small admitted/deep-candidate shortlist, to acquire
#     their baseline ensure() results -- it emits the exact same three log
#     lines again, just with a much smaller "eligible" count. That second,
#     small pass must never overwrite the full-universe Baseline total/
#     completed figures, so this script freezes those two fields the moment
#     it sees the Stage-2 admission marker below (whichever comes first).
#
#  2. Stage-2 admission / deep-candidate selection:
#       radar_acquisition_count operation=baseline_candidates_evaluated count=%d
#       radar_acquisition_count operation=acquisition_candidates_admitted count=%d
#       radar_acquisition_count operation=acquisition_candidates_deferred count=%d
#       radar_acquisition_count operation=deep_candidates_selected count=%d
#       stage2_selected correlationId=%s preliminary=%d pool=%d shortlist=%d
#       stage2_start correlationId=%s admitted=%d   (admitted == the deep-pool
#                                                     denominator for phase 3)
#
#  3. Stage-2 deep investigation (one line per candidate as it completes,
#     plus a periodic/cumulative progress line):
#       stage2_candidate_complete instrument_id=%s disposition=%s
#       stage2_deep_progress: completed=%d/%d active=%d/%d   (every 10, and
#                                                               once at the end)
#     Disposition -> bucket, using the SAME mapping production itself uses
#     for checkpoint state (app/cycle_checkpoint.py DISPOSITION_STATE):
#       ready            ANALYZED, RANK_FILTERED
#       readiness_failed DEEP_READINESS_NOT_MET
#       technical        DEEP_ACQUISITION_TIMEOUT, DEEP_SOURCE_UNAVAILABLE,
#                        DEEP_TECHNICAL_FAILURE, RULE_ENGINE_EXCEPTION,
#                        STAGE2_INTERNAL_ERROR, BASELINE_ACQUISITION_FAILED,
#                        PROFILE_MISSING_AFTER_HYDRATION
#       terminal_outcome PROFILE_HYDRATION_FAILED, PROFILE_IDENTITY_MISMATCH,
#                        CANONICAL_INELIGIBLE
#
#  4. Repair:
#       stage2_candidate_repair instrument_id=%s previous=%s failures=%s
#     Recovery is detected exactly the way production counts it
#     (deep_repair_recovered_count): an instrument that appeared in a repair
#     line later completes with a "ready" disposition.
#
#  5. Final reconciliation (one line, absolute/authoritative totals --
#     always preferred over this script's own running counts once seen):
#       stage2_complete correlationId=%s deep_attempted=%d deep_ready=%d
#         deep_readiness_failed=%d deep_acquisition_timeout=%d
#         deep_source_unavailable=%d rule_analyzed=%d rule_exception=%d
#         stage2_internal_error=%d evaluated=%d suppressed=%d eligible=%d
#         technical_failure=%d repair_attempted=%d repair_recovered=%d
#       stage2_disposition_counts correlationId=%s shortlist=%d
#         baseline_incomplete=%d suppressed=%d diagnostics=%d
#
# No lifecycle phase above exposes a numeric percentage via the HTTP status
# API -- only these log lines carry it. Where no authoritative denominator
# has been seen yet for the active phase, the display below falls back to
# an indeterminate Write-Progress (-PercentComplete -1) with phase + elapsed
# + whatever counts are already known, rather than fabricate a number from
# elapsed time or a hard-coded historical candidate count.
# ---------------------------------------------------------------------------

function New-RadarState {
    [PSCustomObject]@{
        UniverseTotal       = $null
        UniverseCompleted   = $null
        BaselineTotal       = $null
        BaselineCompleted   = $null
        BaselineFailed      = $null
        Stage2SelectionSeen = $false
        Stage2Started       = $false
        AdmissionEvaluated  = $null
        AdmissionAdmitted   = $null
        AdmissionDeferred   = $null
        DeepSelected        = $null
        Stage2Shortlist     = $null
        BaselineAcquisitionCompleted   = $null
        BaselineAcquisitionTotal       = $null
        BaselineAcquisitionFailed      = $null
        BaselineAcquisitionActive      = $null
        BaselineAcquisitionConcurrency = $null
        DeepAdmitted        = $null
        DeepTotal           = $null
        DeepCompleted        = $null
        DeepActive          = $null
        DeepConcurrency     = $null
        CompletedIds        = (New-Object 'System.Collections.Generic.HashSet[string]')
        Ready               = 0
        ReadinessFailed     = 0
        TechnicalFailure    = 0
        TerminalOutcome     = 0
        RepairAttempted     = 0
        RepairRecovered     = 0
        Repairing           = (New-Object 'System.Collections.Generic.HashSet[string]')
        Stage2CompleteSeen  = $false
        FinalDeepAttempted  = $null
        FinalReady          = $null
        FinalReadinessFailed = $null
        FinalTechnicalFailure = $null
        FinalRepairAttempted  = $null
        FinalRepairRecovered  = $null
        FinalEligible       = $null
        Stage2DispositionSeen = $false
    }
}

$ReadyDispositions = @("ANALYZED", "RANK_FILTERED")
$ReadinessFailedDispositions = @("DEEP_READINESS_NOT_MET")
$TechnicalFailureDispositions = @("DEEP_ACQUISITION_TIMEOUT", "DEEP_SOURCE_UNAVAILABLE", "DEEP_TECHNICAL_FAILURE",
    "RULE_ENGINE_EXCEPTION", "STAGE2_INTERNAL_ERROR", "BASELINE_ACQUISITION_FAILED", "PROFILE_MISSING_AFTER_HYDRATION")
$TerminalOutcomeDispositions = @("PROFILE_HYDRATION_FAILED", "PROFILE_IDENTITY_MISMATCH", "CANONICAL_INELIGIBLE")

# Mutates $State in place from NEW log lines only (caller is responsible for
# not re-feeding lines already processed -- see the incremental fetch cursor
# in the polling loop below). Every counter here is either (a) overwritten
# from an absolute/cumulative value the log line itself already carries, or
# (b) guarded by an id-based HashSet, so re-feeding an already-seen line a
# second time (e.g. a one-line overlap at a fetch-window boundary) is a
# harmless no-op rather than a double count.
function Update-RadarState {
    param($State, [string[]]$NewLines)

    foreach ($line in $NewLines) {
        if (-not $State.Stage2SelectionSeen) {
            if ($line -match "scan_progress: completed=(\d+)/(\d+)") {
                $State.UniverseCompleted = [int]$Matches[1]
                $State.UniverseTotal = [int]$Matches[2]
            }
            if ($line -match "baseline_acquisition_start: eligible=(\d+) concurrency=\d+") {
                $State.BaselineTotal = [int]$Matches[1]
            }
            if ($line -match "baseline_acquisition_progress: completed=(\d+)/(\d+) failed=(\d+)") {
                $State.BaselineCompleted = [int]$Matches[1]
                $State.BaselineTotal = [int]$Matches[2]
                $State.BaselineFailed = [int]$Matches[3]
            }
            if ($line -match "baseline_acquisition_complete: eligible=(\d+) completed=(\d+) failed=(\d+)") {
                $State.BaselineTotal = [int]$Matches[1]
                $State.BaselineCompleted = [int]$Matches[2]
                $State.BaselineFailed = [int]$Matches[3]
            }
        }

        # Admitted-population baseline re-acquisition pass (runs between
        # admission and stage2_start in FULL mode; can also match the
        # universe-wide first pass harmlessly). Authoritative completed/total
        # pair, parsed regardless of Stage2SelectionSeen -- separate fields
        # from BaselineTotal/BaselineCompleted above, so the freeze guard on
        # those is never bypassed.
        if ($line -match "baseline_acquisition_progress: completed=(\d+)/(\d+) failed=(\d+) active=(\d+)/(\d+)") {
            $State.BaselineAcquisitionCompleted = [int]$Matches[1]
            $State.BaselineAcquisitionTotal = [int]$Matches[2]
            $State.BaselineAcquisitionFailed = [int]$Matches[3]
            $State.BaselineAcquisitionActive = [int]$Matches[4]
            $State.BaselineAcquisitionConcurrency = [int]$Matches[5]
        }

        if ($line -match "radar_acquisition_count operation=baseline_candidates_evaluated count=(\d+)") {
            $State.AdmissionEvaluated = [int]$Matches[1]
            $State.Stage2SelectionSeen = $true
        }
        if ($line -match "radar_acquisition_count operation=acquisition_candidates_admitted count=(\d+)") {
            $State.AdmissionAdmitted = [int]$Matches[1]
            $State.Stage2SelectionSeen = $true
        }
        if ($line -match "radar_acquisition_count operation=acquisition_candidates_deferred count=(\d+)") {
            $State.AdmissionDeferred = [int]$Matches[1]
        }
        if ($line -match "radar_acquisition_count operation=deep_candidates_selected count=(\d+)") {
            $State.DeepSelected = [int]$Matches[1]
            $State.Stage2SelectionSeen = $true
        }
        if ($line -match "stage2_selected correlationId=\S+ preliminary=\d+ pool=\d+ shortlist=(\d+)") {
            $State.Stage2Shortlist = [int]$Matches[1]
            $State.Stage2SelectionSeen = $true
        }

        if ($line -match "stage2_start correlationId=\S+ admitted=(\d+)") {
            $State.DeepAdmitted = [int]$Matches[1]
            $State.Stage2SelectionSeen = $true
            $State.Stage2Started = $true
        }

        if ($line -match "stage2_deep_progress: completed=(\d+)/(\d+) active=(\d+)/(\d+)") {
            $State.DeepCompleted = [int]$Matches[1]
            $State.DeepTotal = [int]$Matches[2]
            $State.DeepActive = [int]$Matches[3]
            $State.DeepConcurrency = [int]$Matches[4]
        }

        if ($line -match "stage2_candidate_repair instrument_id=(\S+)") {
            $State.RepairAttempted++
            [void]$State.Repairing.Add($Matches[1])
        }

        if ($line -match "stage2_candidate_complete instrument_id=(\S+) disposition=(\S+)") {
            $id = $Matches[1]; $disposition = $Matches[2]
            if ($State.CompletedIds.Add($id)) {
                if ($ReadyDispositions -contains $disposition) {
                    $State.Ready++
                    if ($State.Repairing.Contains($id)) {
                        $State.RepairRecovered++
                        [void]$State.Repairing.Remove($id)
                    }
                } elseif ($ReadinessFailedDispositions -contains $disposition) { $State.ReadinessFailed++ }
                elseif ($TechnicalFailureDispositions -contains $disposition) { $State.TechnicalFailure++ }
                elseif ($TerminalOutcomeDispositions -contains $disposition) { $State.TerminalOutcome++ }
            }
        }

        if ($line -match "stage2_complete correlationId=\S+ deep_attempted=(\d+) deep_ready=(\d+) deep_readiness_failed=(\d+) deep_acquisition_timeout=(\d+) deep_source_unavailable=(\d+) rule_analyzed=(\d+) rule_exception=(\d+) stage2_internal_error=(\d+) evaluated=(\d+) suppressed=(\d+) eligible=(\d+) technical_failure=(\d+) repair_attempted=(\d+) repair_recovered=(\d+)") {
            $State.Stage2CompleteSeen = $true
            $State.FinalDeepAttempted = [int]$Matches[1]
            $State.FinalReady = [int]$Matches[2]
            $State.FinalReadinessFailed = [int]$Matches[3]
            $State.FinalTechnicalFailure = [int]$Matches[12]
            $State.FinalRepairAttempted = [int]$Matches[13]
            $State.FinalRepairRecovered = [int]$Matches[14]
            $State.FinalEligible = [int]$Matches[11]
        }

        if ($line -match "stage2_disposition_counts correlationId=") {
            $State.Stage2DispositionSeen = $true
        }
    }
}

function Get-RadarPhase {
    param($State)
    if ($State.Stage2DispositionSeen -or $State.Stage2CompleteSeen) { return "Finalizing" }
    if ($State.Stage2Started -or $State.DeepAdmitted -ne $null -or $State.DeepTotal -ne $null -or $State.CompletedIds.Count -gt 0 -or $State.RepairAttempted -gt 0) { return "Deep research" }
    # Admission counts (admitted/deferred/selected/shortlist) are known from
    # radar_acquisition_count / stage2_selected, but stage2_start (the only
    # line meaning deep investigation has actually begun) has not yet been
    # seen -- show that the admitted population is known and work is being
    # prepared for it, rather than remaining on the generic "admission"
    # label or jumping ahead to "Deep research" with a fabricated 0/N.
    if ($State.DeepSelected -ne $null -or $State.AdmissionAdmitted -ne $null -or $State.Stage2Shortlist -ne $null) { return "Stage 2 preparing" }
    if ($State.Stage2SelectionSeen) { return "Stage 2 admission" }
    if ($State.BaselineTotal -ne $null) { return "Baseline scan" }
    if ($State.UniverseTotal -ne $null -or $State.UniverseCompleted -ne $null) { return "Discovering universe" }
    return "Starting"
}

function Format-RadarStatus {
    param($State, [string]$Phase, [string]$Status, [TimeSpan]$Elapsed, [string]$AnalysisScope = $null)

    $elapsedText = $Elapsed.ToString("hh\:mm\:ss")
    $segments = New-Object 'System.Collections.Generic.List[string]'

    # Finalization has three authoritative sub-states even though
    # Get-RadarPhase only ever returns the single "Finalizing" identifier
    # (kept stable so Write-RadarProgress's percent-calc switch does not need
    # to change): stage2_complete seen (readiness/evidence reconciliation
    # just finished), stage2_disposition_counts additionally seen (ranking
    # input is final), and the cycle's own authoritative $Status reaching a
    # terminal value (ranking/publication itself is complete). No new log
    # signal is invented for this -- all three come from signals already
    # parsed elsewhere in this script.
    $phaseLabel = $Phase
    if ($Phase -eq "Finalizing") {
        if (@("COMPLETED", "FAILED", "CANCELLED") -contains $Status) { $phaseLabel = "Final ranking complete" }
        elseif ($State.Stage2DispositionSeen) { $phaseLabel = "Ranking successfully evaluated candidates" }
        elseif ($State.Stage2CompleteSeen) { $phaseLabel = "Finalizing evidence/readiness" }
    } elseif ($Phase -eq "Stage 2 preparing") {
        $phaseLabel = "Stage 2 Deep Research"
    }
    [void]$segments.Add($phaseLabel)
    if ($AnalysisScope) { [void]$segments.Add("Mode=$AnalysisScope") }

    if ($State.UniverseTotal -ne $null) {
        if ($Phase -eq "Discovering universe") { [void]$segments.Add("Universe $($State.UniverseCompleted)/$($State.UniverseTotal)") }
        else { [void]$segments.Add("Universe $($State.UniverseTotal)") }
    }
    if ($State.BaselineTotal -ne $null -and $Phase -ne "Discovering universe") {
        [void]$segments.Add("Baseline $($State.BaselineCompleted)/$($State.BaselineTotal)")
    }

    if ($Phase -eq "Stage 2 preparing") {
        # Admission is known (radar_acquisition_count / stage2_selected) but
        # stage2_start has not fired yet -- deep investigation has not
        # actually begun. If the admitted-population baseline re-acquisition
        # pass has emitted its own authoritative completed/total pair
        # (baseline_acquisition_progress), show that real progress instead of
        # the static "Preparing N admitted candidates..." placeholder.
        if ($State.AdmissionAdmitted -ne $null) { [void]$segments.Add("Admitted=$($State.AdmissionAdmitted)") }
        if ($State.AdmissionDeferred -ne $null) { [void]$segments.Add("Deferred=$($State.AdmissionDeferred)") }
        if ($State.BaselineAcquisitionTotal -ne $null -and $State.BaselineAcquisitionTotal -gt 0) {
            $baPercent = [Math]::Round(($State.BaselineAcquisitionCompleted / $State.BaselineAcquisitionTotal) * 100, 1)
            [void]$segments.Add("Baseline acquisition: $($State.BaselineAcquisitionCompleted)/$($State.BaselineAcquisitionTotal)")
            [void]$segments.Add("Failed=$($State.BaselineAcquisitionFailed)")
            [void]$segments.Add("Active=$($State.BaselineAcquisitionActive)/$($State.BaselineAcquisitionConcurrency)")
            [void]$segments.Add("$baPercent%")
        } else {
            $admittedForDisplay = if ($State.DeepSelected -ne $null) { $State.DeepSelected }
                                   elseif ($State.AdmissionAdmitted -ne $null) { $State.AdmissionAdmitted }
                                   elseif ($State.Stage2Shortlist -ne $null) { $State.Stage2Shortlist }
                                   else { $null }
            if ($admittedForDisplay -ne $null) { [void]$segments.Add("Preparing $admittedForDisplay admitted candidates...") }
        }
    }

    if ($Phase -eq "Deep research" -or $Phase -eq "Finalizing") {
        # Precedence for the authoritative deep-pool denominator, strongest
        # evidence first. Never request shortlist_limit, top_n, or
        # UniverseTotal -- none of those appear in this chain.
        $deepDenominator = if ($State.DeepTotal -ne $null) { $State.DeepTotal }
                            elseif ($State.DeepAdmitted -ne $null) { $State.DeepAdmitted }
                            elseif ($State.DeepSelected -ne $null) { $State.DeepSelected }
                            elseif ($State.Stage2Shortlist -ne $null) { $State.Stage2Shortlist }
                            else { $null }
        $deepNumerator = if ($State.Stage2CompleteSeen) { $State.FinalDeepAttempted }
                         elseif ($State.DeepCompleted -ne $null) { $State.DeepCompleted }
                         else { $State.CompletedIds.Count }
        if ($deepDenominator -ne $null) { [void]$segments.Add("Deep $deepNumerator/$deepDenominator") }
        elseif ($deepNumerator -gt 0) { [void]$segments.Add("Deep $deepNumerator") }
        if ($Phase -eq "Deep research" -and $State.DeepActive -ne $null) {
            [void]$segments.Add("Active $($State.DeepActive)/$($State.DeepConcurrency)")
        }

        $ready = if ($State.Stage2CompleteSeen) { $State.FinalReady } else { $State.Ready }
        $readinessFailed = if ($State.Stage2CompleteSeen) { $State.FinalReadinessFailed } else { $State.ReadinessFailed }
        $technicalFailure = if ($State.Stage2CompleteSeen) { $State.FinalTechnicalFailure } else { $State.TechnicalFailure }
        $repairAttempted = if ($State.Stage2CompleteSeen) { $State.FinalRepairAttempted } else { $State.RepairAttempted }
        $repairRecovered = if ($State.Stage2CompleteSeen) { $State.FinalRepairRecovered } else { $State.RepairRecovered }
        [void]$segments.Add("Ready=$ready Failed=$readinessFailed Technical=$technicalFailure")
        [void]$segments.Add("Repair $repairAttempted attempted / $repairRecovered recovered")
        if ($State.Stage2CompleteSeen -and $State.FinalEligible -ne $null) { [void]$segments.Add("Eligible=$($State.FinalEligible)") }
    }

    [void]$segments.Add("status=$Status")
    [void]$segments.Add("elapsed=$elapsedText")
    return ($segments -join " | ")
}

function Write-RadarProgress {
    param($State, [string]$Status, [TimeSpan]$Elapsed, [switch]$Completed, [string]$AnalysisScope = $null)

    if ($Completed) {
        Write-Progress -Activity "Radar Full Deep Scan" -Completed
        return
    }

    $phase = Get-RadarPhase -State $State
    $statusLine = Format-RadarStatus -State $State -Phase $phase -Status $Status -Elapsed $Elapsed -AnalysisScope $AnalysisScope

    $percent = $null
    if ($phase -eq "Discovering universe" -and $State.UniverseTotal -gt 0) {
        $percent = [Math]::Min(100, [Math]::Max(0, [Math]::Round(($State.UniverseCompleted / $State.UniverseTotal) * 100)))
    } elseif ($phase -eq "Baseline scan" -and $State.BaselineTotal -gt 0) {
        $percent = [Math]::Min(100, [Math]::Max(0, [Math]::Round(($State.BaselineCompleted / $State.BaselineTotal) * 100)))
    } elseif ($phase -eq "Stage 2 preparing" -and $State.BaselineAcquisitionTotal -gt 0) {
        $percent = [Math]::Min(100, [Math]::Max(0, [Math]::Round(($State.BaselineAcquisitionCompleted / $State.BaselineAcquisitionTotal) * 100)))
    } elseif ($phase -eq "Deep research") {
        # Same authoritative precedence as Format-RadarStatus: stage2_deep_progress
        # denominator > stage2_start admitted > deep_candidates_selected >
        # stage2_selected shortlist. Never request shortlist_limit/top_n/universe.
        $deepDenominator = if ($State.DeepTotal -ne $null) { $State.DeepTotal }
                            elseif ($State.DeepAdmitted -ne $null) { $State.DeepAdmitted }
                            elseif ($State.DeepSelected -ne $null) { $State.DeepSelected }
                            elseif ($State.Stage2Shortlist -ne $null) { $State.Stage2Shortlist }
                            else { $null }
        $deepNumerator = if ($State.DeepCompleted -ne $null) { $State.DeepCompleted } else { $State.CompletedIds.Count }
        if ($deepDenominator -gt 0) {
            $percent = [Math]::Min(100, [Math]::Max(0, [Math]::Round(($deepNumerator / $deepDenominator) * 100)))
        }
    }
    # "Stage 2 admission", "Stage 2 preparing" and "Finalizing" never have a
    # trustworthy single completed/total pair of their own (admission is a
    # one-shot selection step; "preparing" is the window after admission is
    # known but before stage2_start, with no numerator yet; finalization is
    # publish/persist bookkeeping) -- always indeterminate for those, per
    # the no-invented-percentage requirement.

    if ($percent -ne $null) {
        Write-Progress -Activity "Radar Full Deep Scan" -Status $statusLine -PercentComplete $percent
    } else {
        Write-Progress -Activity "Radar Full Deep Scan" -Status $statusLine -PercentComplete -1
    }
}

try {
    Write-Host "Starting temporary port-forward $LocalPort -> svc/${Service}:80"
    $pf = Start-Process -FilePath "kubectl" -ArgumentList @("port-forward", "-n", $Namespace, "svc/$Service", "${LocalPort}:80") -PassThru -WindowStyle Hidden
    Start-Sleep -Seconds 3
    Write-Host "Checking /health before cycle creation"
    try {
        $health = Invoke-WebRequest -Uri "$BaseUrl/health" -UseBasicParsing -TimeoutSec 5
        if ($health.StatusCode -ne 200) { throw "Health returned HTTP $($health.StatusCode)" }
        Write-Host "Health OK (HTTP $($health.StatusCode))"
    } catch { Write-Host "FATAL: health check failed: $($_.Exception.Message)" -ForegroundColor Red; throw }

    # The only authorized mutating operation in this script.
    # analysis_scope=FULL is the explicit, backend-supported request for a
    # genuine full-universe deep scan (see app/global_opportunity_cycle.py /
    # app/main.py). shortlist_limit is retained as a valid API field but is
    # ignored for deep admission once FULL mode is requested -- the backend
    # bypasses the pre-deep shortlist cap entirely in that mode.
    Write-Host "Creating exactly one Radar FULL analysis cycle"
    Write-Host "analysis_scope: FULL"
    Write-Host "top_n: 4"
    Write-Host "shortlist_limit: 25 (ignored for deep admission in FULL mode)"
    $requestParameters = @{ top_n = 4; shortlist_limit = 25; analysis_scope = "FULL" }
    $body = $requestParameters | ConvertTo-Json -Compress
    try { $createResponse = Invoke-WebRequest -Uri "$BaseUrl/api/v1/research/opportunities/cycles" -Method POST -ContentType "application/json" -Body $body -UseBasicParsing -TimeoutSec 15 }
    catch { Write-Host "Cycle creation failed; no retry will be attempted: $($_.Exception.Message)" -ForegroundColor Red; throw }
    $created = $createResponse.Content | ConvertFrom-Json
    # Prefer what the backend actually persisted (created.parameters), fall
    # back to the value this script requested -- display-only, BOUNDED-mode
    # compatible (if this literal is ever BOUNDED, the display follows it).
    $AnalysisScope = if ($created.parameters -and $created.parameters.analysis_scope) { $created.parameters.analysis_scope } else { $requestParameters.analysis_scope }
    $cycleId = $created.cycle_id
    $correlationId = if ($created.correlation_id) { $created.correlation_id } elseif ($created.parameters) { $created.parameters.correlation_id } else { $null }
    $initialStatus = $created.status
    $createdAt = if ($created.created_at) { $created.created_at } else { $created.updated_at }
    Write-Host "cycle_id: $cycleId"
    Write-Host "correlation_id: $correlationId"
    Write-Host "initial_status: $initialStatus"
    Write-Host "created/updated_at: $createdAt"
    if ([string]::IsNullOrWhiteSpace([string]$cycleId)) { throw "Cycle response did not contain cycle_id." }

    $podList = kubectl get pods -n $Namespace -l app.kubernetes.io/component=$Deployment -o json 2>$null | ConvertFrom-Json
    if (-not $podList.items) { $podList = kubectl get pods -n $Namespace -l app=$Deployment -o json 2>$null | ConvertFrom-Json }
    $podName = ($podList.items | Select-Object -First 1).metadata.name
    if ([string]::IsNullOrWhiteSpace([string]$podName)) { throw "Could not resolve a research-engine pod." }
    Write-Host "Monitoring pod: $podName"
    Write-Host "Monitoring budget: $maxIterations iterations x ${PollIntervalSeconds}s (~$MaxMonitorHours hours), client-side only -- backend timeouts/retries/budgets are unaffected."

    # Progress bar begins only now -- after the cycle has been successfully
    # created above.
    $terminalStatuses = @("COMPLETED", "FAILED", "CANCELLED")
    $status = $initialStatus
    $iteration = 0
    $startTime = Get-Date
    $radarState = New-RadarState
    # Incremental log cursor: each poll fetches only logs since the last
    # successfully-processed line's own timestamp (kubectl --timestamps),
    # not the whole cycle history every time. Re-fetching the boundary line
    # once is harmless (Update-RadarState is idempotent -- see above).
    $logCursor = $createdAt
    $lastProcessedRawLine = $null
    $lastAnnouncedPhase = $null
    $lastConsoleSummaryAt = $startTime
    $announcedPreparingBlock = $false

    try {
        while (($iteration -lt $maxIterations) -and ($terminalStatuses -notcontains $status)) {
            Start-Sleep -Seconds $PollIntervalSeconds
            $iteration++
            try {
                $statusResponse = Invoke-WebRequest -Uri "$BaseUrl/api/v1/research/opportunities/cycles/$cycleId/status" -UseBasicParsing -TimeoutSec 10
                $statusObject = $statusResponse.Content | ConvertFrom-Json
                $status = $statusObject.status
            } catch { Write-Host "Status poll failed (continuing): $($_.Exception.Message)" -ForegroundColor Yellow }

            try {
                $rawLines = @(kubectl logs $podName -n $Namespace -c $Deployment --timestamps --since-time=$logCursor 2>$null)
                $startIdx = 0
                if ($lastProcessedRawLine) {
                    $idx = [array]::IndexOf($rawLines, $lastProcessedRawLine)
                    if ($idx -ge 0) { $startIdx = $idx + 1 }
                }
                if ($rawLines.Count -gt $startIdx) {
                    $newRaw = $rawLines[$startIdx..($rawLines.Count - 1)]
                    $lastProcessedRawLine = $newRaw[-1]
                    if ($lastProcessedRawLine -match "^(\S+)\s") { $logCursor = $Matches[1] }
                    $newLines = $newRaw | ForEach-Object { $_ -replace "^\S+\s", "" }
                    Update-RadarState -State $radarState -NewLines $newLines
                }
            } catch { }

            $elapsed = (Get-Date) - $startTime
            Write-RadarProgress -State $radarState -Status $status -Elapsed $elapsed -AnalysisScope $AnalysisScope

            # Preserve a durable (non-repetitive) console trail: one line on
            # a phase change, and one line roughly every 60s, instead of a
            # Write-Host every poll.
            $now = Get-Date
            $currentPhase = Get-RadarPhase -State $radarState
            if (($currentPhase -eq "Stage 2 preparing") -and (-not $announcedPreparingBlock)) {
                $admittedForBlock = if ($radarState.DeepSelected -ne $null) { $radarState.DeepSelected }
                                    elseif ($radarState.AdmissionAdmitted -ne $null) { $radarState.AdmissionAdmitted }
                                    elseif ($radarState.Stage2Shortlist -ne $null) { $radarState.Stage2Shortlist }
                                    else { "?" }
                Write-Host ""
                Write-Host "Mode       : $AnalysisScope"
                Write-Host "Universe   : $($radarState.UniverseTotal)"
                Write-Host ""
                Write-Host "Baseline / Admission"
                Write-Host "Evaluated  : $($radarState.BaselineCompleted) / $($radarState.BaselineTotal)"
                Write-Host "Admitted   : $($radarState.AdmissionAdmitted)"
                Write-Host "Deferred   : $($radarState.AdmissionDeferred)"
                Write-Host ""
                Write-Host "Stage 2 Deep Research"
                Write-Host "Preparing $admittedForBlock admitted candidates..."
                Write-Host ""
                $announcedPreparingBlock = $true
            }
            if (($currentPhase -ne $lastAnnouncedPhase) -or (($now - $lastConsoleSummaryAt).TotalSeconds -ge 60)) {
                Write-Host ("[{0}s elapsed] {1}" -f [int]$elapsed.TotalSeconds, (Format-RadarStatus -State $radarState -Phase $currentPhase -Status $status -Elapsed $elapsed -AnalysisScope $AnalysisScope))
                $lastAnnouncedPhase = $currentPhase
                $lastConsoleSummaryAt = $now
            }
        }
    } finally {
        # Always clear the progress display before moving on, including on
        # Ctrl+C / an unhandled error inside the loop above.
        Write-RadarProgress -State $radarState -Status $status -Elapsed ((Get-Date) - $startTime) -Completed -AnalysisScope $AnalysisScope
    }
    if ($terminalStatuses -notcontains $status) { Write-Host "WARNING: monitoring budget ($maxIterations iterations) exhausted; no additional cycle will be created." -ForegroundColor Yellow }

    Write-Host "Collecting bounded logs since $createdAt"
    $cycleLogs = @(kubectl logs $podName -n $Namespace -c $Deployment --since-time=$createdAt 2>$null)
    $patterns = @("baseline_acquisition_start", "baseline_acquisition_complete", "stage2_start", "stage2_selected", "stage2_deep_progress", "stage2_complete", "stage2_disposition_counts", "stage2_candidate_repair", "opportunity_cycle_complete", "opportunity_cycle_cancel", "structured_financial", "OfficialFinancialProvider", "NseOfficialFinancialProvider", "pdf_extraction", "fetch_extraction_start", "fetch_pdf_signature", "Traceback", "Exception", "ERROR", "CRITICAL")
    $patternRegex = ($patterns | ForEach-Object { [regex]::Escape($_) }) -join "|"
    $relevant = $cycleLogs | Select-String -Pattern $patternRegex
    if ($relevant) { $relevant | ForEach-Object { Write-Host $_.Line } } else { Write-Host "(no matching lines)" }

    Write-Host "`nA. Full-universe scan signal"
    ($cycleLogs | Select-String -Pattern "baseline_acquisition_start") | ForEach-Object { Write-Host $_.Line }
    Write-Host "`nB. Admission boundary"
    foreach ($pattern in @("stage2_start", "stage2_selected", "baseline_acquisition_complete")) { ($cycleLogs | Select-String -Pattern $pattern) | ForEach-Object { Write-Host $_.Line } }
    Write-Host "`nC. Stage-2 progress/completion"
    foreach ($pattern in @("stage2_deep_progress", "stage2_complete", "stage2_disposition_counts", "stage2_candidate_repair")) { ($cycleLogs | Select-String -Pattern $pattern) | ForEach-Object { Write-Host $_.Line } }
    Write-Host "`nD. Financial / PDF / technical signals"
    foreach ($pattern in @("structured_financial", "OfficialFinancialProvider", "NseOfficialFinancialProvider", "pdf_extraction", "fetch_extraction_start", "fetch_pdf_signature", "Traceback", "Exception", "ERROR", "CRITICAL")) { ($cycleLogs | Select-String -Pattern $pattern) | ForEach-Object { Write-Host $_.Line } }
    Write-Host "`nD2. Stage-2 timing report (Area 2 runtime_ensure/single-flight + Area 3 persistence operation counts, e.g. load_market_schedules/load_market_calendar_exceptions -- already logged by cycle_timing.report(), just not previously surfaced by this script)"
    ($cycleLogs | Select-String -Pattern "stage2_timing_report") | ForEach-Object { Write-Host $_.Line }

    $finalStatusObject = $null
    try { $finalStatusResponse = Invoke-WebRequest -Uri "$BaseUrl/api/v1/research/opportunities/cycles/$cycleId/status" -UseBasicParsing -TimeoutSec 10; $finalStatusObject = $finalStatusResponse.Content | ConvertFrom-Json }
    catch { Write-Host "Final status read failed: $($_.Exception.Message)" -ForegroundColor Yellow }
    $podStatus = kubectl get pod $podName -n $Namespace -o json 2>$null | ConvertFrom-Json
    $container = $podStatus.status.containerStatuses | Where-Object { $_.name -eq $Deployment } | Select-Object -First 1
    $termination = $container.lastState.terminated
    Write-Host "`nE. Final cycle status"
    Write-Host "cycle_id: $cycleId"
    Write-Host "correlation_id: $correlationId"
    Write-Host "final_status: $($finalStatusObject.status)"
    Write-Host "restart_count: $($container.restartCount)"
    Write-Host "last_termination_reason: $($termination.reason)"
    Write-Host "last_termination_exit_code: $($termination.exitCode)"
} finally {
    Write-Progress -Activity "Radar Full Deep Scan" -Completed
    if ($pf -and -not $pf.HasExited) { Stop-Process -Id $pf.Id -Force -ErrorAction SilentlyContinue; Write-Host "Temporary port-forward terminated." }
}
