# Bounded, explicitly approved ETF verification. Defaults to a no-request dry run.
# Uses authenticated Gateway routes; never direct research identity headers.
param(
    [switch]$LiveRun,
    [switch]$PreflightOnly,
    [guid[]]$CandidateIds,
    [Nullable[int]]$TopN = $null,
    [string]$BaseUrl = 'http://localhost:18080',
    [Alias('AuthHeaders')][hashtable]$PortfolioAuthHeaders = $null,
    [double]$MaxMonitorHours = 2.0,
    [int]$PollIntervalSeconds = 5
)

function Invoke-EtfRadarRun {
    if ($LiveRun -and $PreflightOnly) { Write-Host 'Choose preflight-only or approved live execution.'; return 1 }
    if ($null -ne $TopN -or -not $CandidateIds -or $CandidateIds.Count -gt 5 -or
        @($CandidateIds | Select-Object -Unique).Count -ne $CandidateIds.Count -or
        $CandidateIds -contains [guid]::Empty) {
        Write-Host 'Supply 1-5 distinct canonical ETF CandidateIds; TopN must remain unset.'; return 1
    }
    if ($MaxMonitorHours -le 0 -or $MaxMonitorHours -gt 168 -or $PollIntervalSeconds -lt 1) {
        Write-Host 'Invalid monitoring bounds.'; return 1
    }
    $body = @{ instrument_ids=@($CandidateIds | ForEach-Object { $_.ToString() }) }
    if (-not $LiveRun -and -not $PreflightOnly) {
        Write-Host ('DRY RUN: bounded canonical ETF preflight, then one approved controlled cycle: ' + ($body | ConvertTo-Json -Compress))
        return 0
    }
    $uri = $null
    if (-not [uri]::TryCreate($BaseUrl, [UriKind]::Absolute, [ref]$uri) -or
        $uri.UserInfo -or $uri.Query -or $uri.Fragment -or
        ($uri.Scheme -ne 'https' -and -not ($uri.Scheme -eq 'http' -and $uri.IsLoopback))) {
        Write-Host 'Use a trusted HTTPS Gateway or loopback HTTP URL.'; return 1
    }
    if (-not $PortfolioAuthHeaders -or $PortfolioAuthHeaders.Count -ne 1 -or
        [string]$PortfolioAuthHeaders['Authorization'] -notmatch '^Bearer \S+$') {
        Write-Host 'Supply only the authenticated ADMIN Authorization bearer header; never identity headers.'; return 1
    }
    $headers = @{ Authorization=$PortfolioAuthHeaders['Authorization'] }
    $base = $BaseUrl.TrimEnd('/')
    $query = (@($CandidateIds | ForEach-Object { 'instrument_ids=' + $_.ToString() }) -join '&')
    try {
        $response = Invoke-WebRequest -Uri "$base/api/v1/etf-radar/preflight?$query" -Headers $headers -UseBasicParsing -TimeoutSec 30 -MaximumRedirection 0
        if ($response.StatusCode -ne 200) { throw 'Unexpected preflight status' }
        $preflight = $response.Content | ConvertFrom-Json
        $actual = @($preflight.instruments | ForEach-Object { [string]$_.instrument_id } | Sort-Object)
        $expected = @($body.instrument_ids | Sort-Object)
        if ($preflight.scope -ne 'BOUNDED_CANONICAL_ETF' -or $preflight.candidate_count -ne $CandidateIds.Count -or
            ($actual -join ',') -ne ($expected -join ',')) { throw 'Preflight scope mismatch' }
        Write-Host "Preflight: status=$($preflight.status), ready=$($preflight.ready_count)/$($preflight.candidate_count), recovery_paused=$($preflight.recovery_paused)"
        foreach ($item in $preflight.instruments) {
            Write-Host "ETF $($item.instrument_id): mandatory_ready=$($item.mandatory_evidence_ready), reasons=$($item.blocking_reasons -join ',')"
        }
        if ($PreflightOnly) { if ($preflight.status -eq 'READY') { return 0 }; return 6 }
        if ($preflight.status -ne 'READY' -or $preflight.can_submit -isnot [bool] -or -not $preflight.can_submit) {
            Write-Host 'BLOCKED: missing prerequisites, recovery pause or an active ETF cycle. No cycle submitted.'; return 6
        }
        # Exactly one mutating request; never retry a timeout or ambiguous response.
        $response = Invoke-WebRequest -Uri "$base/api/v1/etf-radar/controlled-cycles" -Method Post -Headers $headers `
            -ContentType 'application/json' -Body ($body | ConvertTo-Json -Compress) -UseBasicParsing -TimeoutSec 30 -MaximumRedirection 0
        if ($response.StatusCode -ne 202) { throw 'Unexpected admission response' }
        $created = $response.Content | ConvertFrom-Json
        $cycleId = [guid]::Empty
        if (-not [guid]::TryParse([string]$created.cycle_id, [ref]$cycleId) -or $cycleId -eq [guid]::Empty) { throw 'Missing cycle identity' }
        Write-Host "cycle_id=$cycleId"
        if ($created.coalesced -or $created.paused) { Write-Host 'Another cycle won admission; no retry or cancellation.'; return 6 }
        $deadline = (Get-Date).AddHours($MaxMonitorHours)
        do {
            $response = Invoke-WebRequest -Uri "$base/api/v1/etf-radar/cycles/$cycleId/status" -Headers $headers -UseBasicParsing -TimeoutSec 10 -MaximumRedirection 0
            if ($response.StatusCode -ne 200) { throw 'Status unavailable' }
            $status = $response.Content | ConvertFrom-Json
            if ([string]$status.cycle_id -ne $cycleId.ToString()) { throw 'Status identity mismatch' }
            Write-Host "status=$($status.status)"
            if ($status.status -eq 'FAILED') { return 3 }
            if ($status.status -eq 'CANCELLED') { return 4 }
            if ($status.status -eq 'COMPLETED') {
                $response = Invoke-WebRequest -Uri "$base/api/v1/etf-radar/cycles/$cycleId" -Headers $headers -UseBasicParsing -TimeoutSec 10 -MaximumRedirection 0
                if ($response.StatusCode -ne 200) { throw 'Result unavailable' }
                $result = $response.Content | ConvertFrom-Json
                if ([string]$result.cycle_id -ne $cycleId.ToString()) { throw 'Result identity mismatch' }
                Write-Host "Persisted terminal result: candidates=$(@($result.candidates).Count), ranked=$(@($result.ranked).Count)"
                Write-Host ($result.excluded_by_reason | ConvertTo-Json -Compress)
                return 0
            }
            if ($status.status -notin @('ACCEPTED','RUNNING','PUBLISHED','CANCEL_REQUESTED')) { throw 'Unknown lifecycle status' }
            Start-Sleep -Seconds $PollIntervalSeconds
        } while ((Get-Date) -lt $deadline)
        Write-Host "Monitor timeout; cycle $cycleId may still be running. No retry or cancellation."; return 2
    } catch {
        # Do not print exceptions that may include bearer headers or response bodies.
        Write-Host 'Request or validation failed. No start request will be retried; inspect the returned cycle ID if available.'
        return 1
    }
}

$result = Invoke-EtfRadarRun
exit $result
