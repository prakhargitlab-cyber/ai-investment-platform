<#
 Read-only pre-deployment verification for the ETF Radar branch. Runs on the Windows host.
 Does NOT touch databases, Kubernetes, or start any ETF cycle. Usage (repo root):
   powershell -ExecutionPolicy Bypass -File scripts\verification\etf-radar-predeploy-checks.ps1 [-SkipJava]
#>
param([switch]$SkipJava)
$ErrorActionPreference = 'Continue'
$root = (Resolve-Path "$PSScriptRoot\..\..").Path
$py = Join-Path $root 'ai\research-engine\.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { $py = 'py' ; $pyArgs = @('-3.12') } else { $pyArgs = @() }
& $py @pyArgs --version
Push-Location (Join-Path $root 'ai\research-engine')
Write-Host '== ETF/SBI/identity targeted suites (Python 3.12) =='
& $py @pyArgs -m pytest tests -q -k "etf or sbi_ter or identity or amfi" -p no:cacheprovider
$targeted = $LASTEXITCODE
Write-Host '== Full backend suite (compare failures with the baseline list in the report) =='
& $py @pyArgs -m pytest tests -q -p no:cacheprovider
$full = $LASTEXITCODE
Pop-Location
Push-Location (Join-Path $root 'frontend')
Write-Host '== Frontend tests =='
npm test
$fe = $LASTEXITCODE
Pop-Location
$gw = 'skipped'
if (-not $SkipJava) {
  Push-Location $root
  Write-Host '== Java gateway tests =='
  mvn -pl services/api-gateway -am test
  $gw = $LASTEXITCODE
  Pop-Location
}
Write-Host "RESULT targeted=$targeted full=$full frontend=$fe gateway=$gw (0 = pass; the full suite has known baseline failures)"
