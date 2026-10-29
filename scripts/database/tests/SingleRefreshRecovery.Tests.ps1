# Standalone PowerShell 5.1 tests. AST-load functions only; no workflow entry point,
# real kubectl/Docker/WSL/psql, approved records or live mutations can execute.
$ErrorActionPreference = 'Stop'
$script:Passed = 0
function Assert($Condition, $Message) { if (-not $Condition) { throw $Message } }
function Test($Name, [scriptblock]$Body) { & $Body; $script:Passed++; Write-Output "PASS $Name" }
function Throws([scriptblock]$Body, [string]$Pattern) {
    try { & $Body } catch { Assert ($_.Exception.Message -like $Pattern) "Unexpected error: $($_.Exception.Message)"; return }
    throw "Expected rejection: $Pattern"
}
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../..'))
$sourcePath = Join-Path $root 'scripts/database/maintenance/Invoke-SingleRefreshRecovery.ps1'
$tokens=$null; $errors=$null
$ast = [Management.Automation.Language.Parser]::ParseFile($sourcePath, [ref]$tokens, [ref]$errors)
Assert (-not $errors.Count) ($errors | Out-String)
foreach ($node in $ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst]}, $false)) { . ([scriptblock]::Create($node.Extent.Text)) }
function kubectl { throw 'Real kubectl forbidden' }
function docker { throw 'Real Docker forbidden' }
function wsl.exe { throw 'Real WSL forbidden' }
function psql { throw 'Real psql forbidden' }
function Write-Host { param($Object) }
function Read-Host { param($Prompt); if (-not $script:Answers.Count) { return '' }; $script:Answers.Dequeue() }
$evidence = Join-Path $root 'diagnostics/maintenance-20261018-022509'
$fixture = [pscustomobject]@{
    C=(Get-Content -LiteralPath (Join-Path $evidence 'phase-c-approval.prepared.json') -Raw | ConvertFrom-Json)
    Certificate=(Get-Content -LiteralPath (Join-Path $evidence 'termination-evidence.json') -Raw | ConvertFrom-Json)
    Checks=(Get-Content -LiteralPath (Join-Path $evidence 'phase-a-final-checks-20261018-014344.json') -Raw | ConvertFrom-Json)
    Hashes=@{'termination-evidence.json'='a764f89eab4af72578719fa3285a9d7fdf4563647d28aa1be82fa48ab77f0b59'}
    Evidence=[IO.Path]::Combine([IO.Path]::GetTempPath(), 'single-refresh-rejection-' + [guid]::NewGuid().ToString('N'))
}
function Answers([string[]]$Values) { $script:Answers = [Collections.Generic.Queue[string]]::new(); foreach ($value in $Values) { $script:Answers.Enqueue($value) } }
function Approved-Answers { Answers @('Fixture Reviewer','APPROVE_PHASE_A_TERMINATION_CERTIFICATE','APPROVED_SINGLE_REFRESH_TERMINATION_AND_RECONCILIATION','CONFIRM_UNBROKEN_MAINTENANCE_FREEZE') }

Test 'production script parses' { Assert ($errors.Count -eq 0) 'Parse failure' }
Test 'missing execution switch rejects before inputs or external commands' { Throws { Invoke-SingleRefreshRecovery -Root 'unused' } '*Execution switch missing*' }
Test 'blank identity rejected' { Answers @(''); Throws { Read-RecoveryConsent $fixture } '*real named*' }
Test 'placeholder identity rejected' { Answers @('<real name>'); Throws { Read-RecoveryConsent $fixture } '*real named*' }
Test 'Phase A denial rejected' { Answers @('Fixture Reviewer','NO'); Throws { Read-RecoveryConsent $fixture } '*Phase A approval missing*' }
Test 'Phase C independently required' { Answers @('Fixture Reviewer','APPROVE_PHASE_A_TERMINATION_CERTIFICATE','NO'); Throws { Read-RecoveryConsent $fixture } '*Phase C authorization missing*' }
Test 'unbroken maintenance freeze required' { Answers @('Fixture Reviewer','APPROVE_PHASE_A_TERMINATION_CERTIFICATE','APPROVED_SINGLE_REFRESH_TERMINATION_AND_RECONCILIATION','NO'); Throws { Read-RecoveryConsent $fixture } '*Maintenance freeze not confirmed*' }
Test 'consent capture is in memory with separate UTC decision times' {
    Approved-Answers; $consent = Read-RecoveryConsent $fixture
    Assert ($consent.Name -ceq 'Fixture Reviewer' -and $consent.PhaseAApprovedAtUtc.EndsWith('Z') -and $consent.PhaseCApprovedAtUtc.EndsWith('Z')) 'Consent/UTC capture failed'
    Assert (-not (Test-Path -LiteralPath $fixture.Evidence)) 'Consent wrote an approval'
}
function Get-FileHash { param($LiteralPath,$Algorithm); [pscustomobject]@{Hash=('b'*64)} }
Test 'changed evidence hash rejected' { Throws { Assert-RecoveryHash 'unused' ('a'*64) } '*Approved hash mismatch*' }
Test 'malformed expected hash rejected' { Throws { Assert-RecoveryHash 'unused' 'invalid' } '*Approved hash mismatch*' }
Test 'package tampering rejects before prompts or external commands' { Throws { Read-RecoveryPackage $root } '*Approved hash mismatch*' }

Test 'all reviewed handoff exec commands preserve native separator and psql arguments' {
    # Load the actual package compilation path, mocking only hash I/O. Execute
    # only its K function and individual exec ASTs against a fake native boundary.
    function Assert-RecoveryHash { param($Path,$Expected) }
    function kubectl { $script:ForwardedHandoffArguments=@($args); $global:LASTEXITCODE=0 }
    $package=Read-RecoveryPackage $root
    $kNode=$package.Blocks[0].Ast.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -ceq 'K'},$true)
    . ([scriptblock]::Create($kNode.Extent.Text))
    $Approval=$fixture.C
    $RemoteDir='/tmp/single-refresh-ea48b845-20261018-022509'
    $commands=0; $psqlCommands=0
    foreach ($block in $package.Blocks) {
        foreach ($command in $block.Ast.FindAll({param($n) $n -is [Management.Automation.Language.CommandAst] -and $n.GetCommandName() -ceq 'K' -and $n.CommandElements[1].Value -ceq 'exec'},$true)) {
            $null=& ([scriptblock]::Create($command.Extent.Text))
            $forwarded=$script:ForwardedHandoffArguments
            Assert (($forwarded[0..8] -join '|') -ceq '--context|k3d-ai-investment-dev|-n|ai-investment|exec|postgres-0|-c|postgres|--') 'PowerShell removed the native exec separator'
            if ($forwarded[9] -ceq 'psql') {
                Assert ($forwarded[10] -ceq '-X' -and $forwarded[11] -ceq '-w') 'psql switches escaped the remote command boundary'
                if ($psqlCommands -eq 0) { Assert ($forwarded[-1] -ceq 'BEGIN READ ONLY; SELECT current_database(),session_user,current_user,current_setting(''server_version''),inet_server_addr(); ROLLBACK;') 'SQL ceased to be one unchanged argument' }
                else { Assert ($forwarded -ccontains ('\cd ' + $RemoteDir)) 'psql working-directory argument was split' }
                $psqlCommands++
            }
            $commands++
        }
    }
    Assert ($commands -eq 5 -and $psqlCommands -eq 2) 'Unexpected reviewed handoff command coverage'
}

function Reset-Runtime {
    $script:PodInventory = @{items=@(@{metadata=@{name='research-service';namespace='ai-investment'};status=@{podIP='10.42.2.49'};spec=@{containers=@(@{image='research-service'})}})}
    $script:Deployment = @{metadata=@{uid=$fixture.Certificate.recreation_prevention.deployment.uid;resourceVersion='85373';generation=15};spec=@{replicas=0;selector=$fixture.Certificate.recreation_prevention.isolation.spec.podSelector};status=@{replicas=0;readyReplicas=0;observedGeneration=15}}
    $script:ReplicaSets = @{items=@($fixture.Certificate.recreation_prevention.replica_sets | ForEach-Object { @{metadata=@{uid=$_.uid;ownerReferences=@(@{uid=$script:Deployment.metadata.uid})};spec=@{replicas=0};status=@{replicas=0}} })}
    $script:Policy = @{metadata=@{uid=$fixture.Certificate.recreation_prevention.isolation.uid;resourceVersion='85264'}}
    $script:Postgres = @{metadata=@{uid=$fixture.C.execution_connection.pod_uid};status=@{containerStatuses=@(@{name='postgres';containerID=$fixture.C.execution_connection.container_id;ready=$true;state=@{running=@{startedAt='fixture'}}})}}
    $script:Hpa=@{items=@()}; $script:Processes=@(); $script:Wsl=@('docker-desktop')
    $script:DockerRows=@($fixture.Checks.windows_and_docker.dockerInventory)
    $script:Linux=@('WORKER|/usr/local/bin/python3.12|' + $fixture.Checks.linux_process_attribution[0].cgroup.Substring(7))
    $script:UnmappedTask=$false; $script:IsolationDrift=$false
    $script:Database=@{database='investment';role='investment';session_user='investment';version='16.15';encoding='UTF8';address=$null;clients=@('10.42.2.49')}
}
function Get-DeploymentInventory { $script:PodInventory }
function Assert-DeploymentIsolation { param($Gate); if ($script:IsolationDrift) { throw 'Isolation policy drift' } }
function Get-CimInstance { param($ClassName); $script:Processes }
function Read-RecoveryDatabase { param($Select); $script:Database }
function Invoke-DeploymentKubectl {
    param($Arguments)
    $value = switch ($Arguments[1]) {
        'deployment' { $script:Deployment }
        'replicasets' { $script:ReplicaSets }
        'hpa' { $script:Hpa }
        'networkpolicy' { $script:Policy }
        'pod' { $script:Postgres }
        default { throw 'Unmocked Kubernetes call forbidden' }
    }
    $value | ConvertTo-Json -Depth 15 -Compress
}
function Invoke-RecoveryNative {
    param($Command,$Arguments)
    $script:NativeCalls++
    if ($Command -eq 'wsl.exe') { if ($Arguments[0] -eq '--list') { return $script:Wsl }; return $script:Linux }
    if ($Command -ne 'docker') { throw 'Unmocked native command forbidden' }
    if ($Arguments[0] -eq 'ps') { return @($script:DockerRows | ForEach-Object { $_ | ConvertTo-Json -Compress }) }
    if ($Arguments[0] -eq 'inspect') { return (@{State=@{Running=$false;Pid=0;Status='created'};HostConfig=@{RestartPolicy=@{Name='no'}}} | ConvertTo-Json -Depth 5 -Compress) }
    $node = @($fixture.Checks.node_runtimes | Where-Object node -eq $Arguments[1])[0]
    if ($Arguments[2] -eq 'crictl' -and $Arguments[3] -eq 'ps') {
        return (@{containers=@($node.runningContainerIdentities | ForEach-Object { @{id=$_.id;metadata=@{name=$_.name};image=@{image=$_.image};state='CONTAINER_RUNNING'} })} | ConvertTo-Json -Depth 7 -Compress)
    }
    if ($Arguments[2] -eq 'crictl' -and $Arguments[3] -eq 'pods') { return '{"items":[]}' }
    if ($Arguments[2] -eq 'ctr') { if ($script:UnmappedTask) { return @('TASK PID STATUS',(('f'*64)+' 123 RUNNING')) }; return @('TASK PID STATUS')+@($node.runningContainerIdentities | ForEach-Object { $_.id + ' 123 RUNNING' }) }
    throw 'Unmocked native call forbidden'
}
Test 'unchanged runtime guard passes using mocks only' { Reset-Runtime; Assert-RecoveryRuntime $fixture }
Test 'active Engine executor rejected' { Reset-Runtime; $script:PodInventory.items[0].metadata.name='research-engine-unexpected'; Throws { Assert-RecoveryRuntime $fixture } '*executor present*' }
Test 'nonzero replicas rejected' { Reset-Runtime; $script:Deployment.spec.replicas=1; Throws { Assert-RecoveryRuntime $fixture } '*deployment drift*' }
Test 'deployment resource version drift rejected' { Reset-Runtime; $script:Deployment.metadata.resourceVersion='new'; Throws { Assert-RecoveryRuntime $fixture } '*deployment drift*' }
Test 'isolation drift rejected' { Reset-Runtime; $script:IsolationDrift=$true; Throws { Assert-RecoveryRuntime $fixture } '*Isolation policy drift*' }
Test 'Postgres container identity drift rejected' { Reset-Runtime; $script:Postgres.status.containerStatuses[0].containerID='other'; Throws { Assert-RecoveryRuntime $fixture } '*container identity drift*' }
Test 'operator API tunnel rejected' { Reset-Runtime; $script:Processes=@(@{Name='kubectl.exe';CommandLine='kubectl port-forward svc/api-gateway 18082:80'}); Throws { Assert-RecoveryRuntime $fixture } '*Windows executor/tunnel*' }
Test 'new WSL execution location rejected' { Reset-Runtime; $script:Wsl+= 'Ubuntu'; Throws { Assert-RecoveryRuntime $fixture } '*WSL execution scope drift*' }
Test 'additional Docker executor rejected' { Reset-Runtime; $script:DockerRows+=@{ID='new';Image='research-engine';State='running'}; Throws { Assert-RecoveryRuntime $fixture } '*Docker executor inventory drift*' }
Test 'unmapped containerd task rejected' { Reset-Runtime; $script:UnmappedTask=$true; Throws { Assert-RecoveryRuntime $fixture } '*Unattributed containerd task*' }
Test 'unattributed Linux worker rejected' { Reset-Runtime; $script:Linux=@('WORKER|/usr/bin/python|0::/unknown'); Throws { Assert-RecoveryRuntime $fixture } '*Unattributed Linux*' }
Test 'Linux API tunnel rejected' { Reset-Runtime; $script:Linux=@('TUNNEL'); Throws { Assert-RecoveryRuntime $fixture } '*Unattributed Linux*' }
Test 'database execution identity drift rejected' { Reset-Runtime; $script:Database.role='other'; Throws { Assert-RecoveryRuntime $fixture } '*Database execution identity drift*' }
Test 'unattributed database session rejected' { Reset-Runtime; $script:Database.clients=@('127.0.0.1'); Throws { Assert-RecoveryRuntime $fixture } '*Unattributed database client*' }
Test 'missing commit receipt is UNKNOWN' { Throws { Assert-RecoveryReceipt 'BEFORE | row' } '*UNKNOWN*' }
Test 'out-of-order receipt is UNKNOWN' { Throws { Assert-RecoveryReceipt "COMMIT`nBEFORE | row`nAFTER_PENDING_COMMIT | row`nCOMMITTED:" } '*UNKNOWN*' }
Test 'complete ordered receipt accepted without database calls' { Assert-RecoveryReceipt "BEFORE | row`nAFTER_PENDING_COMMIT | row`nCOMMIT`nCOMMITTED:" }
function Reset-Readback {
    $data=[ordered]@{status='RUNNING';completed_at=$null;updated_at='old';safe_error_code=$null;safe_error_message=$null}
    1..12 | ForEach-Object { $data["preserved_$_"]=$_ }
    $script:Before=[pscustomobject]$data
    $script:After=$script:Before | ConvertTo-Json | ConvertFrom-Json
    $script:After.status='FAILED'; $script:After.completed_at='now'; $script:After.updated_at='now'
    $script:After.safe_error_code='ADMIN_WORKER_TERMINATION_VERIFIED'
    $script:After.safe_error_message='Administrative reconciliation after verified worker termination; original failure time and cause unknown. Evidence: fixture'
    $script:Readback=@{row=$script:After;incomplete_count=0}
}
Test 'readback checks exactly five changed fields in memory' { Reset-Readback; Assert-RecoveryReadback $script:Readback $script:Before 'fixture' }
Test 'preserved field drift rejected' { Reset-Readback; $script:After.preserved_1=99; Throws { Assert-RecoveryReadback $script:Readback $script:Before 'fixture' } '*preserved field changed*' }
Test 'remaining incomplete work rejected' { Reset-Readback; $script:Readback.incomplete_count=1; Throws { Assert-RecoveryReadback $script:Readback $script:Before 'fixture' } '*readback mismatch*' }
Test 'success fabrication rejected' { Reset-Readback; $script:After.status='COMPLETED'; Throws { Assert-RecoveryReadback $script:Readback $script:Before 'fixture' } '*readback mismatch*' }
Test 'existing approval record cannot be overwritten' {
    $path=Join-Path ([IO.Path]::GetTempPath()) ('single-refresh-immutable-' + [guid]::NewGuid().ToString('N') + '.json')
    [IO.File]::WriteAllText($path,'original')
    try { Throws { Write-RecoveryRecord $path @{approved=$true} } '*'; Assert ([IO.File]::ReadAllText($path) -ceq 'original') 'Original record changed' }
    finally { Remove-Item -LiteralPath $path }
}
function Read-RecoveryPackage { param($Root); $fixture }
Test 'whole workflow rejects Phase C denial before any record or native call' {
    $script:NativeCalls=0
    Answers @('Fixture Reviewer','APPROVE_PHASE_A_TERMINATION_CERTIFICATE','NO')
    Throws { Invoke-SingleRefreshRecovery -Root 'unused' -ExecuteReconciliation } '*Phase C authorization missing*'
    Assert ($script:NativeCalls -eq 0 -and -not (Test-Path -LiteralPath $fixture.Evidence)) 'Rejected workflow crossed a side-effect boundary'
}
Test 'whole workflow rejects missing freeze before any record or native call' {
    $script:NativeCalls=0
    Answers @('Fixture Reviewer','APPROVE_PHASE_A_TERMINATION_CERTIFICATE','APPROVED_SINGLE_REFRESH_TERMINATION_AND_RECONCILIATION','NO')
    Throws { Invoke-SingleRefreshRecovery -Root 'unused' -ExecuteReconciliation } '*Maintenance freeze not confirmed*'
    Assert ($script:NativeCalls -eq 0 -and -not (Test-Path -LiteralPath $fixture.Evidence)) 'Rejected workflow crossed a side-effect boundary'
}

# Continuation tests use the signed-off records as read-only fixtures. Every hash,
# file-read, runtime probe and database boundary below is in-memory and mocked.
$originalA=Get-Content -LiteralPath (Join-Path $evidence 'phase-a-certificate-approval.json') -Raw | ConvertFrom-Json
$originalC=Get-Content -LiteralPath (Join-Path $evidence 'single-refresh-approval.json') -Raw | ConvertFrom-Json
$originalLog=Get-Content -LiteralPath (Join-Path $evidence 'single-refresh-workflow.log') -Raw
function Reset-Continuation {
    $script:ContinuationA=$originalA | ConvertTo-Json -Depth 24 | ConvertFrom-Json
    $script:ContinuationC=$originalC | ConvertTo-Json -Depth 24 | ConvertFrom-Json
    $script:ContinuationLog=$originalLog
    $script:ContinuationLogs=@(); $script:RemoteState='STAGING_ABSENT'; $script:ProbeCalls=0; $script:DatabaseCalls=0
    $script:ContinuationHashes=@{
        'phase-a-certificate-approval.json'='c91a17091208bbe114a5a0a643d70dbe4a35175011803f5903158a94a98d6ac6'
        'single-refresh-approval.json'='732f04f08b7d7b5004e0cea029f30cd75350ba47557ba8ea193087fb256d9eac'
        'single-refresh-workflow.log'='e2bfae6acfbb27fb6a6af6e91dad09501183d1820c80c711f5cc9abb6a3eea30'
    }
    $script:ContinuationBefore=[pscustomobject][ordered]@{
        refresh_run_id=$fixture.C.refresh_id;correlation_id=$fixture.C.correlation_id;status='RUNNING';mode='TARGETED_LIVE'
        started_at='2026-10-17T14:46:11.112618+00:00';updated_at='2026-10-17T14:46:11.103582+00:00';completed_at=$null
        safe_error_code=$null;safe_error_message=$null;company_id='fixture-company';instrument_id='fixture-instrument'
        events_created=0;events_updated=0;events_extracted=0;deduplicated_count=0;documents_accepted=0;documents_discovered=0
    }
    $script:ContinuationCurrent=[pscustomobject]@{row=($script:ContinuationBefore | ConvertTo-Json | ConvertFrom-Json);incomplete_count=1}
}
function Assert-RecoveryHash {
    param($Path,$Expected)
    if ($script:ContinuationHashes[(Split-Path -Leaf $Path)] -cne $Expected) { throw "Approved hash mismatch: $Path" }
}
function Get-ChildItem { param($LiteralPath,$Filter,[switch]$File); $script:ContinuationLogs }
function Get-Content {
    param($LiteralPath,[switch]$Raw)
    switch (Split-Path -Leaf $LiteralPath) {
        'phase-a-certificate-approval.json' { return ($script:ContinuationA | ConvertTo-Json -Depth 24) }
        'single-refresh-approval.json' { return ($script:ContinuationC | ConvertTo-Json -Depth 24) }
        'single-refresh-workflow.log' { return $script:ContinuationLog }
        'reviewed-preimages.csv' { $json='['+($script:ContinuationBefore | ConvertTo-Json -Compress)+']'; return ('"'+$json.Replace('"','""')+'"') }
        default { throw 'Unmocked continuation file read forbidden' }
    }
}
function Invoke-DeploymentKubectl {
    param($Arguments)
    Assert (($Arguments[0..6] -join '|') -ceq 'exec|postgres-0|-c|postgres|--|sh|-c') 'Invalid read-only staging-probe boundary'
    $script:ProbeCalls++; $script:RemoteState
}
function Read-RecoveryDatabase { param($Select); $script:DatabaseCalls++; $script:ContinuationCurrent }
Test 'PowerShell 5.1 JSON-array preimage decoding returns the row itself' {
    Reset-Continuation; $before=Read-RecoveryPreimage 'unused'
    Assert (@($before.PSObject.Properties).Count -eq 17 -and $before.refresh_run_id -ceq $fixture.C.refresh_id) 'Nested preimage array retained'
}
Test 'existing exact approvals and investigated failed-preflight log are accepted without execution' {
    Reset-Continuation; $existing=Read-RecoveryContinuation $fixture
    Assert ($existing.C.refresh_id -ceq $fixture.C.refresh_id -and $script:DatabaseCalls -eq 0 -and $script:ProbeCalls -eq 0) 'Continuation record review executed remote operations'
}
foreach ($file in @('phase-a-certificate-approval.json','single-refresh-approval.json','single-refresh-workflow.log')) {
    Test "changed retained artifact rejected: $file" {
        Reset-Continuation; $script:ContinuationHashes[$file]='0'*64
        Throws { Read-RecoveryContinuation $fixture } '*Approved hash mismatch*'
    }
}
Test 'UNKNOWN previous outcome rejected even with a mocked matching hash' {
    Reset-Continuation; $script:ContinuationLog+='UNKNOWN: inspect before retry'
    Throws { Read-RecoveryContinuation $fixture } '*Unrelated or ambiguous*'
}
Test 'previous mutation or commit receipt rejected' {
    Reset-Continuation; $script:ContinuationLog+='COMMITTED:'
    Throws { Read-RecoveryContinuation $fixture } '*Unrelated or ambiguous*'
}
Test 'unrelated prior STOP rejected' {
    Reset-Continuation; $script:ContinuationLog='STOP: unrelated failure'
    Throws { Read-RecoveryContinuation $fixture } '*Unrelated or ambiguous*'
}
Test 'different refresh approval rejected' {
    Reset-Continuation; $script:ContinuationC.refresh_id='different'
    Throws { Read-RecoveryContinuation $fixture } '*authorization identity*'
}
Test 'missing named review rejected' {
    Reset-Continuation; $script:ContinuationA.approved_by=$null
    Throws { Read-RecoveryContinuation $fixture } '*authorization identity*'
}
Test 'different certificate binding rejected' {
    Reset-Continuation; $script:ContinuationA.certificate_sha256='0'*64
    Throws { Read-RecoveryContinuation $fixture } '*not valid for continuation*'
}
Test 'missing Phase C decision rejected' {
    Reset-Continuation; $script:ContinuationC.maintenance_approval=$null
    Throws { Read-RecoveryContinuation $fixture } '*not valid for continuation*'
}
Test 'missing temporary staging authorization rejected' {
    Reset-Continuation; $script:ContinuationC.temporary_postgres_pod_file_staging_authorized=$false
    Throws { Read-RecoveryContinuation $fixture } '*not valid for continuation*'
}
Test 'changed preimage binding rejected' {
    Reset-Continuation; $script:ContinuationC.preimage_sha256='0'*64
    Throws { Read-RecoveryContinuation $fixture } '*binding changed*'
}
Test 'changed PostgreSQL connection rejected' {
    Reset-Continuation; $script:ContinuationC.execution_connection.pod_uid='different'
    Throws { Read-RecoveryContinuation $fixture } '*connection or five-field scope*'
}
Test 'existing continuation attempt prevents automatic replay' {
    Reset-Continuation; $script:ContinuationLogs=@('single-refresh-continuation-existing.log')
    Throws { Read-RecoveryContinuation $fixture } '*Prior continuation exists*'
}
Test 'absent staging and exact 17-field RUNNING preimage pass read-only guards' {
    Reset-Continuation; Assert-RecoveryContinuationReady $fixture
    Assert ($script:ProbeCalls -eq 1 -and $script:DatabaseCalls -eq 1) 'Expected only read-only probes'
}
foreach ($state in @('STAGING_PRESENT','UNKNOWN')) {
    Test "occupied or unknown staging blocks before database continuation: $state" {
        Reset-Continuation; $script:RemoteState=$state
        Throws { Assert-RecoveryContinuationReady $fixture } '*staging exists or is unverified*'
        Assert ($script:DatabaseCalls -eq 0) 'Occupied/unknown staging reached further work'
    }
}
foreach ($field in @('status','completed_at','updated_at','documents_accepted','instrument_id')) {
    Test "changed target field blocks continuation: $field" {
        Reset-Continuation; $script:ContinuationCurrent.row.$field='changed'
        Throws { Assert-RecoveryContinuationReady $fixture } '*no longer matches original preimage*'
    }
}
Test 'additional incomplete refresh blocks continuation' {
    Reset-Continuation; $script:ContinuationCurrent.incomplete_count=2
    Throws { Assert-RecoveryContinuationReady $fixture } '*UNKNOWN or changed refresh state*'
}
Test 'missing target row blocks continuation' {
    Reset-Continuation; $script:ContinuationCurrent.row=$null
    Throws { Assert-RecoveryContinuationReady $fixture } '*UNKNOWN or changed refresh state*'
}
Test 'continuation still requires explicit execution switch' {
    Throws { Invoke-SingleRefreshRecovery -Root 'unused' -ContinueFailedPreflight } '*Execution switch missing*'
}
Test 'whole continuation refuses changed prior audit before any remote call or file creation' {
    Reset-Continuation; $script:ContinuationHashes['single-refresh-workflow.log']='0'*64
    Throws { Invoke-SingleRefreshRecovery -Root 'unused' -ExecuteReconciliation -ContinueFailedPreflight } '*Approved hash mismatch*'
    Assert ($script:ProbeCalls -eq 0 -and $script:DatabaseCalls -eq 0 -and -not (Test-Path -LiteralPath $fixture.Evidence)) 'Rejected continuation performed side effects'
}
Test 'continuation reuses approvals but requires current freeze confirmation' {
    Reset-Continuation; Answers @('NO')
    Throws { Invoke-SingleRefreshRecovery -Root 'unused' -ExecuteReconciliation -ContinueFailedPreflight } '*Maintenance freeze not confirmed*'
    Assert ($script:ProbeCalls -eq 0 -and $script:DatabaseCalls -eq 0 -and -not (Test-Path -LiteralPath $fixture.Evidence)) 'Unconfirmed freeze performed side effects'
}
function Test-ContinuationStopsBeforeStage {
    param([string]$Fault)
    Reset-Continuation
    $script:StageInvoked=$false; $script:MutationInvoked=$false
    $directory=Join-Path ([IO.Path]::GetTempPath()) ('single-refresh-continuation-test-' + [guid]::NewGuid().ToString('N'))
    $null=[IO.Directory]::CreateDirectory($directory)
    $originals=@('phase-a-certificate-approval.json','single-refresh-approval.json','single-refresh-workflow.log')
    foreach ($name in $originals) { [IO.File]::WriteAllText((Join-Path $directory $name),('UNCHANGED:'+ $name)) }
    $logPath=Join-Path $directory ('single-refresh-continuation-' + [guid]::NewGuid().ToString('N') + '.log')
    $package=[pscustomobject]@{Evidence=$directory;C=$fixture.C;Blocks=@({},{$script:StageInvoked=$true;throw 'Unexpected staging'},{$script:MutationInvoked=$true;throw 'Unexpected mutation'})}
    function Assert-RecoveryRuntime { param($Package); if ($Fault -ceq 'executor') { throw 'Unknown executor' } }
    if ($Fault -ceq 'row') { $script:ContinuationCurrent.row.status='FAILED' }
    $pattern=if ($Fault -ceq 'executor') {'*Unknown executor*'} else {'*no longer matches original preimage*'}
    try {
        Throws { Invoke-RecoveryHandoff $package 'fixture-a-hash' 'fixture-c-hash' $logPath -PriorLogHash 'e2bfae6acfbb27fb6a6af6e91dad09501183d1820c80c711f5cc9abb6a3eea30' } $pattern
        Assert (-not $script:StageInvoked -and -not $script:MutationInvoked) 'Rejected continuation attempted staging/mutation'
        foreach ($name in $originals) { Assert ([IO.File]::ReadAllText((Join-Path $directory $name)) -ceq ('UNCHANGED:'+ $name)) 'Original artifact changed' }
        Assert ([IO.File]::ReadAllText($logPath) -match 'STOP: no automatic retry') 'New continuation audit was not retained'
    } finally {
        foreach ($name in $originals) { Remove-Item -LiteralPath (Join-Path $directory $name) }
        if (Test-Path -LiteralPath $logPath) { Remove-Item -LiteralPath $logPath }
        Remove-Item -LiteralPath $directory
    }
}
Test 'changed row stops actual handoff orchestration before staging and preserves original artifacts' { Test-ContinuationStopsBeforeStage 'row' }
Test 'unknown executor stops actual handoff orchestration before staging and preserves original artifacts' { Test-ContinuationStopsBeforeStage 'executor' }
Write-Output "$script:Passed tests passed; no live commands or reconciliation executed."
