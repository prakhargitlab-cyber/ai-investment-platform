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
 if($Arguments[0] -eq 'logs'){if(@($Arguments|Where-Object {$_ -like '--since-time=*'}).Count){return $script:Logs};return $script:FullLogs}
 if($Arguments[1] -eq 'networkpolicies'){return ($script:Policies | ConvertTo-Json -Depth 20)}
 if($Arguments[1] -eq 'jobs,cronjobs'){return ($script:Jobs | ConvertTo-Json -Depth 20)}
 if($Arguments[1] -eq 'pods'){return ($script:Pods | ConvertTo-Json -Depth 20)}
 if($Arguments[1] -eq 'pod'){return ($script:Pod | ConvertTo-Json -Depth 20)}
 if($Arguments[1] -eq 'deployments,statefulsets'){return ($script:Workloads | ConvertTo-Json -Depth 20)}
 throw 'Unexpected mock boundary'
}
function Reset {
 $script:Calls=@();$script:Tunnels=@();$script:ProbeError=$false;$script:ProbeOutput='HTTP/1.0 200 OK';$script:Logs=@();$script:FullLogs=@()
 $script:Jobs=@{items=@()};$script:Pods=@{items=@(@{metadata=@{namespace='ai-investment'};spec=@{}})}
 $script:Pod=@{metadata=@{uid='uid'};status=@{containerStatuses=@(@{name='research-engine';restartCount=0;containerID='one';ready=$true})}}
 $script:Pod=$script:Pod | ConvertTo-Json -Depth 20 | ConvertFrom-Json
 $script:Gate=@{ResearchPod='research-1';ResearchUID='uid';PodIdentity=($script:Pod.status.containerStatuses|Select-Object name,restartCount,containerID|ConvertTo-Json -Compress);Selector=@{matchLabels=@{component='research-engine'}};Callers=@('gateway','portfolio','mcp');ServiceIP='10.43.0.1';PodIP='10.42.0.1';Baseline=@{fingerprint=('a'*64)};AuditPath='mock';Started='2026-10-12T00:00:00Z';Migrations=$null;Workloads=@{items=@(@{kind='Deployment';metadata=@{name='other'};spec=@{replicas=1}})}}
 $script:Policies=@{items=@(@{metadata=@{name='research-bootstrap-isolation'};spec=@{podSelector=$script:Gate.Selector;policyTypes=@('Ingress');ingress=@()}})}
 $script:Workloads=$script:Gate.Workloads
 $script:Fingerprint='a'*64
}
Test 'PowerShell parser' {Assert ($errors.Count -eq 0) 'Parser failed';Write-Output "PowerShell $($PSVersionTable.PSVersion)"}
Test 'baseline/control health probe' {Reset;Invoke-IsolationProbe caller research-service 80;Assert ($script:Calls.Count -eq 1) 'Probe absent'}
Test 'denial requires explicit refusal and increasing REJECT counter, never timeout' {
 Reset;$script:ActiveIsolationGate=$script:Gate
 $script:Pod.status | Add-Member phase 'Running'
 $script:Pod.status | Add-Member podIP $script:Gate.PodIP
 $script:ProbeCounter=0
 function Get-VerifiedResearchRejectCounter {param($Gate);$script:ProbeCounter++;return $script:ProbeCounter}
 function Invoke-DeploymentNativeCommand {param($Program,$Arguments);if($script:ProbeError){throw 'binary unavailable'};return $script:NativeProbe}
 $script:NativeProbe=@{ExitCode=124;StdOut='';StdErr=''}
 Throws {Invoke-IsolationProbe gateway 10.42.0.1 8000 -Denied} '*REJECT proof failed*exit=124*'
 $script:NativeProbe=@{ExitCode=1;StdOut='';StdErr='bash: /dev/tcp/10.42.0.1/8000: Connection refused'}
 Invoke-IsolationProbe gateway 10.42.0.1 8000 -Denied
 $script:ProbeError=$true;Throws {Invoke-IsolationProbe gateway 10.42.0.1 8000 -Denied} '*binary unavailable*'
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
$script:CycleId='86f0567d-978e-4fcc-a9c5-337ed729db69'
function RecoveryObservation {
 param([hashtable]$Override=@{})
 $r=[ordered]@{cycle_id=$script:CycleId;market='NSE';status='RUNNING';owner_id='worker-old';lease_expires_at='2026-10-29T10:00:00.000000+00:00';resume_count=0;updated_at='2026-10-29T09:59:00.000000+00:00';error_code=$null;lease_expired=$true;lease_expired_seconds=900.5;slot=@(,@('NSE',$script:CycleId));checkpoint_rows=3;checkpoint_fingerprint=('c'*64)}
 foreach($k in $Override.Keys){$r[$k]=$Override[$k]}
 return @{status='RECOVERY_SNAPSHOT';reasons=@();fingerprint=$script:Fingerprint;recovery=$r}
}
function RecoveryGate {$g=$script:Gate.Clone();$g.Recovery=@{cycle_id=$script:CycleId;market='NSE'};return $g}
Test 'recovery request requires explicit paired authorization and strict scope' {
 $Profile=$null;$DryRun=$false;$ResumeAuditTag=$null;$AcceptBootstrapRisk=$true;$ConfirmCallerFreeze=$true
 $RecoverInterruptedCycleId=$null;$AuthorizeInterruptedCycleRecovery=$false
 Assert ($null -eq (Get-InterruptedCycleRecoveryRequest -Names @('research-engine'))) 'Ordinary deployment acquired recovery'
 $RecoverInterruptedCycleId=$script:CycleId
 Throws {Get-InterruptedCycleRecoveryRequest -Names @('research-engine')} '*requires both*'
 $RecoverInterruptedCycleId=$null;$AuthorizeInterruptedCycleRecovery=$true
 Throws {Get-InterruptedCycleRecoveryRequest -Names @('research-engine')} '*requires both*'
 foreach($bad in @($script:CycleId.ToUpper(),($script:CycleId+"'"),'86f0567d978e4fcca9c5337ed729db69','x')){
  $RecoverInterruptedCycleId=$bad;Throws {Get-InterruptedCycleRecoveryRequest -Names @('research-engine')} '*Invalid interrupted-cycle id*'
 }
 $RecoverInterruptedCycleId=$script:CycleId
 Throws {Get-InterruptedCycleRecoveryRequest -Names @('research-engine','api-gateway')} '*research-engine alone*'
 Throws {Get-InterruptedCycleRecoveryRequest -Names @('frontend')} '*research-engine alone*'
 $DryRun=$true;Throws {Get-InterruptedCycleRecoveryRequest -Names @('research-engine')} '*incompatible*';$DryRun=$false
 $ResumeAuditTag='review-20261029-000000-'+('a'*32);Throws {Get-InterruptedCycleRecoveryRequest -Names @('research-engine')} '*incompatible*';$ResumeAuditTag=$null
 $ConfirmCallerFreeze=$false;Throws {Get-InterruptedCycleRecoveryRequest -Names @('research-engine')} '*caller freeze*';$ConfirmCallerFreeze=$true
 $AcceptBootstrapRisk=$false;Throws {Get-InterruptedCycleRecoveryRequest -Names @('research-engine')} '*caller freeze*';$AcceptBootstrapRisk=$true
 $request=Get-InterruptedCycleRecoveryRequest -Names @('research-engine')
 Assert ($request.cycle_id -eq $script:CycleId -and $request.market -eq 'NSE' -and $request.Count -eq 2) 'Recovery request malformed'
}
Test 'ordinary gate still rejects recovery snapshots and blocked durable work' {
 Reset;$ProjectRoot=(Get-Location).Path
 $script:Observation=RecoveryObservation
 Throws {Invoke-DeploymentObservation $script:Gate} '*UNKNOWN/BLOCKED*'
 $script:Observation=@{status='BLOCKED';reasons=@('ACTIVE_SLOT','DURABLE_WORK');fingerprint=$script:Fingerprint}
 Throws {Invoke-DeploymentObservation $script:Gate} '*UNKNOWN/BLOCKED*'
 $source=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String(([regex]::Match($script:Calls[-1],"b64decode\('([^']+)'\)")).Groups[1].Value))
 Assert ($source.TrimEnd().EndsWith("raise SystemExit(main(json.loads('null')))")) 'Ordinary observer invocation changed'
}
Test 'recovery gate passes the authorized cycle to the observer and accepts a stable recovery snapshot' {
 Reset;$ProjectRoot=(Get-Location).Path;$gate=RecoveryGate
 $script:Observation=RecoveryObservation
 $result=Invoke-DeploymentObservation $gate
 Assert ($result.status -eq 'RECOVERY_SNAPSHOT' -and $gate.RecoveryOwner -eq 'worker-old' -and $gate.RecoveryPin) 'Recovery snapshot not pinned'
 $source=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String(([regex]::Match($script:Calls[-1],"b64decode\('([^']+)'\)")).Groups[1].Value))
 Assert ($source.TrimEnd().EndsWith("raise SystemExit(main(json.loads('null'), json.loads('{`"cycle_id`":`"$($script:CycleId)`",`"market`":`"NSE`"}')))")) 'Recovery request not passed to observer'
 $script:Observation=RecoveryObservation @{lease_expired_seconds=1200.0}
 $null=Invoke-DeploymentObservation $gate
 $gate.Recovery.cycle_id='11111111-2222-4333-8444-555555555555'
 Throws {Invoke-DeploymentObservation $gate} '*Recovery invariants*'
 $gate.Recovery.cycle_id="$($script:CycleId)')),__import__('os').system('x"
 Throws {Invoke-DeploymentObservation $gate} '*Invalid interrupted-cycle recovery request*'
 $gate.Recovery.cycle_id=$script:CycleId;$gate.Recovery.market='ETF'
 Throws {Invoke-DeploymentObservation $gate} '*Invalid interrupted-cycle recovery request*'
}
Test 'recovery gate fails closed on blocked, quiet or non-matching observations' {
 Reset;$ProjectRoot=(Get-Location).Path
 foreach($obs in @(
  @{status='QUIET_SNAPSHOT';reasons=@();fingerprint=$script:Fingerprint},
  @{status='BLOCKED';reasons=@('DURABLE_WORK');fingerprint=$script:Fingerprint},
  @{status='BLOCKED';reasons=@('BACKGROUND_REFRESH');fingerprint=$script:Fingerprint},
  @{status='BLOCKED';reasons=@('RECOVERY_LEASE_LIVE');fingerprint=$script:Fingerprint})){
  $gate=RecoveryGate;$script:Observation=$obs;Throws {Invoke-DeploymentObservation $gate} '*UNKNOWN/BLOCKED*'
 }
 $o=RecoveryObservation;$o.reasons=@('LEGACY_WORK');$gate=RecoveryGate;$script:Observation=$o;Throws {Invoke-DeploymentObservation $gate} '*UNKNOWN/BLOCKED*'
 foreach($override in @(@{cycle_id='11111111-2222-4333-8444-555555555555'},@{market='ETF'},@{status='ACCEPTED'},@{owner_id=$null},@{lease_expired=$false},@{lease_expired='true'},@{lease_expires_at=$null},@{checkpoint_fingerprint='bad'})){
  $gate=RecoveryGate;$script:Observation=RecoveryObservation $override;Throws {Invoke-DeploymentObservation $gate} '*Recovery invariants*'
 }
 $gate=RecoveryGate;$script:Observation=@{status='RECOVERY_SNAPSHOT';reasons=@();fingerprint=$script:Fingerprint};Throws {Invoke-DeploymentObservation $gate} '*Recovery invariants*'
}
Test 'recovery pin rejects any change of owner, lease, resume count, update, error, slot or checkpoint' {
 Reset;$ProjectRoot=(Get-Location).Path
 foreach($override in @(@{owner_id='worker-new'},@{lease_expires_at='2026-10-29T10:05:00.000000+00:00'},@{resume_count=1},@{updated_at='2026-10-29T10:01:00.000000+00:00'},@{error_code='OWNERSHIP_LOST'},@{slot=@(,@('NSE','11111111-2222-4333-8444-555555555555'))},@{checkpoint_rows=4},@{checkpoint_fingerprint=('d'*64)})){
  $gate=RecoveryGate;$script:Observation=RecoveryObservation;$null=Invoke-DeploymentObservation $gate
  $script:Observation=RecoveryObservation $override
  Throws {Invoke-DeploymentObservation $gate} '*ownership/lease/checkpoint/slot changed*'
 }
}
Test 'recovery requires old-worker ownership-loss evidence and no later activity' {
 Reset;$gate=RecoveryGate;$gate.RecoveryOwner='worker-old'
 $lost="2026-10-29T10:00:01Z WARNING opportunity_cycle_ownership_lost cycleId=$($script:CycleId) owner=worker-old"
 Throws {Assert-RecoveryWorkerExited $gate} '*exit not evidenced*'
 $script:FullLogs=@("2026-10-29T10:00:01Z WARNING opportunity_cycle_ownership_lost cycleId=$($script:CycleId) owner=worker-other")
 Throws {Assert-RecoveryWorkerExited $gate} '*exit not evidenced*'
 $script:FullLogs=@("2026-10-29T10:00:01Z WARNING opportunity_cycle_ownership_lost cycleId=$($script:CycleId) owner=worker-old-2")
 Throws {Assert-RecoveryWorkerExited $gate} '*exit not evidenced*'
 $script:FullLogs=@('INFO boot',$lost,"2026-10-29T10:00:02Z WARNING opportunity_cycle_status_suppressed cycleId=$($script:CycleId) owner=worker-old reason=SUPERSEDED",'INFO "GET /health HTTP/1.1" 200 OK')
 Assert-RecoveryWorkerExited $gate
 foreach($after in @("INFO opportunity_cycle_claimed cycleId=$($script:CycleId) owner=worker-new resumeCount=1","INFO opportunity_cycle_claimed cycleId=11111111-2222-4333-8444-555555555555 owner=x resumeCount=0","INFO opportunity_cycle_resume_enqueued cycleId=$($script:CycleId) status=RUNNING resumeCount=1","INFO stage2 instrument progress cycleId=$($script:CycleId)",'ERROR Traceback (most recent call last)','INFO opportunity_cycle_takeover_check_failed')){
  $script:FullLogs=@($lost,$after);Throws {Assert-RecoveryWorkerExited $gate} '*activity after ownership loss*'
 }
 $gate.RecoveryOwner=$null;Throws {Assert-RecoveryWorkerExited $gate} '*evidence unavailable*'
}
Test 'recovery worker exit parses timestamp-prefixed JSON logs, array or combined multiline stdout' {
 Reset;$gate=RecoveryGate
 $owner='research-engine:865e05b3-dfb7-4c2a-9dca-6ee9c825a013';$gate.RecoveryOwner=$owner
 $boot='2026-10-29T22:00:00Z {"message":"startup complete"}'
 $lost='2026-10-29T22:28:49Z {"message":"opportunity_cycle_ownership_lost cycleId=' + $script:CycleId + ' owner=' + $owner + '"}'
 $suppressed='2026-10-29T22:28:50Z {"message":"opportunity_cycle_status_suppressed cycleId=' + $script:CycleId + ' owner=' + $owner + ' reason=SUPERSEDED"}'
 $health='2026-10-29T22:29:00Z {"message":"GET /health HTTP/1.1 200"}'
 $script:FullLogs=@($boot,$lost,$suppressed,$health);Assert-RecoveryWorkerExited $gate
 # Real kubectl stdout: one combined string, LF or CRLF separated.
 $script:FullLogs=(@($boot,$lost,$suppressed,$health) -join "`n") + "`n";Assert-RecoveryWorkerExited $gate
 $script:FullLogs=(@($boot,$lost,$suppressed,$health) -join "`r`n");Assert-RecoveryWorkerExited $gate
 # Incorrect owner, owner-prefix collisions and a different cycle are not evidence.
 foreach($wrong in @('research-engine:00000000-dfb7-4c2a-9dca-6ee9c825a013',($owner + '0'),($owner + ':other'),'research-engine:865e05b3')){
  $script:FullLogs=(@($boot,('2026-10-29T22:28:49Z {"message":"opportunity_cycle_ownership_lost cycleId=' + $script:CycleId + ' owner=' + $wrong + '"}')) -join "`n")
  Throws {Assert-RecoveryWorkerExited $gate} '*exit not evidenced*'
 }
 $script:FullLogs=@('2026-10-29T22:28:49Z {"message":"opportunity_cycle_ownership_lost cycleId=11111111-2222-4333-8444-555555555555 owner=' + $owner + '"}')
 Throws {Assert-RecoveryWorkerExited $gate} '*exit not evidenced*'
 # Any later claim, resume, acquisition or unexpected cycle activity rejects, in either input shape.
 foreach($after in @(
  ('2026-10-29T22:30:00Z {"message":"opportunity_cycle_claimed cycleId=' + $script:CycleId + ' owner=research-engine:aaaaaaaa-0000-4000-8000-000000000000 resumeCount=1"}'),
  ('2026-10-29T22:30:00Z {"message":"opportunity_cycle_resume_enqueued cycleId=' + $script:CycleId + ' status=RUNNING resumeCount=0"}'),
  ('2026-10-29T22:30:00Z {"message":"opportunity_cycle_operator_resume_admitted cycleId=' + $script:CycleId + ' owner=x previousOwner=' + $owner + '"}'),
  '2026-10-29T22:30:00Z {"message":"etf_acquisition_started attempt=1"}',
  ('2026-10-29T22:30:00Z {"message":"opportunity_cycle_analysis_scope cycleId=' + $script:CycleId + ' analysisScope=FULL"}'))){
  $script:FullLogs=@($lost,$after);Throws {Assert-RecoveryWorkerExited $gate} '*activity after ownership loss*'
  $script:FullLogs=(@($lost,$after) -join "`n");Throws {Assert-RecoveryWorkerExited $gate} '*activity after ownership loss*'
 }
}
Test 'replacement worker log check parses combined JSON stdout' {
 Reset;$gate=RecoveryGate
 $script:FullLogs=(@('2026-10-30T09:00:00Z {"message":"startup complete"}','2026-10-30T09:00:05Z {"message":"scheduler operation=OPPORTUNITY_CYCLE_SKIP phase=RECOVERY_PAUSED reason=pause_active"}') -join "`n")
 Assert-RecoveryNotResumed $gate
 foreach($line in @(
  ('2026-10-30T09:01:00Z {"message":"opportunity_cycle_claimed cycleId=' + $script:CycleId + ' owner=research-engine:new resumeCount=1"}'),
  '2026-10-30T09:01:00Z {"message":"opportunity_cycle_resume_enqueued cycleId=11111111-2222-4333-8444-555555555555 status=RUNNING resumeCount=0"}',
  ('2026-10-30T09:01:00Z {"message":"opportunity_cycle_owned_elsewhere cycleId=' + $script:CycleId + ' owner=x"}'))){
  $script:FullLogs=(@('2026-10-30T09:00:00Z {"message":"startup complete"}',$line) -join "`r`n")
  Throws {Assert-RecoveryNotResumed $gate} '*touched the interrupted cycle*'
 }
}
Test 'replacement worker must not touch the interrupted cycle' {
 Reset;$gate=RecoveryGate
 $script:FullLogs=@('INFO startup','INFO scheduler operation=OPPORTUNITY_CYCLE_SKIP phase=RECOVERY_PAUSED reason=pause_active');Assert-RecoveryNotResumed $gate
 foreach($line in @("INFO anything cycleId=$($script:CycleId)",'INFO opportunity_cycle_claimed cycleId=x owner=y resumeCount=0','INFO opportunity_cycle_operator_resume_admitted cycleId=x')){
  $script:FullLogs=@($line);Throws {Assert-RecoveryNotResumed $gate} '*touched the interrupted cycle*'
 }
}
function Invoke-DeploymentObservation {param($Gate);return @{status='QUIET_SNAPSHOT';fingerprint=$script:Fingerprint}}
Test 'quiet sample passes without mutations' {Reset;Assert-DeploymentQuietSample $script:Gate;Assert (@($script:Calls|Where-Object {$_ -match '^(patch|create|delete)'}).Count -eq 0) 'Mutation'}
Test 'database fingerprint changes abort' {Reset;$script:Fingerprint='b'*64;Throws {Assert-DeploymentQuietSample $script:Gate} '*Durable state changed*'}
Test 'API traffic aborts' {Reset;$script:Logs=@('INFO "POST /api/v1/etf-radar/cycles HTTP/1.1" 202');Throws {Assert-DeploymentQuietSample $script:Gate} '*traffic*'}
Test 'health access log allowed' {Reset;$script:Logs=@('INFO "GET /health HTTP/1.1" 200 OK');Assert-DeploymentQuietSample $script:Gate}
Test 'recovery quiet sample rechecks old-worker exit evidence every sample' {
 Reset;$gate=RecoveryGate;$gate.RecoveryOwner='worker-old'
 Throws {Assert-DeploymentQuietSample $gate} '*exit not evidenced*'
 $script:FullLogs=@("WARNING opportunity_cycle_ownership_lost cycleId=$($script:CycleId) owner=worker-old")
 Assert-DeploymentQuietSample $gate
 $script:FullLogs+="INFO opportunity_cycle_claimed cycleId=$($script:CycleId) owner=worker-new resumeCount=1"
 Throws {Assert-DeploymentQuietSample $gate} '*activity after ownership loss*'
 Reset;Assert-DeploymentQuietSample $script:Gate;Assert (@($script:Calls|Where-Object {$_ -match '^logs' -and $_ -notmatch 'since-time'}).Count -eq 0) 'Ordinary sample consulted recovery evidence'
}
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
