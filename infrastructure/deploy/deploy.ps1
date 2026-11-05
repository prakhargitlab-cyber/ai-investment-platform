param([ValidateSet("plan","verify")][string]$Action="plan")
$ErrorActionPreference="Stop"
$script=Join-Path $PSScriptRoot "release_guard.py"
$spec=Join-Path $PSScriptRoot "releases/auth-reset-pr28.k3d.json"
if ($null -eq (Get-Command python -ErrorAction SilentlyContinue)) { throw "Python 3.10+ required" }
& python $script $Action --spec $spec --mode k3d --node k3d-ai-investment-dev-server-0 --allow-local-http-registry
if ($LASTEXITCODE -ne 0) { throw "Release guard blocked action: $Action" }
