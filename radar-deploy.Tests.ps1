# No external commands execute. PowerShell 5.1, repository root.
# Mirrors scripts/database/tests/DeploymentSafety.Tests.ps1's AST-extraction
# pattern: radar-deploy.ps1 (like platform.ps1) has an immediately-executing
# top-level body (RequireCommand/CheckBuildInputs/the switch($Action) block,
# which with the default -Action Plan runs real git/helm/kubectl commands),
# so it is never dot-sourced directly. Only its FunctionDefinitionAst nodes
# are extracted and loaded; the param block and switch body are read as raw
# text for structural assertions instead of ever being executed.
$ErrorActionPreference='Stop'
$script:Passed=0
function Assert($condition,$message){if(-not $condition){throw $message}}
function Test($name,[scriptblock]$body){ & $body; $script:Passed++; Write-Output "PASS $name" }
function Throws([scriptblock]$body,$pattern){try{& $body}catch{Assert ($_.Exception.Message -like $pattern) "Unexpected failure: $($_.Exception.Message)";return};throw 'Expected failure'}

$scriptPath = Join-Path (Get-Location) 'radar-deploy.ps1'
$raw = Get-Content -Raw $scriptPath
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($scriptPath,[ref]$tokens,[ref]$errors)
Assert (-not $errors.Count) 'PowerShell parse errors'
foreach($node in $ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst]},$false)){. ([scriptblock]::Create($node.Extent.Text))}

# Image()/ClusterImage() are loaded through the AST only; the script's own
# param() block never runs, so its defaults are reproduced here.
$Registry='localhost:5001'
$ClusterRegistry='ai-investment-registry:5000'
$Tag='radar-20261027-2316'

Test 'Image() (the host-side build/push reference) uses $Registry' {
    Assert ((Image 'frontend') -eq "${Registry}/ai-investment/frontend:${Tag}") 'Image() did not use the host-side registry'
}

Test 'ClusterImage() (the kubectl pull reference) uses $ClusterRegistry, never $Registry' {
    $img = ClusterImage 'frontend'
    Assert ($img -eq "${ClusterRegistry}/ai-investment/frontend:${Tag}") 'ClusterImage() did not use the cluster-side registry'
    Assert ($img -notlike '*localhost*') 'ClusterImage() leaked a host-local (localhost) registry reference'
}

Test 'Image() and ClusterImage() agree on repository path and tag, differing only by registry host' {
    foreach ($component in @('frontend','research-engine','api-gateway','portfolio-service')) {
        $hostImg = Image $component
        $clusterImg = ClusterImage $component
        Assert ($hostImg -eq "${Registry}/ai-investment/${component}:${Tag}") "Image() drifted for $component"
        Assert ($clusterImg -eq "${ClusterRegistry}/ai-investment/${component}:${Tag}") "ClusterImage() drifted for $component"
        Assert ($clusterImg -notlike '*localhost*') "ClusterImage() leaked localhost for $component"
    }
}

Test 'ClusterImage() never produces a host-local reference for any configured $ClusterRegistry' {
    foreach ($candidate in @('ai-investment-registry:5000','ai-investment-registry.ai-investment.svc.cluster.local:5000')) {
        $script:ClusterRegistry = $candidate
        $img = ClusterImage 'frontend'
        Assert ($img -notlike '*localhost*') "ClusterImage() leaked localhost for ClusterRegistry=$candidate"
    }
    $script:ClusterRegistry = 'ai-investment-registry:5000'
}

# --- Structural assertions on the un-executed script body (raw text) -------
# These prove the actual 'Deploy' switch case -- which this test harness
# deliberately never executes, since it runs real kubectl/helm commands --
# is wired to ClusterImage, not Image, for the value handed to kubectl,
# while Build/Push (which docker-push to the host-side registry) are
# untouched and still use Image.

$deployStart = $raw.IndexOf("'Deploy' {")
$freshStart = $raw.IndexOf("'Fresh' {")
Assert ($deployStart -ge 0 -and $freshStart -gt $deployStart) 'Could not locate the Deploy case in radar-deploy.ps1'
$deployCase = $raw.Substring($deployStart, $freshStart - $deployStart)

Test 'Deploy case assigns the kubectl image from ClusterImage, never from the host-side Image()' {
    Assert ($deployCase -match '\$img\s*=\s*ClusterImage\s+\$component') 'Deploy case no longer assigns $img from ClusterImage()'
    Assert ($deployCase -notmatch '\$img\s*=\s*Image\s+\$component') 'Deploy case still assigns $img from the host-side Image() function -- the original defect'
}

Test 'Deploy case never references a host-local (localhost) registry anywhere in its kubectl image path' {
    $imagePathLines = ($deployCase -split "`n" | Where-Object { $_ -match 'kubectl|\$img\s*=' }) -join "`n"
    Assert ($imagePathLines -notlike '*localhost*') 'Deploy case kubectl image-setting path references a host-local registry'
}

$buildStart = $raw.IndexOf("'Build' {")
$pushStart = $raw.IndexOf("'Push' {")
$deployStart2 = $raw.IndexOf("'Deploy' {")
$buildCase = $raw.Substring($buildStart, $pushStart - $buildStart)
$pushCase = $raw.Substring($pushStart, $deployStart2 - $pushStart)

Test 'Build and Push cases still use the host-side Image(), unaffected by the registry-address fix' {
    Assert ($buildCase -match 'Image\s+\$component') 'Build case no longer calls Image()'
    Assert ($buildCase -notmatch 'ClusterImage') 'Build case unexpectedly calls ClusterImage()'
    Assert ($pushCase -match 'Image\s+\$component') 'Push case no longer calls Image()'
    Assert ($pushCase -notmatch 'ClusterImage') 'Push case unexpectedly calls ClusterImage()'
}

Test 'param defaults match the k3d cluster-dev.yaml registry (host port 5001, cluster-internal port 5000)' {
    $paramBlock = $raw.Substring(0, $raw.IndexOf('function Image'))
    Assert ($paramBlock -match "\`$Registry\s*=\s*'localhost:5001'") 'Default $Registry drifted from the host-exposed k3d registry port'
    Assert ($paramBlock -match "\`$ClusterRegistry\s*=\s*'ai-investment-registry:5000'") 'Default $ClusterRegistry drifted from the k3d-managed registry name/internal port'
    $k3dConfigPath = Join-Path (Get-Location) 'infrastructure/k3d/cluster-dev.yaml'
    if (Test-Path $k3dConfigPath) {
        $k3dRaw = Get-Content -Raw $k3dConfigPath
        Assert ($k3dRaw -match 'name:\s*ai-investment-registry') 'k3d config registry name no longer matches the ClusterRegistry default'
        Assert ($k3dRaw -match 'hostPort:\s*"?5001"?') 'k3d config host port no longer matches the Registry default'
    }
}

Write-Output "$script:Passed passed"
