# ETF Radar controlled runtime runner. Modeled on radar-controlled-run.ps1
# (Equity Radar), but ETF Radar's HTTP surface and lifecycle are simpler:
# no Write-Progress log-scraping state machine exists for it because the
# backend (app/etf_opportunity_cycle.py) does not emit the rich
# stage2_*/baseline_acquisition_* log lines the Equity script parses --
# progress here is read entirely from the durable job-status endpoint
# (GET /api/v1/etf-radar/cycles/{id}/status), which is both simpler and
# more authoritative than scraping logs.
#
# PowerShell 5.1 compatible: no classes, no ??/?.  operators, no ternary,
# no short-circuit assumptions on -and/-or. All control flow uses plain
# if/else. Requires only cmdlets/types available since PowerShell 3.0
# (splatting, ConvertTo-Json/ConvertFrom-Json, [System.Management.Automation.Language.Parser]).
#
# SAFETY (non-negotiable, enforced in code, not just by convention):
#   - Defaults to -DryRun semantics: a mutating cycle is created ONLY when
#     -LiveRun is passed explicitly. There is no parameter combination that
#     creates a cycle without -LiveRun.
#   - Never calls the NSE ETF Preview or Apply endpoints, and never issues
#     any request that could write to instrument_master, provider mappings,
#     or portfolio_positions. This script is scoped to Radar execution only.
#   - Never fabricates or forges authentication headers. The optional
#     eligibility pre-check against portfolio-service's authenticated
#     instrument API (GET /api/v1/instruments) is attempted ONLY when the
#     caller supplies real credentials via -PortfolioAuthHeaders; if absent,
#     the check is skipped with an explicit warning rather than bypassed.
#   - Exactly one mutating request is made per -LiveRun invocation (the
#     single POST to create the cycle) -- there is exactly one call site
#     for that POST in this entire file, and it is never retried.
#   - All cleanup (stopping the port-forward process) happens in a
#     function-local try/finally using `return`, never `exit`, inside the
#     try block. `return` inside a try ALWAYS runs its finally in every
#     PowerShell version; relying on `exit`-inside-try-runs-finally would
#     not be as certain, so this script never does that. The outer, true
#     process exit happens exactly once, after the function has already
#     returned and its finally has already run.
#   - Exits non-zero on any technical failure, analytical failure the
#     caller should know about, or monitoring-budget exhaustion.

[CmdletBinding()]
param(
    [switch]$LiveRun,                      # Must be passed explicitly to create a cycle. Default = dry run.
    [int]$TopN = 10,                       # Validated against the API's own bounds (1-100) before any call.
    [guid[]]$CandidateIds = $null,         # Optional bounded diagnostic override (API max 200). Dry-run still validates this.
    [string]$KubeContext = "k3d-ai-investment-dev",
    [string]$Namespace = "ai-investment",
    [string]$Deployment = "research-engine",
    [string]$Service = "research-engine",
    [int]$LocalPort = 18082,               # Distinct from radar-controlled-run.ps1's 18081 so both can run concurrently.
    [double]$MaxMonitorHours = 2.0,        # ETF Radar cycles are bounded/DB-evidence-only, not a full-universe deep
                                            # scan, so the default monitoring budget is far smaller than Equity's 8h.
    [int]$PollIntervalSeconds = 5,
    [hashtable]$PortfolioAuthHeaders = $null  # Optional real credentials for the read-only eligibility pre-check.
)

function Invoke-EtfRadarRun {
    [CmdletBinding()]
    param()

    $BaseUrl = "http://127.0.0.1:$LocalPort"
    $pf = $null
    $exitCode = 0

    function Invoke-Kubectl {
        param([string[]]$KubectlArgs)
        & kubectl --context $KubeContext @KubectlArgs
    }

    # -----------------------------------------------------------------
    # Parameter validation -- enforced here, not left to the API, so a
    # bad invocation fails fast and visibly, in both dry-run and live mode.
    # -----------------------------------------------------------------
    if ($TopN -lt 1 -or $TopN -gt 100) {
        Write-Host "FATAL: -TopN must be between 1 and 100 (EtfRadarCycleRequest.top_n bounds). Got: $TopN" -ForegroundColor Red
        return 1
    }
    if ($CandidateIds -and $CandidateIds.Count -gt 200) {
        Write-Host "FATAL: -CandidateIds supports at most 200 entries (EtfRadarCycleRequest.candidate_ids max_length). Got: $($CandidateIds.Count)" -ForegroundColor Red
        return 1
    }

    Write-Host "Mode        : $(if ($LiveRun) { 'LIVE -- will create exactly one ETF Radar cycle' } else { 'DRY RUN -- no cycle will be created' })"
    Write-Host "KubeContext : $KubeContext"
    Write-Host "Namespace   : $Namespace"
    Write-Host "top_n       : $TopN"
    Write-Host "candidate_ids: $(if ($CandidateIds) { $CandidateIds -join ',' } else { '(none -- full canonical ETF universe)' })"

    try {
        Write-Host "`nStarting temporary port-forward $LocalPort -> svc/${Service}:80"
        $pf = Start-Process -FilePath "kubectl" -ArgumentList @("--context", $KubeContext, "port-forward", "-n", $Namespace, "svc/$Service", "${LocalPort}:80") -PassThru -WindowStyle Hidden
        Start-Sleep -Seconds 3

        Write-Host "Checking research-engine /health"
        try {
            $health = Invoke-WebRequest -Uri "$BaseUrl/health" -UseBasicParsing -TimeoutSec 5
            if ($health.StatusCode -ne 200) { throw "Health returned HTTP $($health.StatusCode)" }
            Write-Host "Health OK (HTTP $($health.StatusCode))"
        } catch {
            Write-Host "FATAL: research-engine health check failed: $($_.Exception.Message)" -ForegroundColor Red
            return 1
        }

        # -----------------------------------------------------------------
        # Read-only ETF universe eligibility pre-check. Uses the AUTHENTICATED
        # portfolio-service instrument API exactly as production does -- never
        # an unauthenticated or internal-service-identity shortcut, because
        # this script runs as an external operator, not as research-engine's
        # own internal worker. If the caller did not supply real credentials,
        # the check is skipped (not bypassed) with an explicit warning; the
        # rest of this script still has value (it can still create/poll a
        # cycle with an explicit -CandidateIds override, which never queries
        # portfolio-service).
        # -----------------------------------------------------------------
        Write-Host "`nETF universe eligibility pre-check (read-only, authenticated)"
        if ($PortfolioAuthHeaders) {
            $pfPortfolio = $null
            try {
                $pfPortfolio = Start-Process -FilePath "kubectl" -ArgumentList @("--context", $KubeContext, "port-forward", "-n", $Namespace, "svc/portfolio-service", "18090:80") -PassThru -WindowStyle Hidden
                Start-Sleep -Seconds 3
                $uri = "http://127.0.0.1:18090/api/v1/instruments?status=ACTIVE&assetType=ETF&size=1"
                $resp = Invoke-WebRequest -Uri $uri -Headers $PortfolioAuthHeaders -UseBasicParsing -TimeoutSec 10
                $parsed = $resp.Content | ConvertFrom-Json
                $activeEtfCount = $parsed.totalElements
                Write-Host "ACTIVE ETF instruments currently in instrument_master: $activeEtfCount"
                if ($activeEtfCount -eq 0) {
                    Write-Host "WARNING: zero ACTIVE ETF instruments. Without an explicit -CandidateIds override, a live cycle will complete with an empty candidate set (not an error, but not useful). The NSE ETF universe must be populated via the ADMIN-gated Preview/Apply flow first -- this script never calls that flow." -ForegroundColor Yellow
                }
            } catch {
                Write-Host "Eligibility pre-check failed (non-fatal, continuing): $($_.Exception.Message)" -ForegroundColor Yellow
            } finally {
                if ($pfPortfolio -and -not $pfPortfolio.HasExited) { Stop-Process -Id $pfPortfolio.Id -Force -ErrorAction SilentlyContinue }
            }
        } else {
            Write-Host "SKIPPED -- no -PortfolioAuthHeaders supplied. This script will not fabricate authentication to perform this check. Pass a real, already-authenticated header set (e.g. captured from your own session) if you want it verified before running live." -ForegroundColor Yellow
        }

        if (-not $LiveRun) {
            Write-Host "`nDry run complete. No cycle was created. Re-run with -LiveRun to create one (requires separate authorization)."
            return 0
        }

        # -----------------------------------------------------------------
        # The ONE authorized mutating operation in this script.
        # -----------------------------------------------------------------
        Write-Host "`nCreating exactly one ETF Radar cycle (POST /api/v1/etf-radar/cycles)"
        $requestBody = @{ top_n = $TopN }
        if ($CandidateIds) { $requestBody.candidate_ids = @($CandidateIds | ForEach-Object { $_.ToString() }) }
        $bodyJson = $requestBody | ConvertTo-Json -Compress
        $correlationId = [guid]::NewGuid().ToString()

        try {
            $createResponse = Invoke-WebRequest -Uri "$BaseUrl/api/v1/etf-radar/cycles" -Method POST -ContentType "application/json" `
                -Headers @{ "x-correlation-id" = $correlationId } -Body $bodyJson -UseBasicParsing -TimeoutSec 30
        } catch {
            Write-Host "FATAL: cycle creation failed; no retry will be attempted: $($_.Exception.Message)" -ForegroundColor Red
            return 1
        }

        $created = $createResponse.Content | ConvertFrom-Json

        if ($CandidateIds) {
            # The candidate_ids path (EtfRadarCycleRequest.candidate_ids is set)
            # runs synchronously and returns the final persisted payload
            # directly -- there is no separate job to poll. See
            # app/main.py:etf_radar_cycle: when body.candidate_ids is not
            # None, run_etf_radar_cycle executes inline and the response IS
            # the result, not a 202-style "accepted" envelope.
            Write-Host "cycle_id        : $($created.cycle_id)"
            Write-Host "correlation_id  : $($created.correlation_id)"
            Write-Host "radar_version   : $($created.radar_version)"
            Write-Host "as_of           : $($created.as_of)"
            Write-Host "`nRanked results:"
            $created.ranked | ForEach-Object {
                Write-Host ("  rank={0} symbol={1} disposition={2} recommendation={3} reason={4}" -f $_.rank, $_.symbol, $_.disposition, $_.recommendation, $_.reason)
            }
            Write-Host "`nExcluded by reason:"
            $created.excluded_by_reason.PSObject.Properties | ForEach-Object { Write-Host ("  {0} : {1}" -f $_.Name, $_.Value) }
            return 0
        }

        # No candidate_ids override: this went through the durable
        # OpportunityCycleWorker (market='ETF') and returns a 202-style
        # envelope (HTTP 202 is not separately checked here because
        # Invoke-WebRequest only returns on 2xx/catchable-throw on non-2xx;
        # the response body is what carries the cycle_id we need next).
        $cycleId = $created.cycle_id
        if ([string]::IsNullOrWhiteSpace([string]$cycleId)) {
            Write-Host "FATAL: cycle response did not contain cycle_id: $($createResponse.Content)" -ForegroundColor Red
            return 1
        }
        Write-Host "cycle_id       : $cycleId"
        Write-Host "correlation_id : $correlationId"

        $terminalStatuses = @("COMPLETED", "FAILED", "CANCELLED")
        $technicalFailureNote = $null
        $maxIterations = [int][Math]::Ceiling(([TimeSpan]::FromHours($MaxMonitorHours)).TotalSeconds / $PollIntervalSeconds)
        $iteration = 0
        $status = $null
        $startTime = Get-Date

        Write-Host "`nPolling GET /api/v1/etf-radar/cycles/$cycleId/status every ${PollIntervalSeconds}s (budget: $maxIterations iterations / ~$MaxMonitorHours h, client-side only)"
        while (($iteration -lt $maxIterations) -and ($terminalStatuses -notcontains $status)) {
            Start-Sleep -Seconds $PollIntervalSeconds
            $iteration++
            try {
                $statusResponse = Invoke-WebRequest -Uri "$BaseUrl/api/v1/etf-radar/cycles/$cycleId/status" -UseBasicParsing -TimeoutSec 10
                $statusObject = $statusResponse.Content | ConvertFrom-Json
                $status = $statusObject.status
                $elapsed = (Get-Date) - $startTime
                Write-Host ("[{0}s] status={1}" -f [int]$elapsed.TotalSeconds, $status)
                if ($statusObject.error_code) { $technicalFailureNote = $statusObject.error_code }
            } catch {
                Write-Host "Status poll failed (continuing): $($_.Exception.Message)" -ForegroundColor Yellow
            }
        }

        if ($terminalStatuses -notcontains $status) {
            Write-Host "TIMEOUT: monitoring budget ($maxIterations iterations / ~$MaxMonitorHours h) exhausted before a terminal status was reached. The backend cycle is unaffected by this client-side give-up; check status later with cycle_id=$cycleId." -ForegroundColor Red
            $exitCode = 2
        } elseif ($status -eq "FAILED") {
            Write-Host "TECHNICAL FAILURE: cycle reported status=FAILED. error_code=$technicalFailureNote" -ForegroundColor Red
            $exitCode = 3
        } elseif ($status -eq "CANCELLED") {
            Write-Host "CANCELLED: cycle reported status=CANCELLED." -ForegroundColor Yellow
            $exitCode = 4
        } else {
            Write-Host "COMPLETED. Fetching persisted result (GET /api/v1/etf-radar/cycles/$cycleId)"
            try {
                $resultResponse = Invoke-WebRequest -Uri "$BaseUrl/api/v1/etf-radar/cycles/$cycleId" -UseBasicParsing -TimeoutSec 10
                $result = $resultResponse.Content | ConvertFrom-Json
                Write-Host "radar_version : $($result.radar_version)"
                Write-Host "as_of         : $($result.as_of)"
                Write-Host "`nRanked results:"
                if ($result.ranked -and $result.ranked.Count -gt 0) {
                    $result.ranked | ForEach-Object {
                        Write-Host ("  rank={0} symbol={1} disposition={2} recommendation={3} reason={4}" -f $_.rank, $_.symbol, $_.disposition, $_.recommendation, $_.reason)
                    }
                } else {
                    Write-Host "  (empty -- likely zero eligible ACTIVE ETF instruments; see the eligibility pre-check above)"
                }
                Write-Host "`nExcluded by reason:"
                if ($result.excluded_by_reason) { $result.excluded_by_reason.PSObject.Properties | ForEach-Object { Write-Host ("  {0} : {1}" -f $_.Name, $_.Value) } }
            } catch {
                Write-Host "Failed to fetch persisted result: $($_.Exception.Message)" -ForegroundColor Yellow
                $exitCode = 5
            }
        }

        # Bounded diagnostic log tail -- never the full cycle history, just
        # enough to see an Exception/ERROR/Traceback if one occurred. ETF
        # Radar does not emit the structured stage2_*/baseline_* progress
        # lines Equity does, so no analogous progress log-scrape is attempted.
        try {
            $podList = Invoke-Kubectl @("get", "pods", "-n", $Namespace, "-l", "app.kubernetes.io/component=$Deployment", "-o", "json") | ConvertFrom-Json
            if (-not $podList.items) { $podList = Invoke-Kubectl @("get", "pods", "-n", $Namespace, "-l", "app=$Deployment", "-o", "json") | ConvertFrom-Json }
            $podName = ($podList.items | Select-Object -First 1).metadata.name
            if ($podName) {
                Write-Host "`nDiagnostic log tail (pod=$podName, filtered for errors / etf_radar)"
                $logs = Invoke-Kubectl @("logs", $podName, "-n", $Namespace, "-c", $Deployment, "--tail=500")
                $relevant = $logs | Select-String -Pattern "etf_radar|etf-radar|Exception|Traceback|ERROR|CRITICAL|$cycleId"
                if ($relevant) { $relevant | ForEach-Object { Write-Host $_.Line } } else { Write-Host "(no matching lines in last 500)" }
            }
        } catch { Write-Host "Log collection skipped: $($_.Exception.Message)" -ForegroundColor Yellow }

        return $exitCode

    } catch {
        Write-Host "UNHANDLED ERROR: $($_.Exception.Message)" -ForegroundColor Red
        return 1
    } finally {
        # This finally is inside the function, and every path above uses
        # `return`, never `exit` -- so this cleanup is GUARANTEED to run
        # before the function hands control back, on every code path,
        # including the unhandled-error catch above. No reliance on
        # exit-inside-try-runs-finally semantics anywhere in this script.
        if ($pf -and -not $pf.HasExited) {
            Stop-Process -Id $pf.Id -Force -ErrorAction SilentlyContinue
            Write-Host "`nTemporary port-forward terminated."
        }
    }
}

$result = Invoke-EtfRadarRun
exit $result
