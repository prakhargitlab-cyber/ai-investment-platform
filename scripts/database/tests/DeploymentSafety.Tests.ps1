# No external commands execute. PowerShell 5.1, repository root.
$ErrorActionPreference='Stop'
$script:Passed=0
# Functions are loaded through the AST; initialize the production script default.
$QuietPeriodSeconds=600
function Assert($condition,$message){if(-not $condition){throw $message}}
function Test($name,[scriptblock]$body){ & $body; $script:Passed++; Write-Output "PASS $name" }
function Throws([scriptblock]$body,$pattern){try{& $body}catch{Assert ($_.Exception.Message -like $pattern) "Unexpected failure: $($_.Exception.Message)";return};throw 'Expected failure'}
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile((Join-Path (Get-Location) 'platform.ps1'),[ref]$tokens,[ref]$errors)
Assert (-not $errors.Count) 'PowerShell parse errors'
foreach($node in $ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst]},$false)){. ([scriptblock]::Create($node.Extent.Text))}
function kubectl {throw 'Unmocked Kubernetes forbidden'}
function docker {throw 'Unmocked Docker forbidden'}
function Invoke-DeploymentCommand {throw 'Unmocked command forbidden'}
function Get-CimInstance {return $script:Tunnels}
function Add-Content {param($LiteralPath,$Encoding,[Parameter(ValueFromPipeline=$true)]$Value)}
function Invoke-DeploymentKubectl {
 param($Arguments)
 $script:Calls+=,($Arguments -join ' ')
 if($Arguments[0] -eq 'exec'){if($script:ProbeError){throw 'binary unavailable'}; if($Arguments -contains 'python'){return ($script:Observation | ConvertTo-Json -Depth 10)}; return $script:ProbeOutput}
 if($Arguments[0] -eq 'logs'){return $script:Logs}
 if($Arguments[1] -eq 'networkpolicies'){return ($script:Policies | ConvertTo-Json -Depth 20)}
 if($Arguments[1] -eq 'jobs,cronjobs'){return ($script:Jobs | ConvertTo-Json -Depth 20)}
 if($Arguments[1] -eq 'pods'){return ($script:Pods | ConvertTo-Json -Depth 20)}
 if($Arguments[1] -eq 'pod'){return ($script:Pod | ConvertTo-Json -Depth 20)}
 if($Arguments[1] -eq 'deployments,statefulsets'){return ($script:Workloads | ConvertTo-Json -Depth 20)}
 throw 'Unexpected mock boundary'
}
function Reset {
 $script:Calls=@();$script:Tunnels=@();$script:ProbeError=$false;$script:ProbeOutput='HTTP/1.0 200 OK';$script:Logs=@()
 $script:Jobs=@{items=@()};$script:Pods=@{items=@(@{metadata=@{namespace='ai-investment'};spec=@{}})}
 $script:Pod=@{metadata=@{uid='uid'};status=@{containerStatuses=@(@{name='research-engine';restartCount=0;containerID='one';ready=$true})}}
 $script:Pod=$script:Pod | ConvertTo-Json -Depth 20 | ConvertFrom-Json
 $script:Gate=@{ResearchPod='research-1';ResearchUID='uid';PodIdentity=($script:Pod.status.containerStatuses|Select-Object name,restartCount,containerID|ConvertTo-Json -Compress);Selector=@{matchLabels=@{component='research-engine'}};Callers=@('gateway','portfolio','mcp');ServiceIP='10.43.0.1';PodIP='10.42.0.1';Baseline=@{fingerprint=('a'*64)};AuditPath='mock';Started='2026-10-12T00:00:00Z';Workloads=@{items=@(@{kind='Deployment';metadata=@{name='other'};spec=@{replicas=1}})}}
 $script:Policies=@{items=@(@{metadata=@{name='research-bootstrap-isolation'};spec=@{podSelector=$script:Gate.Selector;policyTypes=@('Ingress');ingress=@()}})}
 $script:Workloads=$script:Gate.Workloads
 $script:Fingerprint='a'*64
}
Test 'PowerShell parser' {Assert ($errors.Count -eq 0) 'Parser failed';Write-Output "PowerShell $($PSVersionTable.PSVersion)"}
Test 'baseline/control health probe' {Reset;Invoke-IsolationProbe caller research-service 80;Assert ($script:Calls.Count -eq 1) 'Probe absent'}
Test 'denial requires timeout, not HTTP/refused/missing binary' {
 Reset;Throws {Invoke-IsolationProbe caller 10.42.0.1 8000 -Denied} '*isolation failed*'
 $script:ProbeOutput='ISOLATED';Invoke-IsolationProbe caller 10.42.0.1 8000 -Denied
 $script:ProbeError=$true;Throws {Invoke-IsolationProbe caller 10.42.0.1 8000 -Denied} '*binary unavailable*'
}
Test 'control timeout rejected' {Reset;$script:ProbeOutput='ISOLATED';Throws {Invoke-IsolationProbe caller research-service 80} '*unavailable*'}
Test 'invalid probe address rejected before command' {Reset;Throws {Invoke-IsolationProbe caller 'host;bad' 80} '*Invalid*';Assert ($script:Calls.Count -eq 0) 'Command escaped'}
Test 'cross namespace caller blocked' {Reset;$script:Pods.items[0].metadata.namespace='unknown';Throws {Get-DeploymentInventory} '*cross-namespace*'}
Test 'host network caller blocked' {Reset;$script:Pods.items[0].spec.hostNetwork=$true;Throws {Get-DeploymentInventory} '*Host-network*'}
Test 'unfrozen cronjob blocked' {Reset;$script:Jobs.items=@(@{kind='CronJob';spec=@{suspend=$false}});Throws {Get-DeploymentInventory} '*not frozen*'}
Test 'active job blocked' {Reset;$script:Jobs.items=@(@{kind='Job';status=@{active=1}});Throws {Get-DeploymentInventory} '*not frozen*'}
Test 'API server tunnels blocked' {Reset;$script:Tunnels=@(@{CommandLine='kubectl port-forward service/research-engine 8000:80'});Throws {Get-DeploymentInventory} '*tunnels*'}
# Only replace probe I/O after testing its fail-closed behavior.
function Invoke-IsolationProbe {param($Pod,$HostName,$Port,[switch]$Denied);$script:Probes+=@{pod=$Pod;denied=$Denied.IsPresent}}
Test 'all caller service and pod paths plus reachable controls' {Reset;$script:Probes=@();Assert-DeploymentIsolation $script:Gate -Probe;Assert ($script:Probes.Count -eq 9) 'Incomplete probe coverage';Assert (@($script:Probes|Where-Object denied).Count -eq 6) 'Incomplete denial coverage'}
Test 'additive policy abort' {Reset;$script:Policies.items+=@{metadata=@{name='allow'}};Throws {Assert-DeploymentIsolation $script:Gate} '*additive*'}
Test 'policy allow rule drift abort' {Reset;$script:Policies.items[0].spec.ingress=@(@{});Throws {Assert-DeploymentIsolation $script:Gate} '*drift*'}
Test 'workload drift detected before rollout' {Reset;$script:Workloads=@{items=@()};Throws {Assert-DeploymentWorkloads $script:Gate @()} '*set changed*'}
Test 'incomplete refresh blocks baseline and quiet samples despite unchanged fingerprint' {
 Reset;$ProjectRoot=(Get-Location).Path
 $script:Observation=@{status='BLOCKED';reasons=@('BACKGROUND_REFRESH');fingerprint=$script:Fingerprint}
 Throws {Invoke-DeploymentObservation $script:Gate} '*UNKNOWN/BLOCKED*'
 # Stable pod, empty logs and identical fingerprint must not admit a silent worker.
 Throws {Assert-DeploymentQuietSample $script:Gate} '*UNKNOWN/BLOCKED*'
}
Test 'all blocked observations rejected including missing or empty reasons' {
 Reset;$ProjectRoot=(Get-Location).Path
 foreach($reasons in @(@('BACKGROUND_REFRESH','DURABLE_WORK'),@('DURABLE_WORK'),@(),$null)) {
  $script:Observation=@{status='BLOCKED';reasons=$reasons;fingerprint=$script:Fingerprint}
  Throws {Invoke-DeploymentObservation $script:Gate} '*UNKNOWN/BLOCKED*'
 }
 $script:Observation=@{status='BLOCKED';fingerprint=$script:Fingerprint}
 Throws {Invoke-DeploymentObservation $script:Gate} '*UNKNOWN/BLOCKED*'
}
Test 'quiet snapshot accepted with valid fingerprint' {
 Reset;$ProjectRoot=(Get-Location).Path
 $script:Observation=@{status='QUIET_SNAPSHOT';reasons=@();fingerprint=$script:Fingerprint}
 $result=Invoke-DeploymentObservation $script:Gate
 Assert ($result.status -eq 'QUIET_SNAPSHOT') 'Quiet observation rejected'
}
Test 'unknown status and invalid fingerprint rejected' {
 Reset;$ProjectRoot=(Get-Location).Path
 foreach($status in @('UNKNOWN','UNRECOGNIZED',$null)) {
  $script:Observation=@{status=$status;fingerprint=$script:Fingerprint}
  Throws {Invoke-DeploymentObservation $script:Gate} '*UNKNOWN*'
 }
 $script:Observation=@{status='QUIET_SNAPSHOT';fingerprint='invalid'}
 Throws {Invoke-DeploymentObservation $script:Gate} '*missing fingerprint*'
}
function Invoke-DeploymentObservation {param($Gate);return @{status='QUIET_SNAPSHOT';fingerprint=$script:Fingerprint}}
Test 'quiet sample passes without mutations' {Reset;Assert-DeploymentQuietSample $script:Gate;Assert (@($script:Calls|Where-Object {$_ -match '^(patch|create|delete)'}).Count -eq 0) 'Mutation'}
Test 'database fingerprint changes abort' {Reset;$script:Fingerprint='b'*64;Throws {Assert-DeploymentQuietSample $script:Gate} '*Durable state changed*'}
Test 'API traffic aborts' {Reset;$script:Logs=@('INFO "POST /api/v1/etf-radar/cycles HTTP/1.1" 202');Throws {Assert-DeploymentQuietSample $script:Gate} '*traffic*'}
Test 'health access log allowed' {Reset;$script:Logs=@('INFO "GET /health HTTP/1.1" 200 OK');Assert-DeploymentQuietSample $script:Gate}
Test 'pod restart aborts' {Reset;$script:Pod.status.containerStatuses[0].restartCount=1;Throws {Assert-DeploymentQuietSample $script:Gate} '*pod changed*'}
Test 'verified OCI child accepted but foreign digest blocked' {
 $script:VerifiedDeploymentDigests=@{'expected'=@(('sha256:'+('a'*64)),('sha256:'+('b'*64)))}
 Assert-VerifiedPodImage expected ('containerd://sha256:'+('b'*64))
 Throws {Assert-VerifiedPodImage expected ('sha256:'+('c'*64))} '*verified OCI*'
}
function Get-DeploymentMonotonicSeconds {$x=$script:Times[0];$script:Times=@($script:Times|Select-Object -Skip 1);return $x}
function Start-Sleep {param($Seconds);Assert ($Seconds -eq 30) 'Unexpected cadence'}
function Assert-DeploymentQuietSample {param($Gate);$script:Samples++;if($script:SampleFails){throw 'sample failed'}}
Test 'quiet window cannot end before 600 seconds' {$script:Times=@(0)+@(0..20 | ForEach-Object {$_*30});$script:Samples=0;$script:SampleFails=$false;Wait-DeploymentQuiet @{};Assert ($script:Samples -eq 21) 'Window shortened'}
Test 'quiet window bounded and failure aborts' {$script:Times=@(0,661);$script:SampleFails=$false;Throws {Wait-DeploymentQuiet @{}} '*bounded window*';$script:Times=@(0);$script:SampleFails=$true;Throws {Wait-DeploymentQuiet @{}} '*sample failed*'}
Write-Output "RESULT: $script:Passed passed, 0 failed"
