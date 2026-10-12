# Actual local Python invocation + mocked kubectl. No cluster/database mutation.
$ErrorActionPreference='Stop';$script:Passed=0
function Assert($Condition,$Message){if(-not $Condition){throw $Message}}
function Test($Name,[scriptblock]$Body){& $Body;$script:Passed++;Write-Output "PASS $Name"}
function Throws([scriptblock]$Body,$Pattern){try{& $Body}catch{Assert ($_.Exception.Message -like $Pattern) "Wrong error: $($_.Exception.Message)";return};throw 'Expected failure'}
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile((Join-Path (Get-Location) 'platform.ps1'),[ref]$tokens,[ref]$errors)
Assert (-not $errors.Count) 'Parser failure'
foreach($name in @('ConvertTo-DeploymentArgument','Invoke-DeploymentNativeCommand','Invoke-DeploymentCommand','Invoke-DeploymentKubectl','Initialize-DeploymentDiagnostics','Set-DeploymentPhase','Get-DeploymentFailureState','Show-DeploymentFailure')){
 $node=$ast.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name},$true)
 . ([scriptblock]::Create($node.Extent.Text))
}
Test 'native stdout/stderr and exit 2 are independently retained' {
 $r=Invoke-DeploymentNativeCommand python @('-c','import sys;print("observer-output");print("observer-stderr",file=sys.stderr);sys.exit(2)')
 Assert ($r.ExitCode -eq 2 -and $r.StdOut.Trim() -eq 'observer-output' -and $r.StdErr.Trim() -eq 'observer-stderr') 'Lost native streams/code'
}
Test 'PowerShell 5.1 argument roundtrip preserves empty, quotes, slashes and spaces' {
 $values=@('','with spaces','say "hello"','C:\space dir\','plain','single''quote')
 $r=Invoke-DeploymentNativeCommand python (@('-c','import json,sys;print(json.dumps(sys.argv[1:]))')+$values)
 Assert ($r.ExitCode -eq 0) 'Argument subprocess failed'
 $actual=$r.StdOut|ConvertFrom-Json
 Assert ($actual.Count -eq $values.Count) 'Dropped argument'
 for($i=0;$i -lt $values.Count;$i++){Assert ($actual[$i] -ceq $values[$i]) "Argument $i changed"}
}
# From this point all native operations, including diagnostic reads, are mocked.
function kubectl {throw 'Real kubectl forbidden'}
function Write-Host {param([Parameter(ValueFromRemainingArguments=$true)]$Object);$script:Messages+=($Object -join ' ')}
function Reset {
 Initialize-DeploymentDiagnostics @('api-gateway','research-engine','portfolio-service','frontend')
 $script:Messages=@();$script:Calls=@();$script:ExecFails=$false;$script:ReadFails=$false;$script:Isolated=$false;$script:ImageDrift=$false
 foreach($name in $script:DeploymentRun.Names){$script:DeploymentRun.BaselineImages[$name]="old/$name"}
}
function Invoke-DeploymentNativeCommand {
 param($Program,$Arguments)
 $script:Calls+=@{Program=$Program;Arguments=$Arguments}
 if($Arguments -contains 'exec' -and $script:ExecFails){return @{ExitCode=2;StdOut='{"status":"UNKNOWN","reason":"INCOMPLETE_OBSERVATION"}';StdErr='command terminated with exit code 2'}}
 if($Arguments -contains 'get'){
  if($script:ReadFails){return @{ExitCode=1;StdOut='';StdErr='state unavailable'}}
  if($Arguments -contains 'networkpolicies'){
   $items=@();if($script:Isolated){$items+=@{metadata=@{name='research-bootstrap-isolation'}}}
   return @{ExitCode=0;StdOut=(@{items=$items}|ConvertTo-Json -Depth 10);StdErr=''}
  }
  $items=@()
  foreach($name in $script:DeploymentRun.Names){
   $image="old/$name";if($script:ImageDrift -and $name -eq 'frontend'){$image='new/frontend'}
   $items+=@{metadata=@{name=$name;generation=1};spec=@{replicas=1;template=@{spec=@{containers=@(@{name=$name;image=$image;env=@(@{name='AIP_RESEARCH_OPPORTUNITY_RECOVERY_PAUSED';value='false'},@{name='AIP_RESEARCH_OPPORTUNITY_SCHEDULER_ENABLED';value='false'})})}}};status=@{readyReplicas=1;observedGeneration=1}}
  }
  return @{ExitCode=0;StdOut=(@{items=$items}|ConvertTo-Json -Depth 20);StdErr=''}
 }
 return @{ExitCode=0;StdOut='ok';StdErr=''}
}
Test 'injected kubectl exec exit 2 retains observation reason, phase, command, stderr' {
 Reset;$script:ExecFails=$true;Set-DeploymentPhase 'INITIAL_POSTGRES_OBSERVATION'
 Throws {Invoke-DeploymentKubectl @('exec','pod/research-1','-c','research-engine','--','python','-c','raise SystemExit(2)')} '*exit 2*INITIAL_POSTGRES_OBSERVATION*'
 $f=$script:DeploymentRun.Failure
 Assert ($f.StdOut -match 'INCOMPLETE_OBSERVATION' -and $f.StdErr -eq 'command terminated with exit code 2' -and $f.ExitCode -eq 2) 'Lost failure cause'
 Assert ($f.Command -match 'kubectl --context k3d-ai-investment-dev -n ai-investment exec pod/research-1 -c research-engine') 'Failed command missing'
 Assert (($script:Messages -join ' ') -match 'Original stdout:.*INCOMPLETE_OBSERVATION') 'Observation stdout hidden'
}
Test 'failure state is read-only, shows controls and conditional next action' {
 Reset;Set-DeploymentPhase 'INITIAL_POSTGRES_OBSERVATION'
 try {throw 'fixture failure'} catch {Show-DeploymentFailure $_}
 $text=$script:Messages -join ' '
 Assert ($text -match '"ResearchRecovery":"false"' -and $text -match '"Isolation":"ABSENT"' -and $text -match 'Fresh attempt: CONDITIONAL') 'Incomplete state/next action'
 Assert (@($script:Calls | Where-Object {($_.Arguments -join ' ') -match '\b(patch|create|delete|rollout)\b'}).Count -eq 0) 'Diagnostics mutated state'
}
foreach($condition in @('ReadFails','Isolated','ImageDrift')){
 Test "unknown/changed state blocks fresh attempt: $condition" {
  Reset;Set-Variable -Scope Script -Name $condition -Value $true
  try{throw 'fixture'}catch{Show-DeploymentFailure $_}
  Assert (($script:Messages -join ' ') -match 'Fresh attempt: BLOCKED') 'Unsafe retry advice'
 }
}
Test 'partial patch attempt blocks retry even if images look unchanged' {
 Reset;$script:DeploymentRun.PatchAttempts=@('research-engine')
 try{throw 'fixture'}catch{Show-DeploymentFailure $_}
 Assert (($script:Messages -join ' ') -match 'Fresh attempt: BLOCKED') 'Partial rollout retried'
}
Test 'successful build push patch progress is recorded' {
 Reset
 $null=Invoke-DeploymentCommand docker @('build','-t','image:unique','context')
 $null=Invoke-DeploymentCommand docker @('push','image:unique')
 $null=Invoke-DeploymentKubectl @('patch','deployment','frontend','--type=strategic','--patch-file','mock.json')
 Assert ($script:DeploymentRun.Built[0] -eq 'image:unique' -and $script:DeploymentRun.Pushed[0] -eq 'image:unique' -and $script:DeploymentRun.Patched[0] -eq 'frontend') 'Missing progress'
}
Write-Output "RESULT: $script:Passed passed, 0 failed"
