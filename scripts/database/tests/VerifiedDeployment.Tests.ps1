# Pester 5 tests. Run from repository root with: Invoke-Pester -Path scripts\database\tests\VerifiedDeployment.Tests.ps1
# All setup (AST extraction of real platform.ps1 functions, and every I/O
# mock) lives inside BeforeAll blocks so nothing executes during Pester's
# discovery phase -- only It/Describe/Context registration happens then.
# No platform top-level code or real external command ever executes.

Describe 'Get-VerifiedRegistryImage and New-VerifiedDeploymentPatch' {
    BeforeAll {
        function Assert($Condition, $Message) { if (-not $Condition) { throw $Message } }
        function Throws([scriptblock]$Body, $Pattern) { try { & $Body } catch { Assert ($_.Exception.Message -like $Pattern) "Unexpected error: $($_.Exception.Message)"; return }; throw "Expected error: $Pattern" }

        $platformPath = Join-Path (Get-Location) 'platform.ps1'
        $tokens = $null; $errors = $null
        $ast = [System.Management.Automation.Language.Parser]::ParseFile($platformPath, [ref]$tokens, [ref]$errors)
        if ($errors.Count) { throw ($errors | Out-String) }
        $functions = @('Get-VerifiedRegistryImage', 'New-VerifiedDeploymentPatch')
        foreach ($name in $functions) {
            $node = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name }, $true)
            . ([scriptblock]::Create($node.Extent.Text))
        }
        # Mirrors platform.ps1's own top-level safety-net initialization
        # (line ~57), which this isolated AST extraction never executes.
        # Get-VerifiedRegistryImage's existing "if ($script:DeploymentRun)"
        # guard needs $script:DeploymentRun to exist (even as $null) under
        # an ambient Set-StrictMode -- exactly what that top-level line is
        # already there to guarantee for every other caller.
        $script:DeploymentRun = $null

        function New-TestDeployment($name) {
            $digests = @{'api-gateway' = '69895f18d6360cdc087d51b3f54c29a822d283f709a1b50ae77bb2e18239d5b5'; 'research-engine' = 'c9ccb210e81c45a3152079baca8ca939e521340ed23dcace4522748eb98edff3'; 'portfolio-service' = '6d5e111a118a659bf84bdee03e2b0561feed003f7feb6b41fa9ddebef80ddd9f'; 'frontend' = 'af0ddfbc03c38e44187a89382c88d717cc0b6bfa5ec199b9a0f0d519c889c60d' }
            $digest = $digests[$name]
            $image = "ai-investment-registry:5000/ai-investment/$name@sha256:$digest"
            $initContainers = @(); if ($name -eq 'research-engine') { $initContainers += @{name = 'wait-for-research-migrations'; image = $image; command = @('python', '-c'); args = @('schema check') } }
            return [pscustomobject]@{metadata = @{name = $name; namespace = 'ai-investment'; resourceVersion = '123' }; spec = @{template = @{spec = @{
                        containers     = @(@{name = $name; image = $image; env = @(@{name = 'UNRELATED'; value = 'keep' }) }, @{name = 'sidecar'; image = 'unchanged' })
                        initContainers = $initContainers
                    } } } }
        }
        function Manifest-Response($value) {
            $bytes = [Text.Encoding]::UTF8.GetBytes(($value | ConvertTo-Json -Depth 20 -Compress))
            $sha = [Security.Cryptography.SHA256]::Create()
            try { $digest = 'sha256:' + ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant() } finally { $sha.Dispose() }
            return @{StatusCode = 200; Content = $bytes; Headers = @{'Docker-Content-Digest' = $digest } }
        }

        function Invoke-WebRequest {
            param($Uri, $Headers, $Method, [switch]$UseBasicParsing, $TimeoutSec, $MaximumRedirection)
            $script:Requests += $Uri
            if ($script:MissingManifest) { throw 'missing manifest' }
            if ($Uri -match '/blobs/') {
                if ($script:MissingBlob) { throw 'missing blob' }
                $blobDigest = ($Uri -split '/')[-1]
                if ($blobDigest -eq $script:configDigest) {
                    if (-not $Method -or $Method -ne 'Head') {
                        return @{StatusCode = 200; Content = $script:blobConfigBytes; Headers = @{'Content-Length' = ($script:blobConfigBytes.Length) } }
                    }
                    return @{StatusCode = 200; Headers = @{'Content-Length' = ($script:blobConfigBytes.Length) } }
                }
                if (-not $Method -or $Method -ne 'Head') {
                    return @{StatusCode = 200; Content = $script:blobConfigBytes; Headers = @{'Content-Length' = $script:BlobLength } }
                }
                return @{StatusCode = 200; Headers = @{'Content-Length' = $script:BlobLength } }
            }
            Assert ($Headers.Accept -match 'application/vnd.oci.image.index.v1\+json') 'OCI Accept absent'
            if ($Uri -match '/manifests/review-test$') { return $script:index }
            return $script:child
        }
        function Reset-Registry {
            $script:Requests = @(); $script:MissingManifest = $false; $script:MissingBlob = $false; $script:BlobLength = '12'
            $script:VerifiedDeploymentDigests = $null
            $script:blobDigest = 'sha256:' + ('a' * 64)
            $script:blobConfigBytes = [Text.Encoding]::UTF8.GetBytes('{"os":"linux","architecture":"amd64"}')
            $sha = [Security.Cryptography.SHA256]::Create()
            try { $script:configDigest = 'sha256:' + ([BitConverter]::ToString($sha.ComputeHash($script:blobConfigBytes))).Replace('-', '').ToLowerInvariant() } finally { $sha.Dispose() }
            $script:child = Manifest-Response @{schemaVersion = 2; mediaType = 'application/vnd.oci.image.manifest.v1+json'; config = @{digest = $script:blobDigest; size = 12 }; layers = @(@{digest = $script:blobDigest; size = 12 }) }
            $script:index = Manifest-Response @{schemaVersion = 2; mediaType = 'application/vnd.oci.image.index.v1+json'; manifests = @(@{digest = $script:child.Headers['Docker-Content-Digest']; platform = @{os = 'linux'; architecture = 'amd64' } }) }
        }
    }

    It 'OCI index, child manifest and blobs' {
        Reset-Registry
        $script:child = Manifest-Response @{schemaVersion = 2; mediaType = 'application/vnd.oci.image.manifest.v1+json'; config = @{digest = $script:configDigest; size = $script:blobConfigBytes.Length }; layers = @(@{digest = $script:blobDigest; size = 12 }) }
        $script:index = Manifest-Response @{schemaVersion = 2; mediaType = 'application/vnd.oci.image.index.v1+json'; manifests = @(@{digest = $script:child.Headers['Docker-Content-Digest']; platform = @{os = 'linux'; architecture = 'amd64' } }) }
        $result = Get-VerifiedRegistryImage api-gateway review-test
        Assert ($result -eq "ai-investment-registry:5000/ai-investment/api-gateway@$($script:index.Headers['Docker-Content-Digest'])") 'Wrong digest'
        Assert ($script:Requests.Count -eq 5) 'Missing child/blob/config verification'
        Assert ($script:VerifiedDeploymentDigests[$result] -contains $script:configDigest) 'Config digest not recorded for OCI index child'
    }
    It 'config digest verified and recorded for direct manifest reference' {
        Reset-Registry
        $script:child = Manifest-Response @{schemaVersion = 2; mediaType = 'application/vnd.oci.image.manifest.v1+json'; config = @{digest = $script:configDigest; size = $script:blobConfigBytes.Length }; layers = @(@{digest = $script:blobDigest; size = 12 }) }
        $childDigest = $script:child.Headers['Docker-Content-Digest']
        $result = Get-VerifiedRegistryImage api-gateway $childDigest
        Assert ($script:Requests.Count -eq 4) 'Missing config blob GET verification'
        Assert ($result -eq "ai-investment-registry:5000/ai-investment/api-gateway@$childDigest") 'Wrong digest for direct reference'
        Assert ($script:VerifiedDeploymentDigests[$result] -contains $script:configDigest) 'Config digest not recorded'
    }
    It 'config digest mismatch rejected for direct manifest reference' {
        Reset-Registry
        $childDigest = $script:child.Headers['Docker-Content-Digest']
        $script:blobConfigBytes = [Text.Encoding]::UTF8.GetBytes('tampered config')
        Throws { Get-VerifiedRegistryImage api-gateway $childDigest } '*Config digest mismatch*'
    }
    It 'config digest mismatch rejected for OCI index child' {
        Reset-Registry
        Throws { Get-VerifiedRegistryImage api-gateway review-test } '*Config digest mismatch*'
    }
    It 'incorrect platform in config blob rejected' {
        Reset-Registry
        $script:blobConfigBytes = [Text.Encoding]::UTF8.GetBytes('{"os":"windows","architecture":"amd64"}')
        $sha = [Security.Cryptography.SHA256]::Create()
        try { $script:configDigest = 'sha256:' + ([BitConverter]::ToString($sha.ComputeHash($script:blobConfigBytes))).Replace('-', '').ToLowerInvariant() } finally { $sha.Dispose() }
        $script:child = Manifest-Response @{schemaVersion = 2; mediaType = 'application/vnd.oci.image.manifest.v1+json'; config = @{digest = $script:configDigest; size = $script:blobConfigBytes.Length }; layers = @(@{digest = $script:blobDigest; size = 12 }) }
        $childDigest = $script:child.Headers['Docker-Content-Digest']
        Throws { Get-VerifiedRegistryImage api-gateway $childDigest } '*No linux/amd64*'
    }
    It 'unrelated configuration digest not accepted as pod image identity' {
        Reset-Registry
        $script:child = Manifest-Response @{schemaVersion = 2; mediaType = 'application/vnd.oci.image.manifest.v1+json'; config = @{digest = $script:configDigest; size = $script:blobConfigBytes.Length }; layers = @(@{digest = $script:blobDigest; size = 12 }) }
        $childDigest = $script:child.Headers['Docker-Content-Digest']
        $result = Get-VerifiedRegistryImage api-gateway $childDigest
        $unrelated = 'sha256:' + ('9' * 64)
        Assert ($script:VerifiedDeploymentDigests[$result] -notcontains $unrelated) 'Unrelated digest accepted as verified identity'
        Assert ($script:VerifiedDeploymentDigests[$result] -contains $script:configDigest) 'Config digest missing from verified identities'
    }
    It 'digest mismatch' { Reset-Registry; $script:index.Headers['Docker-Content-Digest'] = $script:blobDigest; Throws { Get-VerifiedRegistryImage api-gateway review-test } '*digest mismatch*' }
    It 'child digest mismatch' { Reset-Registry; $script:child.Content = [Text.Encoding]::UTF8.GetBytes('{}'); Throws { Get-VerifiedRegistryImage api-gateway review-test } '*digest mismatch*' }
    It 'missing blob' { Reset-Registry; $script:MissingBlob = $true; Throws { Get-VerifiedRegistryImage api-gateway review-test } '*missing blob*' }
    It 'truncated blob' { Reset-Registry; $script:BlobLength = '1'; Throws { Get-VerifiedRegistryImage api-gateway review-test } '*incomplete image blob*' }
    It 'rejects an index with no Linux amd64 platform' {
        Reset-Registry
        $script:index = Manifest-Response @{schemaVersion = 2; mediaType = 'application/vnd.oci.image.index.v1+json'; manifests = @(@{digest = $script:child.Headers['Docker-Content-Digest']; platform = @{os = 'linux'; architecture = 'arm64' } }) }
        Throws { Get-VerifiedRegistryImage api-gateway review-test } '*No linux/amd64*'
    }
    It 'rejects an empty index' {
        Reset-Registry
        $script:index = Manifest-Response @{schemaVersion = 2; mediaType = 'application/vnd.oci.image.index.v1+json'; manifests = @() }
        Throws { Get-VerifiedRegistryImage api-gateway review-test } '*Empty image index*'
    }
    It 'missing rollback manifest' { Reset-Registry; $script:MissingManifest = $true; Throws { Get-VerifiedRegistryImage api-gateway ('sha256:' + ('b' * 64)) } '*missing manifest*' }
    It 'invalid reference' { Reset-Registry; Throws { Get-VerifiedRegistryImage api-gateway 'https://elsewhere/' } '*Invalid image reference*'; Assert ($script:Requests.Count -eq 0) 'Request escaped' }
    It 'missing main container' {
        $d = New-TestDeployment api-gateway; $d.spec.template.spec.containers = @()
        Throws { New-VerifiedDeploymentPatch $d api-gateway ('ai-investment-registry:5000/ai-investment/api-gateway@sha256:' + ('f' * 64)) } '*container*'
    }
    It 'missing migration init container' {
        $d = New-TestDeployment research-engine; $d.spec.template.spec.initContainers = @()
        Throws { New-VerifiedDeploymentPatch $d research-engine ('ai-investment-registry:5000/ai-investment/research-engine@sha256:' + ('f' * 64)) } '*init container*'
    }
}

Describe 'Invoke-DeploymentCommand native failure handling' {
    BeforeAll {
        function Assert($Condition, $Message) { if (-not $Condition) { throw $Message } }
        function Throws([scriptblock]$Body, $Pattern) { try { & $Body } catch { Assert ($_.Exception.Message -like $Pattern) "Unexpected error: $($_.Exception.Message)"; return }; throw "Expected error: $Pattern" }
        $platformPath = Join-Path (Get-Location) 'platform.ps1'
        $tokens = $null; $errors = $null
        $ast = [System.Management.Automation.Language.Parser]::ParseFile($platformPath, [ref]$tokens, [ref]$errors)
        if ($errors.Count) { throw ($errors | Out-String) }
        $node = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Invoke-DeploymentCommand' }, $true)
        . ([scriptblock]::Create($node.Extent.Text))
        # Same test-harness gap as the Describe block above: this isolated
        # extraction never runs platform.ps1's top-level
        # "$script:DeploymentRun = $null" safety net that its own
        # "if ($script:DeploymentRun)" guards depend on under StrictMode.
        $script:DeploymentRun = $null
    }

    It 'native failure stops execution' {
        function Invoke-DeploymentNativeCommand { return @{ExitCode = 17; StdOut = 'fixture stdout'; StdErr = 'fixture stderr' } }
        Throws { Invoke-DeploymentCommand Failed-TestCommand @() } '*exit 17*'
    }
}

Describe 'Verified deployment orchestration' {
    BeforeAll {
        function Assert($Condition, $Message) { if (-not $Condition) { throw $Message } }
        function Throws([scriptblock]$Body, $Pattern) { try { & $Body } catch { Assert ($_.Exception.Message -like $Pattern) "Unexpected error: $($_.Exception.Message)"; return }; throw "Expected error: $Pattern" }

        . (Join-Path (Get-Location) 'scripts/database/rollback-baseline.ps1')

        $platformPath = Join-Path (Get-Location) 'platform.ps1'
        $tokens = $null; $errors = $null
        $ast = [System.Management.Automation.Language.Parser]::ParseFile($platformPath, [ref]$tokens, [ref]$errors)
        if ($errors.Count) { throw ($errors | Out-String) }
        $functions = @('Invoke-DeploymentCommand', 'Invoke-DeploymentKubectl', 'Get-VerifiedRegistryImage',
            'New-VerifiedDeploymentPatch', 'Get-LocalFullEtfExecutionFlag', 'Deploy-VerifiedRadarComponents', 'Deploy-VerifiedRadarComponentsCore', 'Deploy-SelectedComponents',
            'Initialize-DeploymentDiagnostics', 'Set-DeploymentPhase', 'ConvertTo-DeploymentArgument',
            'Assert-LocalPrerequisites', 'Assert-BuildPrerequisites', 'Ensure-RuntimeDirectory', 'Resolve-SelectedComponents')
        foreach ($name in $functions) {
            $node = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name }, $true)
            if (-not $node) { throw "Function not found in platform.ps1: $name" }
            . ([scriptblock]::Create($node.Extent.Text))
        }

        function New-TestDeployment($name) {
            $digests = @{'api-gateway' = '69895f18d6360cdc087d51b3f54c29a822d283f709a1b50ae77bb2e18239d5b5'; 'research-engine' = 'c9ccb210e81c45a3152079baca8ca939e521340ed23dcace4522748eb98edff3'; 'portfolio-service' = '6d5e111a118a659bf84bdee03e2b0561feed003f7feb6b41fa9ddebef80ddd9f'; 'frontend' = 'af0ddfbc03c38e44187a89382c88d717cc0b6bfa5ec199b9a0f0d519c889c60d' }
            $digest = $digests[$name]
            $image = "ai-investment-registry:5000/ai-investment/$name@sha256:$digest"
            $initContainers = @(); if ($name -eq 'research-engine') { $initContainers += @{name = 'wait-for-research-migrations'; image = $image; command = @('python', '-c'); args = @('schema check') } }
            return [pscustomobject]@{metadata = @{name = $name; namespace = 'ai-investment'; resourceVersion = '123' }; spec = @{template = @{spec = @{
                        containers     = @(@{name = $name; image = $image; env = @(@{name = 'UNRELATED'; value = 'keep' }) }, @{name = 'sidecar'; image = 'unchanged' })
                        initContainers = $initContainers
                    } } } }
        }

        # New-VerifiedDeploymentPatch now reads the real LOCAL Full-ETF-execution
        # flag via Get-LocalFullEtfExecutionFlag (extracted above, unmocked) for
        # every research-engine patch. Give it a real fixture file so the real,
        # unmodified function exercises real DEV-configuration-reading behavior
        # instead of being stubbed out.
        $script:HelmValuesFixtureRoot = Join-Path ([IO.Path]::GetTempPath()) ("verified-deploy-tests-" + [guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $script:HelmValuesFixtureRoot -Force | Out-Null
        $script:HelmValues = Join-Path $script:HelmValuesFixtureRoot 'values-dev.yaml'
        [System.IO.File]::WriteAllText($script:HelmValues, "researchEngine:`n  etfFullUniverseExecutionEnabled: false`n")

        function docker { throw 'Unmocked Docker command forbidden' }
        function kubectl { throw 'Unmocked Kubernetes command forbidden' }
        function helm { throw 'Unmocked Helm command forbidden' }
        function mvn { throw 'Unmocked Maven command forbidden' }
        function Assert-LocalPrerequisites {}
        function Assert-BuildPrerequisites {}
        function Ensure-RuntimeDirectory {}
        function Show-DeploymentFailure { param($ErrorRecord) }
        function Initialize-DeploymentSafety { if ($script:GateFail -eq 'preflight') { throw 'gate preflight failed' }; return @{Frontend = @{apiBaseUrl = ''; authDevLoginEnabled = 'false' } } }
        function Save-DeploymentBaseline {}
        function Start-DeploymentIsolation { $script:GateCalls += 'isolation'; if ($script:GateFail -eq 'isolation') { throw 'gate isolation failed' } }
        function Wait-DeploymentQuiet { $script:GateCalls += 'quiet'; if ($script:GateFail -eq 'quiet') { throw 'gate quiet failed' } }
        function Assert-DeploymentQuietSample { if ($script:GateFail -eq 'last-sample') { throw 'gate last sample failed' } }
        function Complete-DeploymentSafety { $script:GateCalls += 'complete'; if ($script:CompleteDeploymentSafetyFail) { throw 'post-rollout verification failed' } }

        function Get-ApprovedRollbackBaseline {
            param($ProjectRoot)
            $components = @{
                # These must equal the running-image digests New-TestDeployment
                # bakes into the fixture Deployments below, so the approved
                # baseline agrees with the "current" running image on the
                # success path. WrongRunningImage deliberately overrides the
                # running image to a different digest ('e'*64) to exercise the
                # mismatch fail-closed path; it must keep diverging from these.
                # $script:ApprovedBaselineOverride lets a test deliberately make
                # the approved baseline STALE relative to the running image, to
                # exercise the durable-promotion-evidence trust path instead.
                'api-gateway'       = 'sha256:69895f18d6360cdc087d51b3f54c29a822d283f709a1b50ae77bb2e18239d5b5'
                'research-engine'   = 'sha256:c9ccb210e81c45a3152079baca8ca939e521340ed23dcace4522748eb98edff3'
                'portfolio-service' = 'sha256:6d5e111a118a659bf84bdee03e2b0561feed003f7feb6b41fa9ddebef80ddd9f'
                'frontend'          = 'sha256:af0ddfbc03c38e44187a89382c88d717cc0b6bfa5ec199b9a0f0d519c889c60d'
            }
            foreach ($key in $script:ApprovedBaselineOverride.Keys) { $components[$key] = $script:ApprovedBaselineOverride[$key] }
            [pscustomobject]@{
                Components      = $components
                InitContainers  = $script:ApprovedInitContainers
                BaselineVersion = 1
                ApprovedAtUtc   = '2026-01-01T00:00:00Z'
                ApprovedBy      = 'fixture'
            }
        }

        function Set-Content { param($LiteralPath, $Encoding, [Parameter(ValueFromPipeline = $true)]$Value) process { $script:Files[$LiteralPath] = $Value } }
        function Save-RollbackBaselineApprovalCandidate {
            param($ProjectRoot, $RuntimeDir, $Tag, $Names, $Images, $InitImages = @{})
            $path = Join-Path $RuntimeDir "rollback-baseline-candidate-$Tag.json"
            $script:PromotionEvidence[$Tag] = @{Names = @($Names); Images = $Images.Clone(); InitImages = $InitImages }
            $script:Files[$path] = 'candidate'
            return $path
        }
        function Get-LatestVerifiedPromotionEvidence {
            param($RuntimeDir, $Name)
            $best = $null
            foreach ($tag in $script:PromotionEvidence.Keys) {
                $entry = $script:PromotionEvidence[$tag]
                if ($Name -notin $entry.Names) { continue }
                $initImage = $null
                if ($entry.InitImages.ContainsKey('wait-for-research-migrations') -and $entry.InitImages['wait-for-research-migrations'].component -eq $Name) {
                    $initImage = [pscustomobject]@{ContainerName = 'wait-for-research-migrations'; Image = $entry.InitImages['wait-for-research-migrations'].image }
                }
                $best = [pscustomobject]@{Image = $entry.Images[$Name]; InitImage = $initImage; Tag = $tag; RecordedAtUtc = (Get-Date) }
            }
            return $best
        }
        function Get-VerifiedRegistryImage {
            param($Name, $Reference)
            $script:RegistryChecks += @{Name = $Name; Reference = $Reference }
            if ($Reference -eq $script:RunningInitDigest -and $script:InitRegistryFailure) { throw $script:InitRegistryFailure }
            if ($Reference -eq $script:RunningInitDigest -and $script:InitRegistryFailsAfterPreflight -and
                @($script:RegistryChecks | Where-Object Reference -eq $script:RunningInitDigest).Count -gt 2) { throw 'Init rollback manifest unavailable' }
            if ($script:FailRegistry -or ($script:FailNewImage -and $Reference -like 'review-*')) { throw 'registry verification failed' }
            $digest = if ($Reference -like 'sha256:*') { $Reference } else { 'sha256:' + ('f' * 64) }
            "ai-investment-registry:5000/ai-investment/$Name@$digest"
        }
        function Invoke-DeploymentCommand {
            param($Program, $Arguments)
            $script:Commands += @{Program = $Program; Arguments = $Arguments }
            if ($script:FailPush -and $Program -eq 'docker' -and $Arguments[0] -eq 'push') { throw 'push failed' }
            if ($Program -eq 'helm') { return '{"name":"ai-investment-platform","version":10}' }
            if ($Program -ne 'kubectl') { return }
            Assert (($Arguments[0..3] -join ' ') -eq '--context k3d-ai-investment-dev -n ai-investment') 'Unpinned kubectl'
            if (($Arguments -join ' ') -match '/readyz') { return 'ok' }
            if ($Arguments[4] -eq 'get' -and $Arguments[5] -eq 'deployment') {
                $name = $Arguments[6]; $d = New-TestDeployment $name
                if ($script:WrongRunningImage) { $d.spec.template.spec.containers[0].image = 'ai-investment-registry:5000/ai-investment/' + $name + '@sha256:' + ('e' * 64) }
                if ($name -eq 'research-engine') {
                    $d.spec.template.spec.initContainers[0].image = "ai-investment-registry:5000/ai-investment/research-engine@$script:RunningInitDigest"
                    if ($script:WrongInitImage) { $d.spec.template.spec.initContainers[0].image = 'ai-investment-registry:5000/ai-investment/research-engine@sha256:' + ('e' * 64) }
                }
                switch ($script:DeploymentFault) {
                    'deployment-name' { $d.metadata.name = 'other' }
                    'namespace' { $d.metadata.namespace = 'other' }
                    'main-container' { $d.spec.template.spec.containers[0].name = 'other' }
                    'init-container' { $d.spec.template.spec.initContainers[0].name = 'other' }
                    'resource-version' { $d.metadata.resourceVersion = $null }
                }
                if ($script:MissingDeployment) { throw 'deployment missing' }
                if ($script:Patched[$name]) {
                    $spec = $script:Patches[$name].spec.template.spec
                    $d.spec.template.spec.containers[0].image = $spec.containers[0].image
                    if ($name -eq 'research-engine') {
                        if (-not $script:KeepOldInitAfterRollout) { $d.spec.template.spec.initContainers[0].image = $spec.initContainers[0].image }
                        $d.spec.template.spec.containers[0].env = $spec.containers[0].env
                    }
                }
                return ($d | ConvertTo-Json -Depth 20)
            }
            if ($Arguments[4] -eq 'patch') {
                if ($script:FailPatch) { throw 'patch failed' }
                $name = $Arguments[6]; $script:Patched[$name] = $true
                $script:Patches[$name] = $script:Files[$Arguments[-1]] | ConvertFrom-Json
            }
        }
        function Reset-Deployment {
            $script:ProjectRoot = 'C:\workspace\ai-investment-platform-integration'; $script:RuntimeDir = 'mock-runtime'
            $script:AcceptBootstrapRisk = $true; $script:ConfirmCallerFreeze = $true; $script:GateFail = ''; $script:GateCalls = @(); $script:SkipBuild = $false; $script:DryRun = $false; $script:NoCache = $false
            $script:Commands = @(); $script:Files = @{}; $script:Patched = @{}; $script:Patches = @{}
            $script:FailRegistry = $false; $script:FailNewImage = $false; $script:FailPush = $false; $script:FailPatch = $false; $script:MissingDeployment = $false; $script:WrongRunningImage = $false
            $script:RunningInitDigest = 'sha256:' + ('7' * 64)
            $script:ApprovedInitContainers = @{'wait-for-research-migrations' = $script:RunningInitDigest }
            $script:RegistryChecks = @(); $script:WrongInitImage = $false; $script:DeploymentFault = ''
            $script:InitRegistryFailure = ''; $script:InitRegistryFailsAfterPreflight = $false; $script:KeepOldInitAfterRollout = $false
            $script:PromotionEvidence = @{}; $script:ApprovedBaselineOverride = @{}; $script:CompleteDeploymentSafetyFail = $false
        }
    }

    AfterAll {
        Remove-Item -LiteralPath $script:HelmValuesFixtureRoot -Recurse -Force -ErrorAction SilentlyContinue
    }

    It 'Get-LocalFullEtfExecutionFlag is callable from New-VerifiedDeploymentPatch''s call scope and reads the fixture value' {
        # Targeted regression for the extraction gap fixed above: this must call
        # the REAL Get-LocalFullEtfExecutionFlag (not a fake) both directly and
        # indirectly (via New-VerifiedDeploymentPatch, exactly as
        # Deploy-VerifiedRadarComponentsCore calls it for every research-engine
        # patch), and both must agree with the real fixture file's value.
        Reset-Deployment
        $flag = Get-LocalFullEtfExecutionFlag
        Assert ($flag -eq 'false') "Expected fixture flag 'false', got '$flag'"
        $d = New-TestDeployment research-engine
        $patch = New-VerifiedDeploymentPatch $d research-engine ('ai-investment-registry:5000/ai-investment/research-engine@sha256:' + ('f' * 64))
        $etfEnv = $patch.spec.template.spec.containers[0].env | Where-Object name -eq 'AIP_RESEARCH_ETF_FULL_UNIVERSE_EXECUTION_ENABLED'
        Assert ($null -ne $etfEnv) 'New-VerifiedDeploymentPatch did not set the Full-ETF-execution env var; Get-LocalFullEtfExecutionFlag was not resolved in its call scope'
        Assert ($etfEnv.value -eq $flag) 'Full-ETF-execution env value does not match the real Get-LocalFullEtfExecutionFlag fixture read'
    }
    It 'two-component scope, Maven dependencies, digest patch and atomic research pause' {
        Reset-Deployment
        Deploy-VerifiedRadarComponents @('api-gateway', 'research-engine')
        $mvn = @($script:Commands | Where-Object Program -eq 'mvn')
        Assert ($mvn.Count -eq 1 -and ($mvn[0].Arguments -join ' ') -match '-pl api-gateway -am package -DskipTests') 'Unscoped Maven'
        foreach ($verb in @('build', 'push')) { Assert (@($script:Commands | Where-Object { $_.Program -eq 'docker' -and $_.Arguments[0] -eq $verb }).Count -eq 2) 'Wrong image scope' }
        Assert (($script:Patched.Keys | Sort-Object) -join ',' -eq 'api-gateway,research-engine') 'Unrelated deployment changed'
        $spec = $script:Patches['research-engine'].spec.template.spec
        Assert ($spec.containers.Count -eq 1 -and $spec.containers[0].image -match '@sha256:f{64}$') 'Main image not pinned'
        Assert ($spec.initContainers[0].image -eq $spec.containers[0].image) 'Init image differs'
        Assert (-not ($spec.initContainers[0].PSObject.Properties.Name -contains 'command')) 'Init command overwritten'
        Assert (($spec.containers[0].env | Where-Object name -eq 'AIP_RESEARCH_OPPORTUNITY_RECOVERY_PAUSED').value -eq 'true') 'Pause absent'
        Assert (($spec.containers[0].env | Where-Object name -eq 'AIP_RESEARCH_OPPORTUNITY_SCHEDULER_ENABLED').value -eq 'false') 'Scheduler enabled'
        Assert ($script:Patches['research-engine'].metadata.resourceVersion -eq '123') 'Concurrent changes not fenced'
        Assert (@($script:Commands | Where-Object { ($_.Arguments -join ' ') -match 'upgrade|delete|prune|apply|set env|rollback' }).Count -eq 0) 'Unsafe command'
        # Two independent, equally required evidence files: the pre-deploy
        # rollback snapshot (Save-VerifiedRollbackSnapshot's inline
        # equivalent, "rollback-<tag>.json") and the post-verification
        # promotion-evidence candidate ("rollback-baseline-candidate-<tag>.json").
        # Both file names start with "rollback-", so they must be
        # distinguished explicitly rather than by a single loose filter.
        $rollbacks = @($script:Files.Keys | Where-Object { $_ -match 'rollback-' -and $_ -notmatch 'rollback-baseline-candidate-' })
        Assert ($rollbacks.Count -eq 1) 'Pre-deployment rollback snapshot not saved'
        Assert ($script:Files[$rollbacks[0]] -match '69895f18' -and $script:Files[$rollbacks[0]] -match 'c9ccb210') 'Rollback references changed'
        $promotionEvidenceFiles = @($script:Files.Keys | Where-Object { $_ -match 'rollback-baseline-candidate-' })
        Assert ($promotionEvidenceFiles.Count -eq 1) 'Post-verification promotion evidence not recorded'
        Assert ($script:PromotionEvidence.Count -eq 1) 'Post-verification promotion evidence not recorded in the evidence store'
        $push = @($script:Commands | Where-Object { $_.Program -eq 'docker' -and $_.Arguments[0] -eq 'push' })[0]
        Assert ($push.Arguments[1] -match ':review-\d{8}-\d{6}-[a-f0-9]{32}$') 'Tag not collision resistant'
    }
    It 'research-only build leaves Java and gateway untouched' {
        Reset-Deployment; Deploy-VerifiedRadarComponents @('research-engine')
        Assert (@($script:Commands | Where-Object Program -eq 'mvn').Count -eq 0) 'Java built'
        Assert (($script:Patched.Keys -join ',') -eq 'research-engine') 'Gateway changed'
    }
    It 'dry-run has no build, push, patch or file writes' {
        Reset-Deployment; $script:DryRun = $true; Deploy-VerifiedRadarComponents @('api-gateway', 'research-engine')
        Assert (@($script:Commands | Where-Object { $_.Program -in @('docker', 'mvn') }).Count -eq 0) 'Build executed'
        Assert ($script:Patched.Count -eq 0 -and $script:Files.Count -eq 0) 'Mutation executed'
    }
    It 'dry-run accepts independently approved main and init digests without risk acceptance' {
        Reset-Deployment
        $script:DryRun = $true; $script:AcceptBootstrapRisk = $false; $script:ConfirmCallerFreeze = $false
        Deploy-VerifiedRadarComponents @('research-engine')
        $researchChecks = @($script:RegistryChecks | Where-Object Name -eq 'research-engine')
        Assert (@($researchChecks | Where-Object Reference -eq $script:RunningInitDigest).Count -eq 2) 'Init baseline and running image were not independently registry-verified'
        Assert (@($researchChecks | Where-Object Reference -eq 'sha256:c9ccb210e81c45a3152079baca8ca939e521340ed23dcace4522748eb98edff3').Count -eq 2) 'Main baseline verification changed'
        Assert ($script:Patched.Count -eq 0 -and $script:Files.Count -eq 0) 'Dry-run mutated state'
        Assert ($script:GateCalls.Count -eq 0) 'Dry-run entered deployment gates'
    }
    It 'rejects an init image that differs from its independently approved baseline' {
        Reset-Deployment; $script:WrongInitImage = $true; $script:DryRun = $true
        Throws { Deploy-VerifiedRadarComponents @('research-engine') } '*wait-for-research-migrations*does not match the approved rollback baseline*promotion evidence*'
        Assert ($script:Patched.Count -eq 0 -and $script:Files.Count -eq 0) 'Mutation after init mismatch'
    }
    It 'rejects a missing or malformed init baseline: <Case>' -ForEach @(
        @{Case = 'absent map'; Baselines = $null },
        @{Case = 'missing container'; Baselines = @{} },
        @{Case = 'empty digest'; Baselines = @{'wait-for-research-migrations' = '' } },
        @{Case = 'malformed digest'; Baselines = @{'wait-for-research-migrations' = 'not-a-digest' } }
    ) {
        Reset-Deployment; $script:ApprovedInitContainers = $Baselines; $script:DryRun = $true
        Throws { Deploy-VerifiedRadarComponents @('research-engine') } '*missing a valid sha256 digest*wait-for-research-migrations*'
        Assert ($script:Patched.Count -eq 0 -and $script:Files.Count -eq 0) 'Mutation with invalid init approval'
    }
    It 'rejects an unavailable or registry-inconsistent init baseline: <_>' -ForEach @('Manifest unavailable', 'Manifest digest mismatch', 'Config digest mismatch') {
        Reset-Deployment; $script:InitRegistryFailure = $_; $script:DryRun = $true
        Throws { Deploy-VerifiedRadarComponents @('research-engine') } "*$_*"
        Assert ($script:Patched.Count -eq 0 -and $script:Files.Count -eq 0) 'Mutation after registry failure'
    }
    It 'rejects incorrect deployment/container identity or missing resource version: <_>' -ForEach @('deployment-name', 'namespace', 'main-container', 'init-container', 'resource-version') {
        Reset-Deployment; $script:DeploymentFault = $_; $script:DryRun = $true
        Throws { Deploy-VerifiedRadarComponents @('research-engine') } '*'
        Assert ($script:Patched.Count -eq 0 -and $script:Files.Count -eq 0) 'Mutation after identity validation failed'
    }
    It 'rechecks init rollback availability before rollout' {
        Reset-Deployment; $script:InitRegistryFailsAfterPreflight = $true
        Throws { Deploy-VerifiedRadarComponents @('research-engine') } '*Init rollback manifest unavailable*'
        Assert ($script:Patched.Count -eq 0) 'Patched without an available init rollback image'
    }
    It 'still requires the new deployment image for the init container after rollout' {
        Reset-Deployment; $script:KeepOldInitAfterRollout = $true
        Throws { Deploy-VerifiedRadarComponents @('research-engine') } '*Init image verification failed*'
    }
    It 'other components do not require or resolve an init baseline' {
        Reset-Deployment; $script:ApprovedInitContainers = @{}; $script:DryRun = $true
        Deploy-VerifiedRadarComponents @('api-gateway', 'portfolio-service', 'frontend')
        Assert (@($script:RegistryChecks | Where-Object Name -eq 'research-engine').Count -eq 0) 'Unrelated init image resolved'
        Assert ($script:Patched.Count -eq 0 -and $script:Files.Count -eq 0) 'Dry-run mutated state'
    }
    It "fail closed: <_>" -ForEach @('FailRegistry', 'FailNewImage', 'FailPush', 'MissingDeployment', 'WrongRunningImage') {
        Reset-Deployment; Set-Variable -Scope Script -Name $_ -Value $true
        Throws { Deploy-VerifiedRadarComponents @('api-gateway', 'research-engine') } '*'
        Assert ($script:Patched.Count -eq 0) 'Patched after failure'
    }
    It 'patch failure stops without automatic rollback' {
        Reset-Deployment; $script:FailPatch = $true
        Throws { Deploy-VerifiedRadarComponents @('api-gateway', 'research-engine') } '*patch failed*'
        Assert (@($script:Commands | Where-Object { $_.Program -eq 'kubectl' -and $_.Arguments[4] -eq 'patch' }).Count -eq 1) 'Continued after failure'
    }
    It 'unrelated selection rejected by narrow path' { Reset-Deployment; Throws { Deploy-VerifiedRadarComponents @('postgres') } '*selection*'; Assert ($script:Commands.Count -eq 0) 'Unexpected commands' }
    It 'entry point explicit risk acceptance always selects verified path' {
        Reset-Deployment
        function Resolve-SelectedComponents { @('api-gateway', 'research-engine') }
        $script:AcceptBootstrapRisk = $true
        $script:ConfirmCallerFreeze = $true
        Deploy-SelectedComponents
        Assert ($script:Patched.Count -eq 2) 'Verified path not used'
    }
    It 'four-service exact scope and mandatory gates' {
        Reset-Deployment
        Deploy-VerifiedRadarComponents @('api-gateway', 'research-engine', 'portfolio-service', 'frontend')
        Assert (($script:Patched.Keys | Sort-Object) -join ',' -eq 'api-gateway,frontend,portfolio-service,research-engine') 'Scope wrong'
        $mvn = @($script:Commands | Where-Object Program -eq 'mvn')
        Assert ($mvn.Count -eq 1 -and ($mvn[0].Arguments -join ' ') -match '-pl api-gateway,portfolio-service -am') 'Java scope wrong'
        Assert (@($script:Commands | Where-Object { $_.Program -eq 'docker' -and $_.Arguments[0] -eq 'build' }).Count -eq 4) 'Wrong build count'
        Assert (($script:GateCalls -join ',') -eq 'isolation,quiet,complete') 'Missing mandatory gates'
        $frontend = @($script:Commands | Where-Object { $_.Program -eq 'docker' -and $_.Arguments[0] -eq 'build' -and ($_.Arguments -join ' ') -match 'NEXT_PUBLIC' })
        Assert ($frontend.Count -eq 1 -and ($frontend[0].Arguments -join ' ') -match 'NEXT_PUBLIC_AUTH_DEV_LOGIN_ENABLED=false') 'Frontend changed'
    }
    It "mandatory gate failure: <_>" -ForEach @('preflight', 'isolation', 'quiet', 'last-sample') {
        Reset-Deployment; $script:GateFail = $_
        Throws { Deploy-VerifiedRadarComponents @('api-gateway', 'research-engine', 'portfolio-service', 'frontend') } '*gate*'
        Assert ($script:Patched.Count -eq 0) 'Workload patched after failed gate'
    }
    It 'risk acceptance required' {
        Reset-Deployment; $script:AcceptBootstrapRisk = $false
        Throws { Deploy-VerifiedRadarComponents @('research-engine') } '*acceptance*'
        Assert (@($script:Commands | Where-Object { $_.Program -in @('docker', 'mvn') }).Count -eq 0) 'Build before acceptance'
    }

    It 'accepts a running image that differs from the approved rollback baseline when durable promotion evidence independently re-verifies it' {
        Reset-Deployment
        $script:ApprovedBaselineOverride = @{'research-engine' = 'sha256:' + ('d' * 64) }
        $runningImage = 'ai-investment-registry:5000/ai-investment/research-engine@sha256:c9ccb210e81c45a3152079baca8ca939e521340ed23dcace4522748eb98edff3'
        $initImage = "ai-investment-registry:5000/ai-investment/research-engine@$script:RunningInitDigest"
        $script:PromotionEvidence['prior-tag'] = @{
            Names      = @('research-engine')
            Images     = @{'research-engine' = $runningImage }
            InitImages = @{'wait-for-research-migrations' = @{component = 'research-engine'; image = $initImage } }
        }
        Deploy-VerifiedRadarComponents @('research-engine')
        Assert ($script:Patched['research-engine']) 'Deployment was blocked despite matching durable promotion evidence, not the stale approved baseline'
    }
    It 'rejects a running image that matches neither the approved baseline nor any durable promotion evidence, before any mutation' {
        Reset-Deployment; $script:WrongRunningImage = $true
        Throws { Deploy-VerifiedRadarComponents @('api-gateway', 'research-engine') } '*does not match the approved rollback baseline*no durable, re-verifiable promotion evidence*'
        Assert ($script:Patched.Count -eq 0 -and $script:Files.Count -eq 0) 'Mutation occurred despite unestablished provenance'
    }
    It 'an interrupted promotion -- rollout succeeded but post-rollout verification failed -- records no durable promotion evidence' {
        Reset-Deployment; $script:CompleteDeploymentSafetyFail = $true
        Throws { Deploy-VerifiedRadarComponents @('research-engine') } '*post-rollout verification failed*'
        Assert ($script:Patched['research-engine']) 'Rollout itself did not occur before the simulated verification failure'
        Assert ($script:PromotionEvidence.Count -eq 0) 'Promotion evidence was recorded despite an incomplete/uncertain promotion -- it must stay uncertain until reconciled'
    }
    It 'a successful authorized promotion records durable promotion evidence for every deployed component, including its init container' {
        Reset-Deployment
        Deploy-VerifiedRadarComponents @('research-engine')
        Assert ($script:PromotionEvidence.Count -eq 1) 'Expected exactly one recorded promotion'
        $entry = @($script:PromotionEvidence.Values)[0]
        Assert ('research-engine' -in $entry.Names) 'Promotion evidence missing component'
        Assert ($entry.Images['research-engine'] -match '^ai-investment-registry:5000/ai-investment/research-engine@sha256:[a-f0-9]{64}$') 'Promotion evidence image malformed'
        Assert ($entry.InitImages.ContainsKey('wait-for-research-migrations') -and $entry.InitImages['wait-for-research-migrations'].component -eq 'research-engine') 'Promotion evidence missing init-container record'
        $evidenceFiles = @($script:Files.Keys | Where-Object { $_ -match 'rollback-baseline-candidate-' })
        Assert ($evidenceFiles.Count -eq 1) 'Promotion evidence file not recorded'
    }
    It 'recording promotion evidence never advances or rewrites the approved rollback baseline' {
        Reset-Deployment
        function Approve-RollbackBaseline { throw 'Approve-RollbackBaseline must never be called automatically by the deployment pipeline' }
        function Write-AdvancedRollbackBaseline { throw 'Write-AdvancedRollbackBaseline must never be called automatically by the deployment pipeline' }
        Deploy-VerifiedRadarComponents @('research-engine')
        Assert ($script:Patched['research-engine']) 'Deployment did not complete normally while these approval functions were armed to fail'
    }
    It 'two consecutive authorized deployments require no manual rollback-baseline edit' {
        Reset-Deployment
        Deploy-VerifiedRadarComponents @('research-engine')
        Assert ($script:PromotionEvidence.Count -eq 1) 'First deployment did not record promotion evidence'
        $patchesAfterFirst = @($script:Commands | Where-Object { $_.Program -eq 'kubectl' -and $_.Arguments[4] -eq 'patch' }).Count
        Deploy-VerifiedRadarComponents @('research-engine')
        $patchesAfterSecond = @($script:Commands | Where-Object { $_.Program -eq 'kubectl' -and $_.Arguments[4] -eq 'patch' }).Count
        Assert ($patchesAfterSecond -gt $patchesAfterFirst) 'Second deployment was blocked by the stale approved rollback baseline despite durable promotion evidence from the first'
        Assert ($script:PromotionEvidence.Count -eq 2) 'Second deployment did not also record its own promotion evidence'
    }
}
Describe 'Assert-ResumeAudit (generalized interrupted-deployment resume)' {
    BeforeAll {
        function Assert($Condition, $Message) { if (-not $Condition) { throw $Message } }
        function Throws([scriptblock]$Body, $Pattern) { try { & $Body } catch { Assert ($_.Exception.Message -like $Pattern) "Unexpected error: $($_.Exception.Message)"; return }; throw "Expected error: $Pattern" }

        $platformPath = Join-Path (Get-Location) 'platform.ps1'
        $tokens = $null; $errors = $null
        $ast = [System.Management.Automation.Language.Parser]::ParseFile($platformPath, [ref]$tokens, [ref]$errors)
        if ($errors.Count) { throw ($errors | Out-String) }
        foreach ($name in @('Assert-ResumeAudit', 'Assert-DeploymentWorkloads')) {
            $node = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name }, $true)
            if (-not $node) { throw "Function not found in platform.ps1: $name" }
            . ([scriptblock]::Create($node.Extent.Text))
        }

        # This Describe block tests Assert-ResumeAudit DIRECTLY and in
        # isolation -- the same granularity already used for
        # Invoke-InterruptedPromotionRecovery -- rather than through the
        # full Deploy-VerifiedRadarComponentsCore pipeline, which would
        # require mocking every kubectl/docker/helm/registry boundary that
        # function also touches. Assert-ResumeAudit reads $RuntimeDir as a
        # bare (unparameterized) variable, resolved dynamically from the
        # caller's scope exactly like platform.ps1's own top-level
        # parameter does for every real caller -- so this BeforeAll sets
        # $script:RuntimeDir, never passes it as an argument.
        $script:ResumeTestRoot = Join-Path ([IO.Path]::GetTempPath()) ("resume-audit-tests-" + [guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $script:ResumeTestRoot -Force | Out-Null
        $script:RuntimeDir = $script:ResumeTestRoot

        # [pscustomobject]@{...} literals preserve source property order on
        # both sides of a JSON round-trip (unlike a plain @{} Hashtable,
        # whose .NET enumeration order is unspecified) -- required so the
        # on-disk saved deployment/workload/isolation fixtures compare
        # byte-identical (via ConvertTo-Json -Compress) to the in-memory
        # "live" fixtures the mocked kubectl boundary returns, exactly as
        # the real cluster/registry-independent comparisons expect.
        function New-ResumeDeployment($name, $uid, $resourceVersion) {
            [pscustomobject]@{
                metadata = [pscustomobject]@{ name = $name; uid = $uid; resourceVersion = $resourceVersion }
                spec     = [pscustomobject]@{ template = [pscustomobject]@{ spec = [pscustomobject]@{ containers = @([pscustomobject]@{ name = $name }) } } }
            }
        }

        $script:Selector = [pscustomobject]@{ matchLabels = [pscustomobject]@{ 'app.kubernetes.io/component' = 'research-engine'; 'app.kubernetes.io/instance' = 'ai-investment-platform'; 'app.kubernetes.io/name' = 'ai-investment-platform' } }

        function New-ResumeFixture {
            param([string]$Tag, [string[]]$Names, [string]$Fingerprint = ('a' * 64), [hashtable]$OldImages, [switch]$OmitObservations, [switch]$BadSelector)
            $dir = Join-Path $script:RuntimeDir $Tag
            New-Item -ItemType Directory -Path $dir -Force | Out-Null
            $script:ResumeDeployments = @{}
            $record = [ordered]@{}
            foreach ($name in $Names) {
                $dep = New-ResumeDeployment $name "uid-$name" "rv-$name"
                $script:ResumeDeployments[$name] = $dep
                $record[$name] = $dep
            }
            $record | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath (Join-Path $dir 'original-deployments.json') -Encoding UTF8
            $script:ResumeWorkloadsLive = [pscustomobject]@{ items = @($Names | ForEach-Object { [pscustomobject]@{ kind = 'Deployment'; metadata = [pscustomobject]@{ name = $_ }; spec = [pscustomobject]@{ marker = 'unchanged' } } }) }
            $script:ResumeWorkloadsLive | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath (Join-Path $dir 'original-workloads.json') -Encoding UTF8
            $selector = if ($BadSelector) { [pscustomobject]@{ matchLabels = [pscustomobject]@{ 'app.kubernetes.io/component' = 'frontend'; 'app.kubernetes.io/instance' = 'ai-investment-platform'; 'app.kubernetes.io/name' = 'ai-investment-platform' } } } else { $script:Selector }
            $policy = [pscustomobject]@{ metadata = [pscustomobject]@{ name = 'research-bootstrap-isolation' }; spec = [pscustomobject]@{ podSelector = $selector; policyTypes = @('Ingress'); ingress = @() } }
            $policy | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath (Join-Path $dir 'isolation.json') -Encoding UTF8
            if (-not $OmitObservations) {
                (@{ fingerprint = $Fingerprint } | ConvertTo-Json -Compress) | Set-Content -LiteralPath (Join-Path $dir 'observations.jsonl') -Encoding UTF8
            }
            $snapshot = [ordered]@{}
            foreach ($name in $Names) { $snapshot[$name] = $OldImages[$name] }
            $snapshot | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $script:RuntimeDir "rollback-$Tag.json") -Encoding ASCII
            $script:RecoveryOldImages = $OldImages
        }

        function New-ResumeGate {
            param([string]$Fingerprint = ('a' * 64))
            return @{ Selector = $script:Selector; Baseline = @{ fingerprint = $Fingerprint } }
        }

        $script:ActiveJobs = [pscustomobject]@{ items = @() }
        function Invoke-DeploymentKubectl {
            param([string[]]$Arguments)
            if ($Arguments -contains 'jobs,cronjobs') { return ($script:ActiveJobs | ConvertTo-Json -Depth 10 -Compress) }
            if ($Arguments -contains 'deployments,statefulsets') { return ($script:ResumeWorkloadsLive | ConvertTo-Json -Depth 20 -Compress) }
            throw "Unmocked Invoke-DeploymentKubectl arguments: $($Arguments -join ' ')"
        }
    }

    BeforeEach {
        $script:ActiveJobs = [pscustomobject]@{ items = @() }
    }

    It 'resumes a well-formed single-component tag never seen on the original hardcoded (October 15, two-component) attempt' {
        $tag = 'review-20261022-000253-c08fbbe701b042719ebe3c15ca05b7d4'
        $names = @('research-engine')
        New-ResumeFixture -Tag $tag -Names $names -OldImages @{ 'research-engine' = 'ai-investment-registry:5000/ai-investment/research-engine@sha256:' + ('b' * 64) }
        $gate = New-ResumeGate
        Assert-ResumeAudit -Tag $tag -Names $names -Deployments $script:ResumeDeployments -Gate $gate
        Assert ($gate.AuditPath -eq (Join-Path $script:RuntimeDir $tag)) 'AuditPath not set to the resumed tag''s audit directory'
    }

    It 'rejects a tag that is not a well-formed review tag' {
        $gate = New-ResumeGate
        Throws { Assert-ResumeAudit -Tag 'review-not-a-valid-tag-shape' -Names @('research-engine') -Deployments @{} -Gate $gate } '*not a well-formed review tag*'
    }

    It 'rejects missing/inconsistent review evidence: component selection not matching the saved audit' {
        $tag = 'review-20261022-010101-d08fbbe701b042719ebe3c15ca05b7d5'
        $names = @('research-engine')
        New-ResumeFixture -Tag $tag -Names $names -OldImages @{ 'research-engine' = 'x' }
        $gate = New-ResumeGate
        Throws { Assert-ResumeAudit -Tag $tag -Names @('research-engine', 'frontend') -Deployments $script:ResumeDeployments -Gate $gate } "*does not match the saved review audit's component set*"
    }

    It 'rejects missing/inconsistent review evidence: a required audit file absent' {
        $tag = 'review-20261022-020202-e08fbbe701b042719ebe3c15ca05b7d6'
        $names = @('research-engine')
        New-ResumeFixture -Tag $tag -Names $names -OldImages @{ 'research-engine' = 'x' } -OmitObservations
        $gate = New-ResumeGate
        Throws { Assert-ResumeAudit -Tag $tag -Names $names -Deployments $script:ResumeDeployments -Gate $gate } '*Missing recovery audit: observations.jsonl*'
    }

    It 'rejects deployment drift since the interrupted attempt' {
        $tag = 'review-20261022-030303-f08fbbe701b042719ebe3c15ca05b7d7'
        $names = @('research-engine')
        New-ResumeFixture -Tag $tag -Names $names -OldImages @{ 'research-engine' = 'x' }
        $gate = New-ResumeGate
        $drifted = @{ 'research-engine' = New-ResumeDeployment 'research-engine' 'uid-research-engine' 'rv-DRIFTED' }
        Throws { Assert-ResumeAudit -Tag $tag -Names $names -Deployments $drifted -Gate $gate } '*Deployment drift since failed attempt*'
    }

    It 'rejects a saved isolation policy whose selector no longer matches the live deployment (isolation mismatch)' {
        $tag = 'review-20261022-040404-008fbbe701b042719ebe3c15ca05b7d8'
        $names = @('research-engine')
        New-ResumeFixture -Tag $tag -Names $names -OldImages @{ 'research-engine' = 'x' } -BadSelector
        $gate = New-ResumeGate
        Throws { Assert-ResumeAudit -Tag $tag -Names $names -Deployments $script:ResumeDeployments -Gate $gate } '*Saved isolation policy mismatch*'
    }

    It 'fails closed when scheduled or internal work (an active Job) is running' {
        $tag = 'review-20261022-050505-108fbbe701b042719ebe3c15ca05b7d9'
        $names = @('research-engine')
        New-ResumeFixture -Tag $tag -Names $names -OldImages @{ 'research-engine' = 'x' }
        $script:ActiveJobs = [pscustomobject]@{ items = @([pscustomobject]@{ kind = 'Job'; status = [pscustomobject]@{ active = 1 } }) }
        $gate = New-ResumeGate
        Throws { Assert-ResumeAudit -Tag $tag -Names $names -Deployments $script:ResumeDeployments -Gate $gate } '*Scheduled/internal work is active*'
    }

    It 'rejects a rollback-audit image mismatch (digest drift)' {
        $tag = 'review-20261022-060606-208fbbe701b042719ebe3c15ca05b7da'
        $names = @('research-engine')
        New-ResumeFixture -Tag $tag -Names $names -OldImages @{ 'research-engine' = 'recorded-image' }
        $script:RecoveryOldImages = @{ 'research-engine' = 'DIFFERENT-image' }
        $gate = New-ResumeGate
        Throws { Assert-ResumeAudit -Tag $tag -Names $names -Deployments $script:ResumeDeployments -Gate $gate } '*Rollback audit mismatch*'
    }

    It 'rejects a durable-state fingerprint that changed since the interrupted run''s own quiet period' {
        $tag = 'review-20261022-070707-308fbbe701b042719ebe3c15ca05b7db'
        $names = @('research-engine')
        New-ResumeFixture -Tag $tag -Names $names -OldImages @{ 'research-engine' = 'x' } -Fingerprint ('a' * 64)
        $gate = New-ResumeGate -Fingerprint ('c' * 64)
        Throws { Assert-ResumeAudit -Tag $tag -Names $names -Deployments $script:ResumeDeployments -Gate $gate } "*Durable-state fingerprint changed since the interrupted attempt*"
    }
}
