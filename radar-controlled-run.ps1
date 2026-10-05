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
    Write-Host "Creating exactly one Radar cycle (top_n=4, shortlist_limit=25)"
    $body = @{ top_n = 4; shortlist_limit = 25 } | ConvertTo-Json -Compress
    try { $createResponse = Invoke-WebRequest -Uri "$BaseUrl/api/v1/research/opportunities/cycles" -Method POST -ContentType "application/json" -Body $body -UseBasicParsing -TimeoutSec 15 }
    catch { Write-Host "Cycle creation failed; no retry will be attempted: $($_.Exception.Message)" -ForegroundColor Red; throw }
    $created = $createResponse.Content | ConvertFrom-Json
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

    $terminalStatuses = @("COMPLETED", "FAILED", "CANCELLED")
    $status = $initialStatus
    $maxIterations = 180
    $iteration = 0
    while (($iteration -lt $maxIterations) -and ($terminalStatuses -notcontains $status)) {
        Start-Sleep -Seconds 10
        $iteration++
        try {
            $statusResponse = Invoke-WebRequest -Uri "$BaseUrl/api/v1/research/opportunities/cycles/$cycleId/status" -UseBasicParsing -TimeoutSec 10
            $statusObject = $statusResponse.Content | ConvertFrom-Json
            $status = $statusObject.status
            Write-Host ("[{0}/180] status={1}" -f $iteration, $status)
        } catch { Write-Host "Status poll failed (continuing): $($_.Exception.Message)" -ForegroundColor Yellow }
    }
    if ($terminalStatuses -notcontains $status) { Write-Host "WARNING: monitoring exhausted 180 iterations; no additional cycle will be created." -ForegroundColor Yellow }

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
    if ($pf -and -not $pf.HasExited) { Stop-Process -Id $pf.Id -Force -ErrorAction SilentlyContinue; Write-Host "Temporary port-forward terminated." }
}
