# Dependency-free PowerShell 5.1 tests. Run from repository root as interactive commands.
# Only AST-extracted functions run; no platform top-level code or real external commands execute.
$ErrorActionPreference = 'Stop'
$script:Passed=0
function Assert($Condition, $Message) { if (-not $Condition) { throw $Message } }
function Test($Name, [scriptblock]$Body) { & $Body; $script:Passed++; Write-Output "PASS $Name" }
function Throws([scriptblock]$Body, $Pattern) { try { & $Body } catch { Assert ($_.Exception.Message -like $Pattern) "Unexpected error: $($_.Exception.Message)"; return }; throw "Expected error: $Pattern" }

    $platformPath = Join-Path (Get-Location) 'platform.ps1'
    $tokens = $null; $errors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile($platformPath, [ref]$tokens, [ref]$errors)
    if ($errors.Count) { throw ($errors | Out-String) }
    $functions = @('Invoke-DeploymentCommand','Invoke-DeploymentKubectl','Get-VerifiedRegistryImage',
        'New-VerifiedDeploymentPatch','Deploy-VerifiedRadarComponents','Deploy-VerifiedRadarComponentsCore','Deploy-SelectedComponents',
        'Initialize-DeploymentDiagnostics','Set-DeploymentPhase','ConvertTo-DeploymentArgument',
        'Assert-LocalPrerequisites','Assert-BuildPrerequisites','Ensure-RuntimeDirectory','Resolve-SelectedComponents')
    foreach ($name in $functions) {
        $node = $ast.Find({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name}, $true)
        . ([scriptblock]::Create($node.Extent.Text))
    }
    function New-TestDeployment($name) {
        $digests=@{'api-gateway'='69895f18d6360cdc087d51b3f54c29a822d283f709a1b50ae77bb2e18239d5b5';'research-engine'='c9ccb210e81c45a3152079baca8ca939e521340ed23dcace4522748eb98edff3';'portfolio-service'='6d5e111a118a659bf84bdee03e2b0561feed003f7feb6b41fa9ddebef80ddd9f';'frontend'='af0ddfbc03c38e44187a89382c88d717cc0b6bfa5ec199b9a0f0d519c889c60d'}
        $digest=$digests[$name]
        $image = "ai-investment-registry:5000/ai-investment/$name@sha256:$digest"
        $initContainers=@(); if($name -eq 'research-engine'){$initContainers+=@{name='wait-for-research-migrations';image=$image;command=@('python','-c');args=@('schema check')}}
        return [pscustomobject]@{metadata=@{name=$name;namespace='ai-investment';resourceVersion='123'};spec=@{template=@{spec=@{
            containers=@(@{name=$name;image=$image;env=@(@{name='UNRELATED';value='keep'})},@{name='sidecar';image='unchanged'})
            initContainers=$initContainers
        }}}}
    }
    function Manifest-Response($value) {
        $bytes = [Text.Encoding]::UTF8.GetBytes(($value | ConvertTo-Json -Depth 20 -Compress))
        $sha = [Security.Cryptography.SHA256]::Create()
        try { $digest = 'sha256:' + ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-','').ToLowerInvariant() } finally { $sha.Dispose() }
        return @{StatusCode=200;Content=$bytes;Headers=@{'Docker-Content-Digest'=$digest}}
    }

function Invoke-WebRequest {
    param($Uri,$Headers,$Method,[switch]$UseBasicParsing,$TimeoutSec,$MaximumRedirection)
    $script:Requests += $Uri
    if ($script:MissingManifest) { throw 'missing manifest' }
    if ($Uri -match '/blobs/') {
        if ($script:MissingBlob) { throw 'missing blob' }
        return @{StatusCode=200;Headers=@{'Content-Length'=$script:BlobLength}}
    }
    Assert ($Headers.Accept -match 'application/vnd.oci.image.index.v1\+json') 'OCI Accept absent'
    if ($Uri -match '/manifests/review-test$') { return $script:index }
    return $script:child
}
function Reset-Registry {
    $script:Requests=@(); $script:MissingManifest=$false; $script:MissingBlob=$false; $script:BlobLength='12'
    $script:blobDigest='sha256:'+('a'*64)
    $script:child=Manifest-Response @{schemaVersion=2;mediaType='application/vnd.oci.image.manifest.v1+json';config=@{digest=$script:blobDigest;size=12};layers=@(@{digest=$script:blobDigest;size=12})}
    $script:index=Manifest-Response @{schemaVersion=2;mediaType='application/vnd.oci.image.index.v1+json';manifests=@(@{digest=$script:child.Headers['Docker-Content-Digest'];platform=@{os='linux';architecture='amd64'}})}
}
Test 'OCI index, child manifest and blobs' {
    Reset-Registry
    $result=Get-VerifiedRegistryImage api-gateway review-test
    Assert ($result -eq "ai-investment-registry:5000/ai-investment/api-gateway@$($script:index.Headers['Docker-Content-Digest'])") 'Wrong digest'
    Assert ($script:Requests.Count -eq 4) 'Missing child/blob verification'
}
Test 'digest mismatch' { Reset-Registry; $script:index.Headers['Docker-Content-Digest']=$script:blobDigest; Throws {Get-VerifiedRegistryImage api-gateway review-test} '*digest mismatch*' }
Test 'child digest mismatch' { Reset-Registry; $script:child.Content=[Text.Encoding]::UTF8.GetBytes('{}'); Throws {Get-VerifiedRegistryImage api-gateway review-test} '*digest mismatch*' }
Test 'missing blob' { Reset-Registry; $script:MissingBlob=$true; Throws {Get-VerifiedRegistryImage api-gateway review-test} '*missing blob*' }
Test 'truncated blob' { Reset-Registry; $script:BlobLength='1'; Throws {Get-VerifiedRegistryImage api-gateway review-test} '*incomplete image blob*' }
Test 'rejects an index with no Linux amd64 platform' {
    Reset-Registry
    $script:index=Manifest-Response @{schemaVersion=2;mediaType='application/vnd.oci.image.index.v1+json';manifests=@(@{digest=$script:child.Headers['Docker-Content-Digest'];platform=@{os='linux';architecture='arm64'}})}
    Throws {Get-VerifiedRegistryImage api-gateway review-test} '*No linux/amd64*'
}
Test 'rejects an empty index' {
    Reset-Registry
    $script:index=Manifest-Response @{schemaVersion=2;mediaType='application/vnd.oci.image.index.v1+json';manifests=@()}
    Throws {Get-VerifiedRegistryImage api-gateway review-test} '*Empty image index*'
}
Test 'missing rollback manifest' { Reset-Registry; $script:MissingManifest=$true; Throws {Get-VerifiedRegistryImage api-gateway ('sha256:'+('b'*64))} '*missing manifest*' }
Test 'invalid reference' { Reset-Registry; Throws {Get-VerifiedRegistryImage api-gateway 'https://elsewhere/'} '*Invalid image reference*'; Assert ($script:Requests.Count -eq 0) 'Request escaped' }
Test 'missing main container' {
    $d=New-TestDeployment api-gateway; $d.spec.template.spec.containers=@()
    Throws {New-VerifiedDeploymentPatch $d api-gateway ('ai-investment-registry:5000/ai-investment/api-gateway@sha256:'+('f'*64))} '*container*'
}
Test 'missing migration init container' {
    $d=New-TestDeployment research-engine; $d.spec.template.spec.initContainers=@()
    Throws {New-VerifiedDeploymentPatch $d research-engine ('ai-investment-registry:5000/ai-investment/research-engine@sha256:'+('f'*64))} '*init container*'
}
Test 'native failure stops execution' {
    function Invoke-DeploymentNativeCommand { return @{ExitCode=17;StdOut='fixture stdout';StdErr='fixture stderr'} }
    Throws {Invoke-DeploymentCommand Failed-TestCommand @()} '*exit 17*'
}

# Replace all I/O boundaries before testing orchestration. No files are created.
function docker { throw 'Unmocked Docker command forbidden' }
function kubectl { throw 'Unmocked Kubernetes command forbidden' }
function helm { throw 'Unmocked Helm command forbidden' }
function mvn { throw 'Unmocked Maven command forbidden' }
function Assert-LocalPrerequisites {}
function Assert-BuildPrerequisites {}
function Ensure-RuntimeDirectory {}
function Show-DeploymentFailure {param($ErrorRecord)}
function Initialize-DeploymentSafety { if($script:GateFail -eq 'preflight'){throw 'gate preflight failed'}; return @{Frontend=@{apiBaseUrl='';authDevLoginEnabled='false'}} }
function Save-DeploymentBaseline {}
function Start-DeploymentIsolation { $script:GateCalls+='isolation'; if($script:GateFail -eq 'isolation'){throw 'gate isolation failed'} }
function Wait-DeploymentQuiet { $script:GateCalls+='quiet'; if($script:GateFail -eq 'quiet'){throw 'gate quiet failed'} }
function Assert-DeploymentQuietSample { if($script:GateFail -eq 'last-sample'){throw 'gate last sample failed'} }
function Complete-DeploymentSafety { $script:GateCalls+='complete' }

function Set-Content { param($LiteralPath,$Encoding,[Parameter(ValueFromPipeline=$true)]$Value) process { $script:Files[$LiteralPath]=$Value } }
function Get-VerifiedRegistryImage {
    param($Name,$Reference)
    if ($script:FailRegistry -or ($script:FailNewImage -and $Reference -like 'review-*')) { throw 'registry verification failed' }
    $digest=if($Reference -like 'sha256:*'){$Reference}else{'sha256:'+('f'*64)}
    "ai-investment-registry:5000/ai-investment/$Name@$digest"
}
function Invoke-DeploymentCommand {
    param($Program,$Arguments)
    $script:Commands+=@{Program=$Program;Arguments=$Arguments}
    if ($script:FailPush -and $Program -eq 'docker' -and $Arguments[0] -eq 'push') { throw 'push failed' }
    if ($Program -eq 'helm') { return '{"name":"ai-investment-platform","version":10}' }
    if ($Program -ne 'kubectl') { return }
    Assert (($Arguments[0..3] -join ' ') -eq '--context k3d-ai-investment-dev -n ai-investment') 'Unpinned kubectl'
    if (($Arguments -join ' ') -match '/readyz') { return 'ok' }
    if ($Arguments[4] -eq 'get' -and $Arguments[5] -eq 'deployment') {
        $name=$Arguments[6]; $d=New-TestDeployment $name
        if ($script:WrongRunningImage) { $d.spec.template.spec.containers[0].image='ai-investment-registry:5000/ai-investment/'+$name+'@sha256:'+('e'*64) }
        if ($script:MissingDeployment) { throw 'deployment missing' }
        if ($script:Patched[$name]) {
            $spec=$script:Patches[$name].spec.template.spec
            $d.spec.template.spec.containers[0].image=$spec.containers[0].image
            if ($name -eq 'research-engine') {
                $d.spec.template.spec.initContainers[0].image=$spec.initContainers[0].image
                $d.spec.template.spec.containers[0].env=$spec.containers[0].env
            }
        }
        return ($d|ConvertTo-Json -Depth 20)
    }
    if ($Arguments[4] -eq 'patch') {
        if ($script:FailPatch) { throw 'patch failed' }
        $name=$Arguments[6]; $script:Patched[$name]=$true
        $script:Patches[$name]=$script:Files[$Arguments[-1]]|ConvertFrom-Json
    }
}
function Reset-Deployment {
    $script:ProjectRoot='C:\workspace\ai-investment-platform-integration'; $script:RuntimeDir='mock-runtime'
    $script:AcceptBootstrapRisk=$true; $script:ConfirmCallerFreeze=$true; $script:GateFail='';$script:GateCalls=@(); $script:SkipBuild=$false; $script:DryRun=$false; $script:NoCache=$false
    $script:Commands=@();$script:Files=@{};$script:Patched=@{};$script:Patches=@{}
    $script:FailRegistry=$false;$script:FailNewImage=$false;$script:FailPush=$false;$script:FailPatch=$false;$script:MissingDeployment=$false;$script:WrongRunningImage=$false
}
Test 'two-component scope, Maven dependencies, digest patch and atomic research pause' {
    Reset-Deployment
    Deploy-VerifiedRadarComponents @('api-gateway','research-engine')
    $mvn=@($script:Commands|Where-Object Program -eq 'mvn')
    Assert ($mvn.Count -eq 1 -and ($mvn[0].Arguments -join ' ') -match '-pl api-gateway -am package -DskipTests') 'Unscoped Maven'
    foreach($verb in @('build','push')) { Assert (@($script:Commands|Where-Object {$_.Program -eq 'docker' -and $_.Arguments[0] -eq $verb}).Count -eq 2) 'Wrong image scope' }
    Assert (($script:Patched.Keys|Sort-Object) -join ',' -eq 'api-gateway,research-engine') 'Unrelated deployment changed'
    $spec=$script:Patches['research-engine'].spec.template.spec
    Assert ($spec.containers.Count -eq 1 -and $spec.containers[0].image -match '@sha256:f{64}$') 'Main image not pinned'
    Assert ($spec.initContainers[0].image -eq $spec.containers[0].image) 'Init image differs'
    Assert (-not ($spec.initContainers[0].PSObject.Properties.Name -contains 'command')) 'Init command overwritten'
    Assert (($spec.containers[0].env|Where-Object name -eq 'AIP_RESEARCH_OPPORTUNITY_RECOVERY_PAUSED').value -eq 'true') 'Pause absent'
    Assert (($spec.containers[0].env|Where-Object name -eq 'AIP_RESEARCH_OPPORTUNITY_SCHEDULER_ENABLED').value -eq 'false') 'Scheduler enabled'
    Assert ($script:Patches['research-engine'].metadata.resourceVersion -eq '123') 'Concurrent changes not fenced'
    Assert (@($script:Commands|Where-Object {($_.Arguments -join ' ') -match 'upgrade|delete|prune|apply|set env|rollback'}).Count -eq 0) 'Unsafe command'
    $rollbacks=@($script:Files.Keys|Where-Object {$_ -match 'rollback-'})
    Assert ($rollbacks.Count -eq 1) 'Rollback not saved'
    Assert ($script:Files[$rollbacks[0]] -match '69895f18' -and $script:Files[$rollbacks[0]] -match 'c9ccb210') 'Rollback references changed'
    $push=@($script:Commands|Where-Object {$_.Program -eq 'docker' -and $_.Arguments[0] -eq 'push'})[0]
    Assert ($push.Arguments[1] -match ':review-\d{8}-\d{6}-[a-f0-9]{32}$') 'Tag not collision resistant'
}
Test 'research-only build leaves Java and gateway untouched' {
    Reset-Deployment; Deploy-VerifiedRadarComponents @('research-engine')
    Assert (@($script:Commands|Where-Object Program -eq 'mvn').Count -eq 0) 'Java built'
    Assert (($script:Patched.Keys -join ',') -eq 'research-engine') 'Gateway changed'
}
Test 'dry-run has no build, push, patch or file writes' {
    Reset-Deployment; $script:DryRun=$true; Deploy-VerifiedRadarComponents @('api-gateway','research-engine')
    Assert (@($script:Commands|Where-Object {$_.Program -in @('docker','mvn')}).Count -eq 0) 'Build executed'
    Assert ($script:Patched.Count -eq 0 -and $script:Files.Count -eq 0) 'Mutation executed'
}
foreach($failure in @('FailRegistry','FailNewImage','FailPush','MissingDeployment','WrongRunningImage')) {
    Test "fail closed: $failure" {
        Reset-Deployment; Set-Variable -Scope Script -Name $failure -Value $true
        Throws {Deploy-VerifiedRadarComponents @('api-gateway','research-engine')} '*'
        Assert ($script:Patched.Count -eq 0) 'Patched after failure'
    }
}
Test 'patch failure stops without automatic rollback' {
    Reset-Deployment; $script:FailPatch=$true
    Throws {Deploy-VerifiedRadarComponents @('api-gateway','research-engine')} '*patch failed*'
    Assert (@($script:Commands|Where-Object {$_.Program -eq 'kubectl' -and $_.Arguments[4] -eq 'patch'}).Count -eq 1) 'Continued after failure'
}
Test 'unrelated selection rejected by narrow path' { Reset-Deployment; Throws {Deploy-VerifiedRadarComponents @('postgres')} '*selection*'; Assert ($script:Commands.Count -eq 0) 'Unexpected commands' }
Test 'entry point selects verified path' {
    Reset-Deployment
    function Resolve-SelectedComponents { @('api-gateway','research-engine') }
    Deploy-SelectedComponents
    Assert ($script:Patched.Count -eq 2) 'Verified path not used'
}
Test 'four-service exact scope and mandatory gates' {
 Reset-Deployment
 Deploy-VerifiedRadarComponents @('api-gateway','research-engine','portfolio-service','frontend')
 Assert (($script:Patched.Keys | Sort-Object) -join ',' -eq 'api-gateway,frontend,portfolio-service,research-engine') 'Scope wrong'
 $mvn=@($script:Commands | Where-Object Program -eq 'mvn')
 Assert ($mvn.Count -eq 1 -and ($mvn[0].Arguments -join ' ') -match '-pl api-gateway,portfolio-service -am') 'Java scope wrong'
 Assert (@($script:Commands | Where-Object {$_.Program -eq 'docker' -and $_.Arguments[0] -eq 'build'}).Count -eq 4) 'Wrong build count'
 Assert (($script:GateCalls -join ',') -eq 'isolation,quiet,complete') 'Missing mandatory gates'
 $frontend=@($script:Commands | Where-Object {$_.Program -eq 'docker' -and $_.Arguments[0] -eq 'build' -and ($_.Arguments -join ' ') -match 'NEXT_PUBLIC'})
 Assert ($frontend.Count -eq 1 -and ($frontend[0].Arguments -join ' ') -match 'NEXT_PUBLIC_AUTH_DEV_LOGIN_ENABLED=false') 'Frontend changed'
}
foreach($gate in @('preflight','isolation','quiet','last-sample')) {
 Test "mandatory gate failure: $gate" {
  Reset-Deployment; $script:GateFail=$gate
  Throws {Deploy-VerifiedRadarComponents @('api-gateway','research-engine','portfolio-service','frontend')} '*gate*'
  Assert ($script:Patched.Count -eq 0) 'Workload patched after failed gate'
 }
}
Test 'risk acceptance required' {
 Reset-Deployment; $script:AcceptBootstrapRisk=$false
 Throws {Deploy-VerifiedRadarComponents @('research-engine')} '*acceptance*'
 Assert (@($script:Commands | Where-Object {$_.Program -in @('docker','mvn')}).Count -eq 0) 'Build before acceptance'
}
Write-Output "RESULT: $script:Passed passed, 0 failed"
