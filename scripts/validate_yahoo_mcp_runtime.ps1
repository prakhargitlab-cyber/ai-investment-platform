[CmdletBinding()]
param(
    [string]$YahooMcpBaseUrl = "http://127.0.0.1:18002",
    [string]$McpGatewayBaseUrl = "http://127.0.0.1:18001",
    [string]$ResearchBaseUrl = "http://127.0.0.1:18000",
    [string]$GlobalInstrumentId = "",
    [ValidateSet("LATEST_PRICE", "HISTORICAL_PRICE_SERIES", "VALUATION_INPUTS", "BUSINESS_QUALITY_FACTS", "GROWTH_FACTS", "BALANCE_SHEET_FACTS", "QUARTERLY_FINANCIALS", "CURRENT_NEWS", "SHAREHOLDING", "SECTOR_MACRO")]
    [string]$Requirement = "LATEST_PRICE",
    [string]$UserId = "",
    [string]$UserIssuer = "aip-local-runtime-acceptance",
    [switch]$Ensure
)

$ErrorActionPreference = "Stop"

function Assert-LoopbackUrl([string]$Name, [string]$Value) {
    $uri = [Uri]$Value
    if ($uri.Scheme -notin @("http", "https") -or $uri.Host -notin @("127.0.0.1", "localhost")) {
        throw "$Name must target a loopback URL reached through the manually prepared LOCAL runtime."
    }
}

Assert-LoopbackUrl "YahooMcpBaseUrl" $YahooMcpBaseUrl
Assert-LoopbackUrl "McpGatewayBaseUrl" $McpGatewayBaseUrl
Assert-LoopbackUrl "ResearchBaseUrl" $ResearchBaseUrl

$yahooHealth = Invoke-RestMethod -Method Get -Uri "$($YahooMcpBaseUrl.TrimEnd('/'))/health"
$yahooReady = Invoke-RestMethod -Method Get -Uri "$($YahooMcpBaseUrl.TrimEnd('/'))/health/ready"
$gatewayHealth = Invoke-RestMethod -Method Get -Uri "$($McpGatewayBaseUrl.TrimEnd('/'))/health"

[pscustomobject]@{
    yahooHealth = $yahooHealth.status
    yahooReady = $yahooReady.status
    yahooRegisteredTools = $yahooHealth.registeredTools
    gatewayHealth = $gatewayHealth.status
} | Format-List

$workspace = Split-Path -Parent $PSScriptRoot
$python = Join-Path $workspace "ai/yahoo-finance-mcp/.venv/Scripts/python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "Create ai/yahoo-finance-mcp/.venv using the documented local setup before capability discovery."
}
& $python (Join-Path $PSScriptRoot "list_yahoo_mcp_tools.py") --url "$($YahooMcpBaseUrl.TrimEnd('/'))/mcp"
if ($LASTEXITCODE -ne 0) { throw "Yahoo MCP capability discovery failed." }

if (-not $GlobalInstrumentId) {
    Write-Host "Health and capability discovery passed. Supply -GlobalInstrumentId and -UserId for readiness validation."
    exit 0
}
if (-not $UserId) {
    throw "UserId is required for the user-scoped readiness endpoint."
}

$headers = @{
    "X-AIP-User-Id" = $UserId
    "X-AIP-User-Issuer" = $UserIssuer
    "X-AIP-User-Subject" = $UserId
    "X-Correlation-ID" = "yahoo-mcp-runtime-$([Guid]::NewGuid())"
}
$readinessUri = "$($ResearchBaseUrl.TrimEnd('/'))/api/v1/research/readiness/$GlobalInstrumentId"
$before = Invoke-RestMethod -Method Get -Uri $readinessUri -Headers $headers
$beforeRequirement = $before.requirements | Where-Object { $_.requirementId -eq $Requirement } | Select-Object -First 1
Write-Host "Before: requirement=$Requirement status=$($beforeRequirement.status)"

if ($Ensure) {
    $body = @{ requirements = @($Requirement) } | ConvertTo-Json -Compress
    $after = Invoke-RestMethod -Method Post -Uri "$readinessUri/ensure" -Headers $headers -ContentType "application/json" -Body $body
    $afterRequirement = $after.requirements | Where-Object { $_.requirementId -eq $Requirement } | Select-Object -First 1
    $executed = @($after.ensure.executedCapabilities) -join ","
    Write-Host "After: requirement=$Requirement status=$($afterRequirement.status) providerPath=$executed"
}
