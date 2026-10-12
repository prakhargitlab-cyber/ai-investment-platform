$ErrorActionPreference='Stop'
$tokens=$null; $errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile((Join-Path (Get-Location) 'etf-radar-controlled-run.ps1'),[ref]$tokens,[ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
$node=$ast.Find({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Invoke-EtfRadarRun'},$true)
. ([scriptblock]::Create($node.Extent.Text))
function Invoke-WebRequest {
 param($Uri,$Method,$Headers,$Body,$ContentType,$TimeoutSec,$MaximumRedirection,[switch]$UseBasicParsing)
 $script:Calls+=@{Uri=$Uri;Method=$Method}
 if ($Uri -match '/preflight\?') {
  $value=@{scope='BOUNDED_CANONICAL_ETF';candidate_count=1;status='READY';ready_count=1;can_submit=$true;instruments=@(@{instrument_id=$CandidateIds[0].ToString()})}
  if ($script:Case -eq 'blocked') { $value.status='BLOCKED' }
  if ($script:Case -eq 'paused') { $value.can_submit=$false }
  if ($script:Case -eq 'scope') { $value.scope='WRONG' }
 } elseif ($Method -eq 'Post') {
  if ($script:Case -eq 'post_failure') { throw 'ambiguous timeout' }
  $value=@{cycle_id=$script:Cycle;coalesced=($script:Case -eq 'coalesced')}
  return @{StatusCode=202;Content=($value | ConvertTo-Json -Depth 10)}
 } elseif ($Uri -match '/status$') {
  $state=switch ($script:Case) { failed {'FAILED'} cancelled {'CANCELLED'} timeout {'RUNNING'} default {'COMPLETED'} }
  $value=@{cycle_id=$script:Cycle;status=$state}
 } else { $value=@{cycle_id=$script:Cycle;candidates=@();ranked=@();excluded_by_reason=@{}} }
 return @{StatusCode=200;Content=($value | ConvertTo-Json -Depth 10)}
}
function Start-Sleep { param($Seconds) }
function Get-Date { $script:Ticks++; return ([datetime]'2026-10-12T12:00:00Z').AddSeconds($script:Ticks*10) }
$script:Cycle='00000000-0000-0000-0000-000000000099'
$passed=0
foreach ($caseName in @('dry','preview','blocked','paused','scope','completed','failed','cancelled','timeout','post_failure','coalesced','auth','duplicates','limit')) {
 $script:Case=$caseName; $script:Calls=@(); $script:Ticks=0
 $CandidateIds=@([guid]'00000000-0000-0000-0000-000000000001')
 $LiveRun=$caseName -ne 'dry' -and $caseName -ne 'preview'; $PreflightOnly=$caseName -eq 'preview'
 $TopN=$null; $BaseUrl='http://localhost:18080'; $PortfolioAuthHeaders=@{Authorization='Bearer test-only'}
 $MaxMonitorHours=0.001; $PollIntervalSeconds=1
 if ($caseName -eq 'auth') { $PortfolioAuthHeaders=@{'X-AIP-User-Roles'='ADMIN'} }
 if ($caseName -eq 'duplicates') { $CandidateIds=@($CandidateIds[0],$CandidateIds[0]) }
 if ($caseName -eq 'limit') { $CandidateIds=@(1..6 | ForEach-Object { [guid]::NewGuid() }) }
 $expected=switch ($caseName) { blocked {6} paused {6} scope {1} failed {3} cancelled {4} timeout {2} post_failure {1} coalesced {6} auth {1} duplicates {1} limit {1} default {0} }
 $actual=Invoke-EtfRadarRun
 if ($actual -ne $expected) { throw "$caseName expected $expected got $actual" }
 $posts=@($script:Calls | Where-Object {$_.Method -eq 'Post'}).Count
 $expectedPosts=if ($caseName -in @('completed','failed','cancelled','timeout','post_failure','coalesced')) {1} else {0}
 if ($posts -ne $expectedPosts) { throw "$caseName incorrect mutation count $posts" }
 if ($caseName -in @('dry','auth','duplicates','limit') -and $script:Calls.Count) { throw 'Unexpected request' }
 $passed++; Write-Output "PASS $caseName"
}
Write-Output "$passed passed, 0 failed"
