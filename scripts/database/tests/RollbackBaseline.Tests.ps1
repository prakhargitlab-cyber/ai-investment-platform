# Pester 5 safety tests for scripts/database/rollback-baseline.ps1.
#
# Covers the externalized, versioned, explicitly-approved rollback-baseline
# manifest that replaces the old hardcoded $rollback digest map in
# platform.ps1's protected deployment path. No test in this file reaches a
# real registry, Kubernetes, or database -- Get-VerifiedRegistryImage (the
# only external boundary this module calls) is stubbed in BeforeAll and
# replaced per-test with Mock, exactly like platform.ps1's own registry
# calls are mocked elsewhere in this test suite family.

Describe "rollback-baseline" {
    BeforeAll {
        $script:RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
        $script:ScriptUnderTest = Join-Path $script:RepoRoot "scripts\database\rollback-baseline.ps1"
        if (-not (Test-Path -LiteralPath $script:ScriptUnderTest)) {
            throw "rollback-baseline.ps1 not found: $script:ScriptUnderTest"
        }
        . $script:ScriptUnderTest

        # Get-VerifiedRegistryImage is defined in platform.ps1, not in this
        # module -- it is never dot-sourced here. Declare a stub so Pester's
        # Mock can override it per test without ever touching a real
        # registry, and so a forgotten-mock bug fails loud (stub throws)
        # rather than silently resolving anything.
        function Get-VerifiedRegistryImage {
            param($Name, $Reference)
            throw "Get-VerifiedRegistryImage stub called without a Mock in place for this test."
        }

        # Invoke-DeploymentKubectl is defined in platform.ps1 (every call
        # pinned to -context k3d-ai-investment-dev -n ai-investment) and is
        # never dot-sourced here either -- same throw-if-unmocked stub
        # pattern as Get-VerifiedRegistryImage above.
        function Invoke-DeploymentKubectl {
            param([string[]]$Arguments)
            throw "Invoke-DeploymentKubectl stub called without a Mock in place for this test."
        }

        function Invoke-DeploymentCommand {
            param($Program, [string[]]$Arguments)
            throw "Invoke-DeploymentCommand stub called without a Mock in place for this test."
        }

        # Assert-VerifiedPodImage is also defined in platform.ps1. Unlike the
        # two boundary functions above, it is pure validation logic with no
        # external I/O (it only compares a pod's imageID digest against the
        # set Get-VerifiedRegistryImage populated as a side effect in
        # $script:VerifiedDeploymentDigests), so the real algorithm is
        # reproduced verbatim here rather than stubbed-to-throw: tests mock
        # Get-VerifiedRegistryImage to populate $script:VerifiedDeploymentDigests
        # the same way the real function does, and this exercises the real
        # drift-detection semantics end-to-end.
        function Assert-VerifiedPodImage {
            param([string]$Expected, [string]$Actual)
            if ($Actual -notmatch "(sha256:[a-f0-9]{64})$" -or -not $script:VerifiedDeploymentDigests -or $Matches[1] -notin $script:VerifiedDeploymentDigests[$Expected]) { throw "Pod digest not in verified OCI image graph" }
        }

        # [ordered]@{} (System.Collections.Specialized.OrderedDictionary) does
        # not support .Clone() the way a plain Hashtable does on the Windows
        # PowerShell 5.1 this suite targets. This is a compatible, explicit
        # copy mechanism used everywhere a test needs an independent mutable
        # copy of a shared ordered fixture.
        function Copy-OrderedDictionary {
            param($Source)
            $copy = [ordered]@{}
            foreach ($key in $Source.Keys) { $copy[$key] = $Source[$key] }
            return $copy
        }
    }

    BeforeEach {
        $script:TestRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("rbtest-" + [guid]::NewGuid().ToString("N"))
        $script:BaselineDir = Join-Path $script:TestRoot "scripts\database\rollback-baseline"
        New-Item -ItemType Directory -Path $script:BaselineDir -Force | Out-Null
        $script:ManifestPath = Join-Path $script:BaselineDir "approved-rollback-baseline.json"
        $script:HistoryPath = Join-Path $script:BaselineDir "approved-rollback-baseline-history.jsonl"
        $script:RuntimeDir = Join-Path $script:TestRoot ".tmp"
        New-Item -ItemType Directory -Path $script:RuntimeDir -Force | Out-Null

        # Every digest below is a real, verified 64-character lowercase hex
        # string (sha256:<64 hex chars>) -- the exact shape both
        # Assert-ValidRollbackDigest and the candidate image-reference regex
        # in Approve-RollbackBaseline require.
        $script:ValidManifest = [ordered]@{
            schemaVersion   = 1
            baselineVersion = 1
            approvedAtUtc   = "2026-10-11T20:28:20Z"
            approvedBy      = "test-fixture"
            components      = [ordered]@{
                "api-gateway"       = "sha256:111111111111111111111111111111111111111111111111111111111111aaaa"
                "research-engine"   = "sha256:222222222222222222222222222222222222222222222222222222222222bbbb"
                "portfolio-service" = "sha256:333333333333333333333333333333333333333333333333333333333333cccc"
                "frontend"          = "sha256:444444444444444444444444444444444444444444444444444444444444dddd"
            }
        }
    }

    AfterEach {
        if (Test-Path -LiteralPath $script:TestRoot) { Remove-Item -LiteralPath $script:TestRoot -Recurse -Force -ErrorAction SilentlyContinue }
    }

    Context "Get-ApprovedRollbackBaseline (fail-closed manifest loading)" {
        It "throws when the manifest file does not exist" {
            { Get-ApprovedRollbackBaseline -ProjectRoot $script:TestRoot } | Should -Throw "*not found*"
        }

        It "throws on malformed JSON" {
            Set-Content -LiteralPath $script:ManifestPath -Value "{ not valid json" -Encoding UTF8
            { Get-ApprovedRollbackBaseline -ProjectRoot $script:TestRoot } | Should -Throw "*not valid JSON*"
        }

        It "throws when a required component digest is missing" {
            $manifest = Copy-OrderedDictionary $script:ValidManifest
            $components = [ordered]@{}
            foreach ($key in $script:ValidManifest.components.Keys) { if ($key -ne "frontend") { $components[$key] = $script:ValidManifest.components[$key] } }
            $manifest.components = $components
            $manifest | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $script:ManifestPath -Encoding UTF8
            { Get-ApprovedRollbackBaseline -ProjectRoot $script:TestRoot } | Should -Throw "*frontend*"
        }

        It "throws when a component digest is not a valid sha256 string" {
            $manifest = Copy-OrderedDictionary $script:ValidManifest
            $manifest.components = Copy-OrderedDictionary $script:ValidManifest.components
            $manifest.components.frontend = "not-a-digest"
            $manifest | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $script:ManifestPath -Encoding UTF8
            { Get-ApprovedRollbackBaseline -ProjectRoot $script:TestRoot } | Should -Throw "*valid sha256 digest*"
        }

        It "throws on an unsupported schemaVersion" {
            $manifest = Copy-OrderedDictionary $script:ValidManifest
            $manifest.schemaVersion = 99
            $manifest | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $script:ManifestPath -Encoding UTF8
            { Get-ApprovedRollbackBaseline -ProjectRoot $script:TestRoot } | Should -Throw "*schemaVersion*"
        }

        It "returns all four approved digests and metadata for a valid manifest" {
            $script:ValidManifest | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $script:ManifestPath -Encoding UTF8
            $result = Get-ApprovedRollbackBaseline -ProjectRoot $script:TestRoot
            $result.BaselineVersion | Should -Be 1
            $result.Components["api-gateway"] | Should -Be $script:ValidManifest.components["api-gateway"]
            $result.Components["frontend"] | Should -Be $script:ValidManifest.components["frontend"]
        }
        It "treats a manifest with no 'initContainers' field as having no init-container approvals (never falling back to the main component's digest)" {
            $script:ValidManifest | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $script:ManifestPath -Encoding UTF8
            $result = Get-ApprovedRollbackBaseline -ProjectRoot $script:TestRoot
            $result.InitContainers.ContainsKey("wait-for-research-migrations") | Should -Be $false
        }

        It "returns an independently-approved init-container digest when present, distinct from the main component digest" {
            $manifest = Copy-OrderedDictionary $script:ValidManifest
            $manifest.initContainers = [ordered]@{ "wait-for-research-migrations" = "sha256:" + ("7" * 60 + "dddd") }
            $manifest | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $script:ManifestPath -Encoding UTF8
            $result = Get-ApprovedRollbackBaseline -ProjectRoot $script:TestRoot
            $result.InitContainers["wait-for-research-migrations"] | Should -Be ("sha256:" + ("7" * 60 + "dddd"))
            $result.InitContainers["wait-for-research-migrations"] | Should -Not -Be $result.Components["research-engine"]
        }

        It "throws when an init-container digest is present but malformed" {
            $manifest = Copy-OrderedDictionary $script:ValidManifest
            $manifest.initContainers = [ordered]@{ "wait-for-research-migrations" = "not-a-digest" }
            $manifest | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $script:ManifestPath -Encoding UTF8
            { Get-ApprovedRollbackBaseline -ProjectRoot $script:TestRoot } | Should -Throw "*valid sha256 digest*"
        }
    }

    Context "Save-VerifiedRollbackSnapshot (pre-deploy snapshot)" {
        It "writes the supplied images to the given path as JSON" {
            $path = Join-Path $script:RuntimeDir "rollback-test.json"
            $images = @{ frontend = "ai-investment-registry:5000/ai-investment/frontend@sha256:444444444444444444444444444444444444444444444444444444444444dddd" }
            Save-VerifiedRollbackSnapshot -Path $path -Images $images
            (Test-Path -LiteralPath $path) | Should -Be $true
            $written = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
            $written.frontend | Should -Be $images.frontend
        }
    }

    Context "Save-RollbackBaselineApprovalCandidate (post-rollout evidence)" {
        It "throws when a selected component has no verified image" {
            $images = @{ frontend = "ai-investment-registry:5000/ai-investment/frontend@sha256:444444444444444444444444444444444444444444444444444444444444dddd" }
            { Save-RollbackBaselineApprovalCandidate -ProjectRoot $script:TestRoot -RuntimeDir $script:RuntimeDir -Tag "review-test" -Names @("frontend", "api-gateway") -Images $images } | Should -Throw "*api-gateway*"
        }

        It "writes a candidate file with the expected shape and returns its path" {
            $images = @{ frontend = "ai-investment-registry:5000/ai-investment/frontend@sha256:444444444444444444444444444444444444444444444444444444444444dddd" }
            $path = Save-RollbackBaselineApprovalCandidate -ProjectRoot $script:TestRoot -RuntimeDir $script:RuntimeDir -Tag "review-test" -Names @("frontend") -Images $images
            (Test-Path -LiteralPath $path) | Should -Be $true
            $written = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
            $written.tag | Should -Be "review-test"
            @($written.names) | Should -Be @("frontend")
            $written.images.frontend | Should -Be $images.frontend
        }
    }

    Context "Approve-RollbackBaseline (explicit, fail-closed advancement)" {
        BeforeEach {
            $script:ValidManifest | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $script:ManifestPath -Encoding UTF8
            $script:NewFrontendImage = "ai-investment-registry:5000/ai-investment/frontend@sha256:555555555555555555555555555555555555555555555555555555555555eeee"
            $script:ValidCandidate = [ordered]@{
                schemaVersion = 1
                tag           = "review-20261013-010101-abc"
                recordedAtUtc = "2026-10-13T20:00:00Z"
                names         = @("frontend")
                images        = @{ frontend = $script:NewFrontendImage }
            }
            $script:CandidatePath = Join-Path $script:RuntimeDir "rollback-baseline-candidate-review-test.json"
            $script:ValidCandidate | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $script:CandidatePath -Encoding UTF8
        }

        It "throws without -AuthorizeRollbackBaselineApproval, and never writes the manifest" {
            $before = Get-Content -LiteralPath $script:ManifestPath -Raw
            { Approve-RollbackBaseline -ProjectRoot $script:TestRoot -CandidatePath $script:CandidatePath } | Should -Throw "*AuthorizeRollbackBaselineApproval*"
            (Get-Content -LiteralPath $script:ManifestPath -Raw) | Should -Be $before
        }

        It "throws when the candidate file does not exist" {
            { Approve-RollbackBaseline -ProjectRoot $script:TestRoot -CandidatePath (Join-Path $script:RuntimeDir "missing.json") -AuthorizeRollbackBaselineApproval } | Should -Throw "*not found*"
        }

        It "throws on an unknown component name in the candidate" {
            $bad = Copy-OrderedDictionary $script:ValidCandidate
            $bad.names = @("not-a-real-component")
            $bad.images = @{ "not-a-real-component" = $script:NewFrontendImage }
            $badPath = Join-Path $script:RuntimeDir "bad-candidate.json"
            $bad | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $badPath -Encoding UTF8
            { Approve-RollbackBaseline -ProjectRoot $script:TestRoot -CandidatePath $badPath -AuthorizeRollbackBaselineApproval } | Should -Throw "*unknown component*"
        }

        It "REGISTRY FAILURE: propagates a Get-VerifiedRegistryImage failure and leaves the manifest untouched" {
            Mock Get-VerifiedRegistryImage { throw "Manifest unavailable" }
            $before = Get-Content -LiteralPath $script:ManifestPath -Raw
            { Approve-RollbackBaseline -ProjectRoot $script:TestRoot -CandidatePath $script:CandidatePath -AuthorizeRollbackBaselineApproval } | Should -Throw "*Manifest unavailable*"
            (Get-Content -LiteralPath $script:ManifestPath -Raw) | Should -Be $before
        }

        It "DRIFT: refuses to approve when the registry no longer resolves to the candidate's recorded image, and leaves the manifest untouched" {
            # Registry now resolves a DIFFERENT, but still well-formed, digest
            # than what the candidate recorded -- simulating the image having
            # been overwritten/retagged since the rollout.
            Mock Get-VerifiedRegistryImage { return "ai-investment-registry:5000/ai-investment/frontend@sha256:999999999999999999999999999999999999999999999999999999999999ffff" }
            $before = Get-Content -LiteralPath $script:ManifestPath -Raw
            { Approve-RollbackBaseline -ProjectRoot $script:TestRoot -CandidatePath $script:CandidatePath -AuthorizeRollbackBaselineApproval } | Should -Throw "*no longer matches the registry*"
            (Get-Content -LiteralPath $script:ManifestPath -Raw) | Should -Be $before
        }

        It "APPROVAL: advances the baseline version and updates only the touched component when re-verification matches" {
            Mock Get-VerifiedRegistryImage { return $script:NewFrontendImage }
            $result = Approve-RollbackBaseline -ProjectRoot $script:TestRoot -CandidatePath $script:CandidatePath -AuthorizeRollbackBaselineApproval
            $result.BaselineVersion | Should -Be 2
            $result.Components["frontend"] | Should -Be "sha256:555555555555555555555555555555555555555555555555555555555555eeee"
            # Untouched components must be preserved unchanged from the prior baseline.
            $result.Components["api-gateway"] | Should -Be $script:ValidManifest.components["api-gateway"]
            $result.Components["research-engine"] | Should -Be $script:ValidManifest.components["research-engine"]
            $result.Components["portfolio-service"] | Should -Be $script:ValidManifest.components["portfolio-service"]

            $onDisk = Get-Content -LiteralPath $script:ManifestPath -Raw | ConvertFrom-Json
            $onDisk.baselineVersion | Should -Be 2
            $onDisk.components.frontend | Should -Be "sha256:555555555555555555555555555555555555555555555555555555555555eeee"
        }

        It "ROLLBACK (history): appends the superseded manifest to the history file before overwriting it" {
            Mock Get-VerifiedRegistryImage { return $script:NewFrontendImage }
            (Test-Path -LiteralPath $script:HistoryPath) | Should -Be $false
            Approve-RollbackBaseline -ProjectRoot $script:TestRoot -CandidatePath $script:CandidatePath -AuthorizeRollbackBaselineApproval | Out-Null
            (Test-Path -LiteralPath $script:HistoryPath) | Should -Be $true
            $history = Get-Content -LiteralPath $script:HistoryPath -Raw
            $history | Should -Match "`"baselineVersion`":\s*1"
            $history | Should -Match ([regex]::Escape($script:ValidManifest.components["frontend"]))
        }

        It "INIT APPROVAL: re-verifies and approves an init-container image recorded in the candidate, independently of the main component" {
            $initImage = "ai-investment-registry:5000/ai-investment/research-engine@sha256:" + ("7" * 60 + "cccc")
            $candidateWithInit = Copy-OrderedDictionary $script:ValidCandidate
            $candidateWithInit.images = @{ frontend = $script:NewFrontendImage }
            $candidateWithInit.initImages = @{ "wait-for-research-migrations" = @{ component = "research-engine"; image = $initImage } }
            $path = Join-Path $script:RuntimeDir "rollback-baseline-candidate-with-init.json"
            $candidateWithInit | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $path -Encoding UTF8

            Mock Get-VerifiedRegistryImage {
                param($Name, $Reference)
                if ($Name -eq "research-engine") { return $initImage }
                return $script:NewFrontendImage
            }
            $result = Approve-RollbackBaseline -ProjectRoot $script:TestRoot -CandidatePath $path -AuthorizeRollbackBaselineApproval
            $result.InitContainers["wait-for-research-migrations"] | Should -Be ("sha256:" + ("7" * 60 + "cccc"))
            # The main research-engine component digest is untouched by this
            # init-only approval -- independent approval, independent storage.
            $result.Components["research-engine"] | Should -Be $script:ValidManifest.components["research-engine"]
        }

        It "PRESERVATION: carries forward a previously-approved init-container digest when the candidate does not touch it" {
            $manifestWithInit = Copy-OrderedDictionary $script:ValidManifest
            $manifestWithInit.initContainers = [ordered]@{ "wait-for-research-migrations" = "sha256:" + ("9" * 60 + "eeee") }
            $manifestWithInit | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $script:ManifestPath -Encoding UTF8

            Mock Get-VerifiedRegistryImage { return $script:NewFrontendImage }
            $result = Approve-RollbackBaseline -ProjectRoot $script:TestRoot -CandidatePath $script:CandidatePath -AuthorizeRollbackBaselineApproval
            # This candidate only approves 'frontend' -- the previously-approved
            # init-container digest must be carried forward unchanged, never
            # dropped, reset, or silently copied from anywhere else.
            $result.InitContainers["wait-for-research-migrations"] | Should -Be ("sha256:" + ("9" * 60 + "eeee"))

            $onDisk = Get-Content -LiteralPath $script:ManifestPath -Raw | ConvertFrom-Json
            $onDisk.initContainers."wait-for-research-migrations" | Should -Be ("sha256:" + ("9" * 60 + "eeee"))
        }
    }

    Context "Approve-RollbackBaselineReconciliation (direct-running-image reconciliation, no candidate)" {
        BeforeEach {
            $script:ValidManifest | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $script:ManifestPath -Encoding UTF8

            $script:GoodDigestHex = "6" * 60 + "aaaa"
            $script:GoodResolvedImage = "ai-investment-registry:5000/ai-investment/api-gateway@sha256:$script:GoodDigestHex"
            $script:DriftDigestHex = "7" * 60 + "bbbb"

            function script:New-FakeDeploymentJson {
                param([string]$Name, [int]$Replicas = 1, [int]$Generation = 3)
                # Realistic pod template spec -- Approve-RollbackBaselineReconciliation
                # dynamically looks up the targeted container by name on this
                # object (never assuming it exists), so the fixture must
                # declare one. Image is the same digest the mocked registry
                # and fake pods already use, so it reflects consistent state.
                return (@{
                    metadata = @{ name = $Name; namespace = "ai-investment"; generation = $Generation }
                    spec     = @{
                        replicas = $Replicas
                        selector = @{ matchLabels = @{ app = $Name } }
                        template = @{ spec = @{
                            containers     = @(@{ name = $Name; image = "ai-investment-registry:5000/ai-investment/$Name@sha256:$script:GoodDigestHex" })
                            initContainers = @()
                        } }
                    }
                    status   = @{ readyReplicas = $Replicas; observedGeneration = $Generation }
                } | ConvertTo-Json -Depth 10)
            }

            function script:New-FakePodsJson {
                param([string]$Name, [string[]]$ImageIDs)
                $items = @($ImageIDs | ForEach-Object {
                    @{
                        metadata = @{ name = "$Name-pod-$_"; deletionTimestamp = $null }
                        status   = @{ containerStatuses = @(@{ name = $Name; ready = $true; imageID = $_ }) }
                    }
                })
                return (@{ items = $items } | ConvertTo-Json -Depth 10)
            }

            # Default happy-path mocks; individual tests override as needed.
            Mock Invoke-DeploymentKubectl {
                param([string[]]$Arguments)
                if ($Arguments[1] -eq "deployment") { return (New-FakeDeploymentJson -Name "api-gateway" -Replicas 1) }
                return (New-FakePodsJson -Name "api-gateway" -ImageIDs @("registry/api-gateway@sha256:$script:GoodDigestHex"))
            }
            Mock Get-VerifiedRegistryImage {
                $script:VerifiedDeploymentDigests = @{ $script:GoodResolvedImage = @("sha256:$script:GoodDigestHex") }
                return $script:GoodResolvedImage
            }
        }

        It "AUTHORIZATION (context/namespace): refuses any work unless the exact approved context and namespace are given, and never touches the manifest" {
            $before = Get-Content -LiteralPath $script:ManifestPath -Raw
            {
                Approve-RollbackBaselineReconciliation -ProjectRoot $script:TestRoot -KubeContext "some-other-context" -KubeNamespace "ai-investment" `
                    -ComponentTags @{ "api-gateway" = "good-tag" } -AuthorizeRollbackBaselineReconciliation -ReconciliationReason "test"
            } | Should -Throw "*k3d-ai-investment-dev*"
            (Get-Content -LiteralPath $script:ManifestPath -Raw) | Should -Be $before
            Assert-MockCalled Invoke-DeploymentKubectl -Times 0
        }

        It "AUTHORIZATION (reason required): throws when -AuthorizeRollbackBaselineReconciliation is passed without a -ReconciliationReason, and never touches the manifest" {
            $before = Get-Content -LiteralPath $script:ManifestPath -Raw
            {
                Approve-RollbackBaselineReconciliation -ProjectRoot $script:TestRoot -KubeContext "k3d-ai-investment-dev" -KubeNamespace "ai-investment" `
                    -ComponentTags @{ "api-gateway" = "good-tag" } -AuthorizeRollbackBaselineReconciliation
            } | Should -Throw "*ReconciliationReason*"
            (Get-Content -LiteralPath $script:ManifestPath -Raw) | Should -Be $before
        }

        It "COMPONENT SCOPE: rejects an arbitrary/path-shaped key before any kubectl or registry call, and never touches the manifest" {
            # '../../etc/passwd' fails the '<component>' / '<component>/<initContainerName>'
            # key-format check itself (dots and multiple path segments are
            # not a valid component or container name) -- it never reaches
            # the known-component allowlist check below. This is a
            # stricter rejection than "unknown component", not a weaker one.
            $before = Get-Content -LiteralPath $script:ManifestPath -Raw
            {
                Approve-RollbackBaselineReconciliation -ProjectRoot $script:TestRoot -KubeContext "k3d-ai-investment-dev" -KubeNamespace "ai-investment" `
                    -ComponentTags @{ "../../etc/passwd" = "good-tag" } -AuthorizeRollbackBaselineReconciliation -ReconciliationReason "test"
            } | Should -Throw "*Invalid -ComponentTags key*"
            (Get-Content -LiteralPath $script:ManifestPath -Raw) | Should -Be $before
            Assert-MockCalled Invoke-DeploymentKubectl -Times 0
        }

        It "COMPONENT SCOPE: rejects a validly-shaped but unknown component name before any kubectl or registry call, and never touches the manifest" {
            $before = Get-Content -LiteralPath $script:ManifestPath -Raw
            {
                Approve-RollbackBaselineReconciliation -ProjectRoot $script:TestRoot -KubeContext "k3d-ai-investment-dev" -KubeNamespace "ai-investment" `
                    -ComponentTags @{ "not-a-real-service" = "good-tag" } -AuthorizeRollbackBaselineReconciliation -ReconciliationReason "test"
            } | Should -Throw "*Unknown component*"
            (Get-Content -LiteralPath $script:ManifestPath -Raw) | Should -Be $before
            Assert-MockCalled Invoke-DeploymentKubectl -Times 0
        }

        It "COMPONENT SCOPE: rejects an invalid registry tag shape before any kubectl or registry call" {
            $before = Get-Content -LiteralPath $script:ManifestPath -Raw
            {
                Approve-RollbackBaselineReconciliation -ProjectRoot $script:TestRoot -KubeContext "k3d-ai-investment-dev" -KubeNamespace "ai-investment" `
                    -ComponentTags @{ "api-gateway" = "not a valid tag!" } -AuthorizeRollbackBaselineReconciliation -ReconciliationReason "test"
            } | Should -Throw "*Invalid registry tag*"
            (Get-Content -LiteralPath $script:ManifestPath -Raw) | Should -Be $before
            Assert-MockCalled Invoke-DeploymentKubectl -Times 0
        }

        It "REGISTRY FAILURE: propagates a Get-VerifiedRegistryImage failure and leaves the manifest untouched" {
            Mock Get-VerifiedRegistryImage { throw "Manifest unavailable" }
            $before = Get-Content -LiteralPath $script:ManifestPath -Raw
            {
                Approve-RollbackBaselineReconciliation -ProjectRoot $script:TestRoot -KubeContext "k3d-ai-investment-dev" -KubeNamespace "ai-investment" `
                    -ComponentTags @{ "api-gateway" = "good-tag" } -AuthorizeRollbackBaselineReconciliation -ReconciliationReason "test"
            } | Should -Throw "*Manifest unavailable*"
            (Get-Content -LiteralPath $script:ManifestPath -Raw) | Should -Be $before
        }

        It "IMAGE DRIFT: refuses when a running pod's imageID digest does not match the freshly-resolved registry digest, and leaves the manifest untouched" {
            Mock Invoke-DeploymentKubectl {
                param([string[]]$Arguments)
                if ($Arguments[1] -eq "deployment") { return (New-FakeDeploymentJson -Name "api-gateway" -Replicas 1) }
                return (New-FakePodsJson -Name "api-gateway" -ImageIDs @("registry/api-gateway@sha256:$script:DriftDigestHex"))
            }
            $before = Get-Content -LiteralPath $script:ManifestPath -Raw
            {
                Approve-RollbackBaselineReconciliation -ProjectRoot $script:TestRoot -KubeContext "k3d-ai-investment-dev" -KubeNamespace "ai-investment" `
                    -ComponentTags @{ "api-gateway" = "good-tag" } -AuthorizeRollbackBaselineReconciliation -ReconciliationReason "test"
            } | Should -Throw "*verified OCI image graph*"
            (Get-Content -LiteralPath $script:ManifestPath -Raw) | Should -Be $before
        }

        It "DRIFT (inconsistent replicas): refuses when the pod count under the exact selector does not match readyReplicas, and leaves the manifest untouched" {
            Mock Invoke-DeploymentKubectl {
                param([string[]]$Arguments)
                if ($Arguments[1] -eq "deployment") { return (New-FakeDeploymentJson -Name "api-gateway" -Replicas 2) }
                return (New-FakePodsJson -Name "api-gateway" -ImageIDs @("registry/api-gateway@sha256:$script:GoodDigestHex"))
            }
            $before = Get-Content -LiteralPath $script:ManifestPath -Raw
            {
                Approve-RollbackBaselineReconciliation -ProjectRoot $script:TestRoot -KubeContext "k3d-ai-investment-dev" -KubeNamespace "ai-investment" `
                    -ComponentTags @{ "api-gateway" = "good-tag" } -AuthorizeRollbackBaselineReconciliation -ReconciliationReason "test"
            } | Should -Throw "*inconsistent replicas*"
            (Get-Content -LiteralPath $script:ManifestPath -Raw) | Should -Be $before
        }

        It "PREVIEW: without -AuthorizeRollbackBaselineReconciliation, still fully re-verifies but never writes the manifest" {
            $before = Get-Content -LiteralPath $script:ManifestPath -Raw
            $result = Approve-RollbackBaselineReconciliation -ProjectRoot $script:TestRoot -KubeContext "k3d-ai-investment-dev" -KubeNamespace "ai-investment" `
                -ComponentTags @{ "api-gateway" = "good-tag" }
            $result.Previewed | Should -Be $true
            $result.Authorized | Should -Be $false
            $result.Components["api-gateway"] | Should -Be "sha256:$script:GoodDigestHex"
            (Get-Content -LiteralPath $script:ManifestPath -Raw) | Should -Be $before
        }

        It "SUCCESS (history-preserving reconciliation): advances the baseline, appends history, and updates only the selected component" {
            (Test-Path -LiteralPath $script:HistoryPath) | Should -Be $false
            $result = Approve-RollbackBaselineReconciliation -ProjectRoot $script:TestRoot -KubeContext "k3d-ai-investment-dev" -KubeNamespace "ai-investment" `
                -ComponentTags @{ "api-gateway" = "good-tag" } -AuthorizeRollbackBaselineReconciliation -ReconciliationReason "test: reconcile already-running image"

            $result.BaselineVersion | Should -Be 2
            $result.Components["api-gateway"] | Should -Be "sha256:$script:GoodDigestHex"
            # Untouched components must be preserved unchanged from the prior baseline.
            $result.Components["research-engine"] | Should -Be $script:ValidManifest.components["research-engine"]
            $result.Components["portfolio-service"] | Should -Be $script:ValidManifest.components["portfolio-service"]
            $result.Components["frontend"] | Should -Be $script:ValidManifest.components["frontend"]

            $onDisk = Get-Content -LiteralPath $script:ManifestPath -Raw | ConvertFrom-Json
            $onDisk.baselineVersion | Should -Be 2
            $onDisk.components."api-gateway" | Should -Be "sha256:$script:GoodDigestHex"

            (Test-Path -LiteralPath $script:HistoryPath) | Should -Be $true
            $history = Get-Content -LiteralPath $script:HistoryPath -Raw
            $history | Should -Match "`"baselineVersion`":\s*1"
            $history | Should -Match ([regex]::Escape($script:ValidManifest.components["api-gateway"]))
        }
    }

    Context "Invoke-InterruptedPromotionRecovery (one-time interrupted-promotion recovery)" {
        BeforeAll {
            # Invoke-DeploymentObservation is defined in platform.ps1, not in
            # this module, and is never dot-sourced here -- same
            # throw-if-unmocked stub pattern as the other platform.ps1
            # boundary functions above.
            function Invoke-DeploymentObservation {
                param($Gate)
                throw "Invoke-DeploymentObservation stub called without a Mock in place for this test."
            }

            $script:RecoveryTag = "review-20261021-210233-d3950bcdb04844a0b32078eeb5da4c24"
            $script:RecoveryMainDigest = ("a" * 64)
            $script:RecoveryInitDigest = ("b" * 64)
            $script:RecoveryMainImage = "ai-investment-registry:5000/ai-investment/research-engine@sha256:$script:RecoveryMainDigest"
            $script:RecoveryInitImage = "ai-investment-registry:5000/ai-investment/research-engine@sha256:$script:RecoveryInitDigest"
            $script:RecoveryFingerprint = ("f" * 64)
            $script:RecoveryDeploymentUid = "11111111-1111-1111-1111-111111111111"

            function script:New-RecoveryDeploymentJson {
                param(
                    $Generation = 5, $ObservedGeneration = 5, $Replicas = 1,
                    $Ready = 1, $Updated = 1, $Available = 1,
                    $MainImage = $script:RecoveryMainImage, $InitImage = $script:RecoveryInitImage,
                    $SchedulerEnabled = "false", $RecoveryPaused = "true",
                    $Uid = $script:RecoveryDeploymentUid
                )
                return (@{
                        metadata = @{ name = "research-engine"; uid = $Uid; generation = $Generation }
                        spec     = @{
                            replicas = $Replicas
                            template = @{
                                spec = @{
                                    containers     = @(@{
                                            name  = "research-engine"
                                            image = $MainImage
                                            env   = @(
                                                @{ name = "AIP_RESEARCH_OPPORTUNITY_SCHEDULER_ENABLED"; value = $SchedulerEnabled },
                                                @{ name = "AIP_RESEARCH_OPPORTUNITY_RECOVERY_PAUSED"; value = $RecoveryPaused }
                                            )
                                        })
                                    initContainers = @(@{ name = "wait-for-research-migrations"; image = $InitImage })
                                }
                            }
                        }
                        status   = @{ observedGeneration = $ObservedGeneration; readyReplicas = $Ready; updatedReplicas = $Updated; availableReplicas = $Available }
                    } | ConvertTo-Json -Depth 20 -Compress)
            }

            function script:New-RecoveryPodsJson {
                param(
                    $MainImageID = "registry/research-engine@sha256:$script:RecoveryMainDigest",
                    $InitImageID = "registry/research-engine@sha256:$script:RecoveryInitDigest",
                    $InitExitCode = 0, $Ready = $true, $DeletionTimestamp = $null
                )
                return (@{
                        items = @(@{
                                metadata = @{ name = "research-engine-pod-1"; labels = @{ "app.kubernetes.io/component" = "research-engine" }; deletionTimestamp = $DeletionTimestamp }
                                status   = @{
                                    containerStatuses     = @(@{ name = "research-engine"; ready = $Ready; imageID = $MainImageID })
                                    initContainerStatuses = @(@{ name = "wait-for-research-migrations"; imageID = $InitImageID; state = @{ terminated = @{ exitCode = $InitExitCode } } })
                                }
                            })
                    } | ConvertTo-Json -Depth 20 -Compress)
            }

            function script:New-RecoveryWorkloadsJson {
                param($UnrelatedReplicas = 1)
                return (@{
                        items = @(
                            @{ kind = "Deployment"; metadata = @{ name = "api-gateway" }; spec = @{ replicas = $UnrelatedReplicas } },
                            @{ kind = "Deployment"; metadata = @{ name = "research-engine" }; spec = @{ replicas = 1 } }
                        )
                    } | ConvertTo-Json -Depth 20 -Compress)
            }

            function script:New-RecoveryAuditFixture {
                # Writes the four saved review artifacts plus the
                # pre-deployment rollback snapshot, exactly as a real
                # Deploy-VerifiedRadarComponentsCore run would have left
                # them for this tag. Returns the audit directory path.
                param(
                    [switch]$OmitDeployments, [switch]$OmitWorkloads, [switch]$OmitIsolation, [switch]$OmitObservations,
                    [switch]$EmptyObservations, $Fingerprint = $script:RecoveryFingerprint,
                    $Uid = $script:RecoveryDeploymentUid, $UnrelatedReplicas = 1
                )
                $auditDir = Join-Path $script:RuntimeDir $script:RecoveryTag
                New-Item -ItemType Directory -Path $auditDir -Force | Out-Null

                if (-not $OmitDeployments) {
                    $record = @{ "research-engine" = (@{ metadata = @{ uid = $Uid } }) }
                    $record | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $auditDir "original-deployments.json") -Encoding UTF8
                }
                if (-not $OmitWorkloads) {
                    (New-RecoveryWorkloadsJson -UnrelatedReplicas $UnrelatedReplicas) | Set-Content -LiteralPath (Join-Path $auditDir "original-workloads.json") -Encoding UTF8
                }
                if (-not $OmitIsolation) {
                    (@{ metadata = @{ name = "research-bootstrap-isolation" } } | ConvertTo-Json -Depth 10) | Set-Content -LiteralPath (Join-Path $auditDir "isolation.json") -Encoding UTF8
                }
                if (-not $OmitObservations) {
                    if (-not $EmptyObservations) {
                        (@{ fingerprint = $Fingerprint; status = "QUIET_SNAPSHOT" } | ConvertTo-Json -Compress) | Set-Content -LiteralPath (Join-Path $auditDir "observations.jsonl") -Encoding UTF8
                    } else {
                        Set-Content -LiteralPath (Join-Path $auditDir "observations.jsonl") -Value "" -Encoding UTF8
                    }
                }
                $rollbackSnapshotPath = Join-Path $script:RuntimeDir "rollback-$script:RecoveryTag.json"
                @{ "research-engine" = "sha256:" + ("2" * 64) } | ConvertTo-Json | Set-Content -LiteralPath $rollbackSnapshotPath -Encoding UTF8
                return $auditDir
            }
        }

        BeforeEach {
            New-RecoveryAuditFixture | Out-Null

            # Default happy-path mocks; individual tests override as needed.
            Mock Invoke-DeploymentKubectl {
                param([string[]]$Arguments)
                if ($Arguments[0] -eq "get" -and $Arguments[1] -eq "networkpolicies") { return (@{ items = @() } | ConvertTo-Json -Depth 5 -Compress) }
                if ($Arguments[0] -eq "get" -and $Arguments[1] -eq "deployments,statefulsets") { return (New-RecoveryWorkloadsJson) }
                if ($Arguments[0] -eq "get" -and $Arguments[1] -eq "deployment") { return (New-RecoveryDeploymentJson) }
                if ($Arguments[0] -eq "get" -and $Arguments[1] -eq "pods") { return (New-RecoveryPodsJson) }
                throw "Unexpected Invoke-DeploymentKubectl call in this test: $($Arguments -join ' ')"
            }
            Mock Get-VerifiedRegistryImage {
                param($Name, $Reference)
                $script:VerifiedDeploymentDigests = @{
                    $script:RecoveryMainImage = @("sha256:$script:RecoveryMainDigest")
                    $script:RecoveryInitImage = @("sha256:$script:RecoveryInitDigest")
                }
                # $Reference is the "sha256:<64 hex>"-PREFIXED digest captured from
                # the running container's image string (see the regex in
                # Invoke-InterruptedPromotionRecovery), never the bare hex
                # digest -- compare against the prefixed form, exactly like
                # the real registry boundary receives it.
                if ($Reference -eq "sha256:$script:RecoveryMainDigest") { return $script:RecoveryMainImage }
                return $script:RecoveryInitImage
            }
            Mock Invoke-DeploymentObservation { return @{ fingerprint = $script:RecoveryFingerprint; status = "QUIET_SNAPSHOT" } }
        }

        It "PREVIEW: every check passes but no evidence file is written without -AuthorizeRecovery" {
            $auditDir = Join-Path $script:RuntimeDir $script:RecoveryTag
            $evidencePath = Join-Path $script:RuntimeDir "rollback-baseline-candidate-$script:RecoveryTag.json"
            $result = Invoke-InterruptedPromotionRecovery -ProjectRoot $script:TestRoot -RuntimeDir $script:RuntimeDir -Tag $script:RecoveryTag -Names @("research-engine")
            $result.EvidenceRecorded | Should -Be $false
            $result.VerifiedImages["research-engine"] | Should -Be $script:RecoveryMainImage
            (Test-Path -LiteralPath $evidencePath) | Should -Be $false
        }

        It "AUTHORIZATION: evidence is recorded only with -AuthorizeRecovery, in the schema Get-LatestVerifiedPromotionEvidence reads, without touching the approved rollback baseline" {
            $manifestBefore = Test-Path -LiteralPath $script:ManifestPath
            $result = Invoke-InterruptedPromotionRecovery -ProjectRoot $script:TestRoot -RuntimeDir $script:RuntimeDir -Tag $script:RecoveryTag -Names @("research-engine") -AuthorizeRecovery
            $result.EvidenceRecorded | Should -Be $true
            (Test-Path -LiteralPath $result.EvidencePath) | Should -Be $true
            $written = Get-Content -LiteralPath $result.EvidencePath -Raw | ConvertFrom-Json
            $written.schemaVersion | Should -Be 1
            $written.tag | Should -Be $script:RecoveryTag
            @($written.names) | Should -Be @("research-engine")
            $written.images."research-engine" | Should -Be $script:RecoveryMainImage
            $written.initImages."wait-for-research-migrations".image | Should -Be $script:RecoveryInitImage
            $written.origin | Should -Be "recovered-interrupted-promotion"
            (Test-Path -LiteralPath $script:ManifestPath) | Should -Be $manifestBefore

            # Get-LatestVerifiedPromotionEvidence (the real reader used during
            # a normal deploy's ARTIFACT_PREFLIGHT) must accept this file.
            $evidence = Get-LatestVerifiedPromotionEvidence -RuntimeDir $script:RuntimeDir -Name "research-engine"
            $evidence.Image | Should -Be $script:RecoveryMainImage
            $evidence.InitImage.Image | Should -Be $script:RecoveryInitImage
        }

        It "MAIN DIGEST MISMATCH: refuses when the running pod's main-image imageID does not match the independently-verified registry digest" {
            Mock Invoke-DeploymentKubectl {
                param([string[]]$Arguments)
                if ($Arguments[0] -eq "get" -and $Arguments[1] -eq "networkpolicies") { return (@{ items = @() } | ConvertTo-Json -Depth 5 -Compress) }
                if ($Arguments[0] -eq "get" -and $Arguments[1] -eq "deployments,statefulsets") { return (New-RecoveryWorkloadsJson) }
                if ($Arguments[0] -eq "get" -and $Arguments[1] -eq "deployment") { return (New-RecoveryDeploymentJson) }
                if ($Arguments[0] -eq "get" -and $Arguments[1] -eq "pods") { return (New-RecoveryPodsJson -MainImageID "registry/research-engine@sha256:$('9' * 64)") }
                throw "Unexpected call"
            }
            { Invoke-InterruptedPromotionRecovery -ProjectRoot $script:TestRoot -RuntimeDir $script:RuntimeDir -Tag $script:RecoveryTag -Names @("research-engine") -AuthorizeRecovery } | Should -Throw "*verified OCI image graph*"
            (Test-Path -LiteralPath (Join-Path $script:RuntimeDir "rollback-baseline-candidate-$script:RecoveryTag.json")) | Should -Be $false
        }

        It "INIT DIGEST MISMATCH: refuses when the migration-wait init container's imageID does not match the independently-verified registry digest" {
            Mock Invoke-DeploymentKubectl {
                param([string[]]$Arguments)
                if ($Arguments[0] -eq "get" -and $Arguments[1] -eq "networkpolicies") { return (@{ items = @() } | ConvertTo-Json -Depth 5 -Compress) }
                if ($Arguments[0] -eq "get" -and $Arguments[1] -eq "deployments,statefulsets") { return (New-RecoveryWorkloadsJson) }
                if ($Arguments[0] -eq "get" -and $Arguments[1] -eq "deployment") { return (New-RecoveryDeploymentJson) }
                if ($Arguments[0] -eq "get" -and $Arguments[1] -eq "pods") { return (New-RecoveryPodsJson -InitImageID "registry/research-engine@sha256:$('9' * 64)") }
                throw "Unexpected call"
            }
            { Invoke-InterruptedPromotionRecovery -ProjectRoot $script:TestRoot -RuntimeDir $script:RuntimeDir -Tag $script:RecoveryTag -Names @("research-engine") -AuthorizeRecovery } | Should -Throw "*verified OCI image graph*"
            (Test-Path -LiteralPath (Join-Path $script:RuntimeDir "rollback-baseline-candidate-$script:RecoveryTag.json")) | Should -Be $false
        }

        It "MISSING ARTIFACT: refuses when a required saved review artifact is absent, before any kubectl or registry call" {
            Remove-Item -LiteralPath (Join-Path $script:RuntimeDir $script:RecoveryTag) -Recurse -Force
            New-RecoveryAuditFixture -OmitIsolation | Out-Null
            { Invoke-InterruptedPromotionRecovery -ProjectRoot $script:TestRoot -RuntimeDir $script:RuntimeDir -Tag $script:RecoveryTag -Names @("research-engine") } | Should -Throw "*missing saved review artifact*isolation.json*"
            Assert-MockCalled Invoke-DeploymentKubectl -Times 0
        }

        It "MISSING ARTIFACT (observations): refuses when observations.jsonl records no durable-state samples" {
            Remove-Item -LiteralPath (Join-Path $script:RuntimeDir $script:RecoveryTag) -Recurse -Force
            New-RecoveryAuditFixture -EmptyObservations | Out-Null
            { Invoke-InterruptedPromotionRecovery -ProjectRoot $script:TestRoot -RuntimeDir $script:RuntimeDir -Tag $script:RecoveryTag -Names @("research-engine") } | Should -Throw "*records no durable-state samples*"
        }

        It "INCONSISTENT ARTIFACT: refuses when the saved deployment identity (uid) does not match the currently running deployment" {
            Remove-Item -LiteralPath (Join-Path $script:RuntimeDir $script:RecoveryTag) -Recurse -Force
            New-RecoveryAuditFixture -Uid "99999999-9999-9999-9999-999999999999" | Out-Null
            { Invoke-InterruptedPromotionRecovery -ProjectRoot $script:TestRoot -RuntimeDir $script:RuntimeDir -Tag $script:RecoveryTag -Names @("research-engine") } | Should -Throw "*deployment object identity*"
        }

        It "UNRELATED WORKLOAD DRIFT: refuses when a workload outside the recovered component set has changed since the interrupted attempt" {
            Remove-Item -LiteralPath (Join-Path $script:RuntimeDir $script:RecoveryTag) -Recurse -Force
            New-RecoveryAuditFixture -UnrelatedReplicas 1 | Out-Null
            Mock Invoke-DeploymentKubectl {
                param([string[]]$Arguments)
                if ($Arguments[0] -eq "get" -and $Arguments[1] -eq "networkpolicies") { return (@{ items = @() } | ConvertTo-Json -Depth 5 -Compress) }
                if ($Arguments[0] -eq "get" -and $Arguments[1] -eq "deployments,statefulsets") { return (New-RecoveryWorkloadsJson -UnrelatedReplicas 3) }
                if ($Arguments[0] -eq "get" -and $Arguments[1] -eq "deployment") { return (New-RecoveryDeploymentJson) }
                if ($Arguments[0] -eq "get" -and $Arguments[1] -eq "pods") { return (New-RecoveryPodsJson) }
                throw "Unexpected call"
            }
            { Invoke-InterruptedPromotionRecovery -ProjectRoot $script:TestRoot -RuntimeDir $script:RuntimeDir -Tag $script:RecoveryTag -Names @("research-engine") } | Should -Throw "*unrelated workload*drifted*"
        }

        It "DURABLE-STATE MISMATCH: refuses when the current fingerprint does not match the last fingerprint recorded during the interrupted run's own quiet period" {
            Mock Invoke-DeploymentObservation { return @{ fingerprint = ("9" * 64); status = "QUIET_SNAPSHOT" } }
            { Invoke-InterruptedPromotionRecovery -ProjectRoot $script:TestRoot -RuntimeDir $script:RuntimeDir -Tag $script:RecoveryTag -Names @("research-engine") } | Should -Throw "*durable-state fingerprint*does not match*"
        }

        It "ISOLATION STILL ACTIVE: refuses when a NetworkPolicy is still present, before any registry or database call" {
            Mock Invoke-DeploymentKubectl {
                param([string[]]$Arguments)
                if ($Arguments[0] -eq "get" -and $Arguments[1] -eq "networkpolicies") { return (@{ items = @(@{ metadata = @{ name = "research-bootstrap-isolation" } }) } | ConvertTo-Json -Depth 5 -Compress) }
                throw "Unexpected call: isolation is still active and must be rejected before any further lookup"
            }
            { Invoke-InterruptedPromotionRecovery -ProjectRoot $script:TestRoot -RuntimeDir $script:RuntimeDir -Tag $script:RecoveryTag -Names @("research-engine") } | Should -Throw "*NetworkPolicy is still present*"
            Assert-MockCalled Get-VerifiedRegistryImage -Times 0
            Assert-MockCalled Invoke-DeploymentObservation -Times 0
        }

        It "SUBSEQUENT NORMAL DEPLOYMENT: a recovered-evidence candidate is accepted by Get-LatestVerifiedPromotionEvidence exactly like a normally-recorded one" {
            Invoke-InterruptedPromotionRecovery -ProjectRoot $script:TestRoot -RuntimeDir $script:RuntimeDir -Tag $script:RecoveryTag -Names @("research-engine") -AuthorizeRecovery | Out-Null
            $evidence = Get-LatestVerifiedPromotionEvidence -RuntimeDir $script:RuntimeDir -Name "research-engine"
            $evidence | Should -Not -BeNullOrEmpty
            $evidence.Image | Should -Be $script:RecoveryMainImage
            $evidence.InitImage.ContainerName | Should -Be "wait-for-research-migrations"
        }
        Context "isolation still active after rollout" {
            # Narrow extension of the same recovery tool for a promotion
            # that reached rollout and generation/readiness, but was
            # interrupted before Complete-DeploymentSafety could remove
            # the isolation policy and record evidence (e.g. the
            # now-fixed StrictMode deletionTimestamp defect). The policy,
            # if still present, is trusted only when it verifiably
            # matches this interrupted attempt's own saved review
            # evidence -- never on age or presence alone -- and is
            # removed only under -AuthorizeRecovery, after every other
            # check (including the fresh durable-state fingerprint match)
            # has independently passed.
            BeforeAll {
                $script:IsolationSelector = @{ matchLabels = @{ "app.kubernetes.io/component" = "research-engine"; "app.kubernetes.io/instance" = "ai-investment-platform"; "app.kubernetes.io/name" = "ai-investment-platform" } }

                function script:New-ActiveIsolationFixture {
                    # Overwrites this tag's isolation.json with the FULL
                    # deployment-owned shape (podSelector/policyTypes/
                    # ingress), unlike the outer Context's minimal
                    # {metadata:{name:...}} fixture -- required so the
                    # live-vs-saved shape comparison this extension adds
                    # has something real to compare against.
                    $auditDir = Join-Path $script:RuntimeDir $script:RecoveryTag
                    (@{ metadata = @{ name = "research-bootstrap-isolation" }; spec = @{ podSelector = $script:IsolationSelector; policyTypes = @("Ingress"); ingress = @() } } | ConvertTo-Json -Depth 20) |
                        Set-Content -LiteralPath (Join-Path $auditDir "isolation.json") -Encoding UTF8
                }

                function script:New-MatchingLivePolicyJson {
                    return (@{ items = @(@{ metadata = @{ name = "research-bootstrap-isolation"; namespace = "ai-investment" }; spec = @{ podSelector = $script:IsolationSelector; policyTypes = @("Ingress"); ingress = @() } }) } | ConvertTo-Json -Depth 20 -Compress)
                }
            }

            BeforeEach {
                New-RecoveryAuditFixture | Out-Null
                New-ActiveIsolationFixture

                $script:DeletedPolicy = $false
                Mock Invoke-DeploymentKubectl {
                    param([string[]]$Arguments)
                    if ($Arguments[0] -eq "get" -and $Arguments[1] -eq "networkpolicies") { return (New-MatchingLivePolicyJson) }
                    if ($Arguments[0] -eq "get" -and $Arguments[1] -eq "jobs,cronjobs") { return (@{ items = @() } | ConvertTo-Json -Depth 5 -Compress) }
                    if ($Arguments[0] -eq "get" -and $Arguments[1] -eq "deployments,statefulsets") { return (New-RecoveryWorkloadsJson) }
                    if ($Arguments[0] -eq "get" -and $Arguments[1] -eq "deployment") { return (New-RecoveryDeploymentJson) }
                    if ($Arguments[0] -eq "get" -and $Arguments[1] -eq "pods") { return (New-RecoveryPodsJson) }
                    if ($Arguments[0] -eq "delete" -and $Arguments[1] -eq "networkpolicy") { $script:DeletedPolicy = $true; return "" }
                    throw "Unexpected Invoke-DeploymentKubectl call in this test: $($Arguments -join ' ')"
                }
                Mock Get-VerifiedRegistryImage {
                    param($Name, $Reference)
                    $script:VerifiedDeploymentDigests = @{
                        $script:RecoveryMainImage = @("sha256:$script:RecoveryMainDigest")
                        $script:RecoveryInitImage = @("sha256:$script:RecoveryInitDigest")
                    }
                    if ($Reference -eq "sha256:$script:RecoveryMainDigest") { return $script:RecoveryMainImage }
                    return $script:RecoveryInitImage
                }
                Mock Invoke-DeploymentObservation { return @{ fingerprint = $script:RecoveryFingerprint; status = "QUIET_SNAPSHOT" } }
                Mock Invoke-DeploymentCommand {
                    param($Program, [string[]]$Arguments)
                    if ($Program -eq "helm") { return (@{ name = "ai-investment-platform"; version = 3; info = @{ status = "deployed" } } | ConvertTo-Json -Compress) }
                    throw "Unexpected Invoke-DeploymentCommand call in this test: $Program"
                }
            }

            It "PREVIEW: isolation still active and verifiably matching -- verifies cleanly without removing it" {
                $result = Invoke-InterruptedPromotionRecovery -ProjectRoot $script:TestRoot -RuntimeDir $script:RuntimeDir -Tag $script:RecoveryTag -Names @("research-engine")
                $result.EvidenceRecorded | Should -Be $false
                $result.IsolationWasActive | Should -Be $true
                $result.IsolationRemoved | Should -Be $false
                $script:DeletedPolicy | Should -Be $false
            }

            It "AUTHORIZED: removes the exact matching isolation policy and records evidence for the already-running image" {
                $result = Invoke-InterruptedPromotionRecovery -ProjectRoot $script:TestRoot -RuntimeDir $script:RuntimeDir -Tag $script:RecoveryTag -Names @("research-engine") -AuthorizeRecovery
                $result.EvidenceRecorded | Should -Be $true
                $result.IsolationRemoved | Should -Be $true
                $script:DeletedPolicy | Should -Be $true
                $written = Get-Content -LiteralPath $result.EvidencePath -Raw | ConvertFrom-Json
                $written.images."research-engine" | Should -Be $script:RecoveryMainImage
                $evidence = Get-LatestVerifiedPromotionEvidence -RuntimeDir $script:RuntimeDir -Name "research-engine"
                $evidence.Image | Should -Be $script:RecoveryMainImage
            }

            It "FAILS CLOSED: an active Job blocks recovery and isolation is left untouched" {
                Mock Invoke-DeploymentKubectl {
                    param([string[]]$Arguments)
                    if ($Arguments[0] -eq "get" -and $Arguments[1] -eq "networkpolicies") { return (New-MatchingLivePolicyJson) }
                    if ($Arguments[0] -eq "get" -and $Arguments[1] -eq "jobs,cronjobs") { return (@{ items = @(@{ kind = "Job"; status = @{ active = 1 } }) } | ConvertTo-Json -Depth 10 -Compress) }
                    if ($Arguments[0] -eq "delete" -and $Arguments[1] -eq "networkpolicy") { $script:DeletedPolicy = $true; return "" }
                    throw "Unexpected call: active work must block recovery before any further lookup"
                }
                { Invoke-InterruptedPromotionRecovery -ProjectRoot $script:TestRoot -RuntimeDir $script:RuntimeDir -Tag $script:RecoveryTag -Names @("research-engine") -AuthorizeRecovery } | Should -Throw "*Scheduled/internal work is active*"
                $script:DeletedPolicy | Should -Be $false
            }

            It "FAILS CLOSED: a Helm release that is not in a settled 'deployed' state blocks recovery" {
                Mock Invoke-DeploymentCommand {
                    param($Program, [string[]]$Arguments)
                    if ($Program -eq "helm") { return (@{ name = "ai-investment-platform"; version = 3; info = @{ status = "pending-upgrade" } } | ConvertTo-Json -Compress) }
                    throw "Unexpected Invoke-DeploymentCommand call: $Program"
                }
                { Invoke-InterruptedPromotionRecovery -ProjectRoot $script:TestRoot -RuntimeDir $script:RuntimeDir -Tag $script:RecoveryTag -Names @("research-engine") -AuthorizeRecovery } | Should -Throw "*Helm release*settled*"
                $script:DeletedPolicy | Should -Be $false
            }

            It "FAILS CLOSED: a NetworkPolicy present but not matching this attempt's saved selector is refused, before registry/db calls" {
                Mock Invoke-DeploymentKubectl {
                    param([string[]]$Arguments)
                    if ($Arguments[0] -eq "get" -and $Arguments[1] -eq "networkpolicies") {
                        return (@{ items = @(@{ metadata = @{ name = "research-bootstrap-isolation"; namespace = "ai-investment" }; spec = @{ podSelector = @{ matchLabels = @{ "app.kubernetes.io/component" = "frontend" } }; policyTypes = @("Ingress"); ingress = @() } }) } | ConvertTo-Json -Depth 20 -Compress)
                    }
                    throw "Unexpected call: a mismatched isolation policy must be rejected before any further lookup"
                }
                { Invoke-InterruptedPromotionRecovery -ProjectRoot $script:TestRoot -RuntimeDir $script:RuntimeDir -Tag $script:RecoveryTag -Names @("research-engine") -AuthorizeRecovery } | Should -Throw "*does not verifiably match this interrupted attempt*"
                Assert-MockCalled Get-VerifiedRegistryImage -Times 0
                Assert-MockCalled Invoke-DeploymentObservation -Times 0
                $script:DeletedPolicy | Should -Be $false
            }

            Context "reviewed Helm revision-2 failure exception" {
                # This pre-existing, already-reviewed Helm failure predates
                # every review tag this recovery tool can be pointed at:
                # revision 2 of 'ai-investment-platform' failed at
                # 2026-10-21T17:48:10.6560239+02:00 due to a Kubernetes
                # server-side-apply field-ownership conflict. These tests
                # exercise Assert-ReviewedHelmFailureException through the
                # full Invoke-InterruptedPromotionRecovery call path, not in
                # isolation, so they also prove the other safeguards
                # (registry image identity, workload invariants, durable-
                # state fingerprint, active-work, NetworkPolicy identity)
                # are completely unaffected by this narrow Helm exception.
                BeforeAll {
                    # 'helm history -o json' records 'chart' as a flat
                    # "<name>-<version>" string per revision (used as-is
                    # below for history-entry fixtures). 'helm status
                    # -o json' reports NO chart identity at all -- confirmed
                    # against the real cluster, its top-level properties are
                    # only name, info, config, manifest, version, namespace,
                    # apply_method -- so chart identity is independently
                    # verified via Helm's own dedicated 'helm get metadata'
                    # command instead, which reports 'chart' and 'version'
                    # as separate flat strings. $script:HelmChartName /
                    # $script:HelmChartVersion mirror that real 'helm get
                    # metadata' shape; $script:HelmChart is the combined
                    # "<name>-<version>" string history uses, built from the
                    # same two pieces for a like-for-like comparison.
                    $script:HelmChartName = "ai-investment-platform"
                    $script:HelmChartVersion = "1.4.0"
                    $script:HelmChart = "$script:HelmChartName-$script:HelmChartVersion"
                    $script:HelmFailureTimestamp = "2026-10-21T17:48:10.6560239+02:00"

                    function script:New-GoodHelmHistoryJson {
                        $history = @(
                            @{ revision = 1; status = "deployed"; updated = "2026-10-15T09:00:00.0000000+02:00"; chart = $script:HelmChart; description = "Upgrade complete" },
                            @{ revision = 2; status = "failed"; updated = $script:HelmFailureTimestamp; chart = $script:HelmChart; description = 'Upgrade "ai-investment-platform" failed: conflict occurred while applying object ai-investment/research-engine apps/v1, Kind=Deployment: Apply failed with 2 conflicts: conflicts with "kubectl-patch" using apps/v1: .spec.template.spec.containers[name="research-engine"].image, .spec.template.spec.initContainers[name="wait-for-research-migrations"].image' }
                        )
                        return ($history | ConvertTo-Json -Depth 10 -Compress)
                    }

                    function script:New-HelmMetadataJson {
                        # Builds a 'helm get metadata' response. Defaults
                        # match the one reviewed revision-2 failure; any
                        # parameter can be overridden to build a mismatched
                        # or malformed fixture for a specific REJECTS test.
                        param(
                            $Name = "ai-investment-platform",
                            $Chart = $script:HelmChartName,
                            $Version = $script:HelmChartVersion,
                            $Revision = 2,
                            $Status = "failed",
                            [switch]$OmitChart,
                            [switch]$OmitVersion
                        )
                        $metadata = [ordered]@{
                            name      = $Name
                            revision  = $Revision
                            status    = $Status
                            namespace = "ai-investment"
                        }
                        if (-not $OmitChart) { $metadata["chart"] = $Chart }
                        if (-not $OmitVersion) { $metadata["version"] = $Version }
                        return ($metadata | ConvertTo-Json -Depth 10 -Compress)
                    }
                }

                BeforeEach {
                    Mock Invoke-DeploymentCommand {
                        param($Program, [string[]]$Arguments)
                        if ($Program -eq "helm" -and $Arguments -contains "status") {
                            return (@{ name = "ai-investment-platform"; version = 2; info = @{ status = "failed" } } | ConvertTo-Json -Depth 10 -Compress)
                        }
                        if ($Program -eq "helm" -and $Arguments -contains "history") { return (New-GoodHelmHistoryJson) }
                        if ($Program -eq "helm" -and $Arguments -contains "get" -and $Arguments -contains "metadata") { return (New-HelmMetadataJson) }
                        throw "Unexpected Invoke-DeploymentCommand call: $Program $($Arguments -join ' ')"
                    }
                }

                It "ACCEPTS: the exact reviewed revision-2 failure lets recovery proceed and record evidence" {
                    $result = Invoke-InterruptedPromotionRecovery -ProjectRoot $script:TestRoot -RuntimeDir $script:RuntimeDir -Tag $script:RecoveryTag -Names @("research-engine") -AuthorizeRecovery
                    $result.EvidenceRecorded | Should -Be $true
                    $result.IsolationRemoved | Should -Be $true
                    $script:DeletedPolicy | Should -Be $true
                }

                It "REJECTS: revision 2 is no longer the latest (superseded by a later attempt)" {
                    Mock Invoke-DeploymentCommand {
                        param($Program, [string[]]$Arguments)
                        if ($Program -eq "helm" -and $Arguments -contains "status") {
                            return (@{ name = "ai-investment-platform"; version = 2; info = @{ status = "failed" } } | ConvertTo-Json -Depth 10 -Compress)
                        }
                        if ($Program -eq "helm" -and $Arguments -contains "history") {
                            $history = @(
                                @{ revision = 1; status = "deployed"; updated = "2026-10-15T09:00:00.0000000+02:00"; chart = $script:HelmChart; description = "Upgrade complete" },
                                @{ revision = 2; status = "superseded"; updated = $script:HelmFailureTimestamp; chart = $script:HelmChart; description = "field manager conflict" },
                                @{ revision = 3; status = "pending-upgrade"; updated = "2026-10-22T08:00:00.0000000+02:00"; chart = $script:HelmChart; description = "Upgrade in progress" }
                            )
                            return ($history | ConvertTo-Json -Depth 10 -Compress)
                        }
                        throw "Unexpected call: revision is no longer latest must be rejected before any chart-identity lookup"
                    }
                    { Invoke-InterruptedPromotionRecovery -ProjectRoot $script:TestRoot -RuntimeDir $script:RuntimeDir -Tag $script:RecoveryTag -Names @("research-engine") -AuthorizeRecovery } | Should -Throw "*no longer the latest revision*"
                    $script:DeletedPolicy | Should -Be $false
                }

                It "REJECTS: an unknown/new Helm failure at a different revision" {
                    Mock Invoke-DeploymentCommand {
                        param($Program, [string[]]$Arguments)
                        if ($Program -eq "helm" -and $Arguments -contains "status") {
                            return (@{ name = "ai-investment-platform"; version = 4; info = @{ status = "failed" } } | ConvertTo-Json -Depth 10 -Compress)
                        }
                        throw "Unexpected call: an unknown failure must be rejected before consulting Helm history or chart identity"
                    }
                    { Invoke-InterruptedPromotionRecovery -ProjectRoot $script:TestRoot -RuntimeDir $script:RuntimeDir -Tag $script:RecoveryTag -Names @("research-engine") -AuthorizeRecovery } | Should -Throw "*does not match the one reviewed historical exception*"
                    $script:DeletedPolicy | Should -Be $false
                }

                It "REJECTS: a timestamp that does not match the reviewed failure" {
                    Mock Invoke-DeploymentCommand {
                        param($Program, [string[]]$Arguments)
                        if ($Program -eq "helm" -and $Arguments -contains "status") {
                            return (@{ name = "ai-investment-platform"; version = 2; info = @{ status = "failed" } } | ConvertTo-Json -Depth 10 -Compress)
                        }
                        if ($Program -eq "helm" -and $Arguments -contains "history") {
                            $history = @(
                                @{ revision = 1; status = "deployed"; updated = "2026-10-15T09:00:00.0000000+02:00"; chart = $script:HelmChart; description = "Upgrade complete" },
                                @{ revision = 2; status = "failed"; updated = "2026-10-21T18:00:00.0000000+02:00"; chart = $script:HelmChart; description = "field manager conflict" }
                            )
                            return ($history | ConvertTo-Json -Depth 10 -Compress)
                        }
                        throw "Unexpected call: a timestamp mismatch must be rejected before any chart-identity lookup"
                    }
                    { Invoke-InterruptedPromotionRecovery -ProjectRoot $script:TestRoot -RuntimeDir $script:RuntimeDir -Tag $script:RecoveryTag -Names @("research-engine") -AuthorizeRecovery } | Should -Throw "*unexpected or unparseable timestamp*"
                    $script:DeletedPolicy | Should -Be $false
                }

                It "REJECTS: a description that does not match the server-side-apply field-conflict signature" {
                    Mock Invoke-DeploymentCommand {
                        param($Program, [string[]]$Arguments)
                        if ($Program -eq "helm" -and $Arguments -contains "status") {
                            return (@{ name = "ai-investment-platform"; version = 2; info = @{ status = "failed" } } | ConvertTo-Json -Depth 10 -Compress)
                        }
                        if ($Program -eq "helm" -and $Arguments -contains "history") {
                            $history = @(
                                @{ revision = 1; status = "deployed"; updated = "2026-10-15T09:00:00.0000000+02:00"; chart = $script:HelmChart; description = "Upgrade complete" },
                                @{ revision = 2; status = "failed"; updated = $script:HelmFailureTimestamp; chart = $script:HelmChart; description = "image pull backoff" }
                            )
                            return ($history | ConvertTo-Json -Depth 10 -Compress)
                        }
                        if ($Program -eq "helm" -and $Arguments -contains "get" -and $Arguments -contains "metadata") { return (New-HelmMetadataJson) }
                        throw "Unexpected call"
                    }
                    { Invoke-InterruptedPromotionRecovery -ProjectRoot $script:TestRoot -RuntimeDir $script:RuntimeDir -Tag $script:RecoveryTag -Names @("research-engine") -AuthorizeRecovery } | Should -Throw "*missing required server-side-apply conflict signature markers*"
                    $script:DeletedPolicy | Should -Be $false
                }

                It "ACCEPTS: the real, actual recorded description (conflict wording with no literal English word 'field')" {
                    # Confirmed live against the real cluster: this Helm/
                    # Kubernetes version's conflict description never emits
                    # the literal word 'field' anywhere -- an earlier version
                    # of this check required that word and was therefore
                    # rejecting this exact, genuinely-matching historical
                    # failure. The real text instead names the conflicting
                    # object, the owning field manager ('kubectl-patch'),
                    # and the specific field paths -- this is what the
                    # corrected signature check requires instead.
                    # New-GoodHelmHistoryJson's revision-2 description (set
                    # up in BeforeAll) already IS this exact real text, so
                    # this uses the ordinary default mock to confirm it.
                    $result = Invoke-InterruptedPromotionRecovery -ProjectRoot $script:TestRoot -RuntimeDir $script:RuntimeDir -Tag $script:RecoveryTag -Names @("research-engine") -AuthorizeRecovery
                    $result.EvidenceRecorded | Should -Be $true
                    $result.IsolationRemoved | Should -Be $true
                    $script:DeletedPolicy | Should -Be $true
                }

                It "REJECTS: a generic message that mentions only 'conflict', with none of the other required signature markers" {
                    Mock Invoke-DeploymentCommand {
                        param($Program, [string[]]$Arguments)
                        if ($Program -eq "helm" -and $Arguments -contains "status") {
                            return (@{ name = "ai-investment-platform"; version = 2; info = @{ status = "failed" } } | ConvertTo-Json -Depth 10 -Compress)
                        }
                        if ($Program -eq "helm" -and $Arguments -contains "history") {
                            $history = @(
                                @{ revision = 1; status = "deployed"; updated = "2026-10-15T09:00:00.0000000+02:00"; chart = $script:HelmChart; description = "Upgrade complete" },
                                @{ revision = 2; status = "failed"; updated = $script:HelmFailureTimestamp; chart = $script:HelmChart; description = "Upgrade failed: a generic conflict occurred" }
                            )
                            return ($history | ConvertTo-Json -Depth 10 -Compress)
                        }
                        if ($Program -eq "helm" -and $Arguments -contains "get" -and $Arguments -contains "metadata") { return (New-HelmMetadataJson) }
                        throw "Unexpected call"
                    }
                    { Invoke-InterruptedPromotionRecovery -ProjectRoot $script:TestRoot -RuntimeDir $script:RuntimeDir -Tag $script:RecoveryTag -Names @("research-engine") -AuthorizeRecovery } | Should -Throw "*missing required server-side-apply conflict signature markers*"
                    $script:DeletedPolicy | Should -Be $false
                }

                It "REJECTS: the real conflict wording but with an altered resource name in the field path" {
                    Mock Invoke-DeploymentCommand {
                        param($Program, [string[]]$Arguments)
                        if ($Program -eq "helm" -and $Arguments -contains "status") {
                            return (@{ name = "ai-investment-platform"; version = 2; info = @{ status = "failed" } } | ConvertTo-Json -Depth 10 -Compress)
                        }
                        if ($Program -eq "helm" -and $Arguments -contains "history") {
                            $alteredDescription = 'Upgrade "ai-investment-platform" failed: conflict occurred while applying object ai-investment/research-engine apps/v1, Kind=Deployment: Apply failed with 2 conflicts: conflicts with "kubectl-patch" using apps/v1: .spec.template.spec.containers[name="research-engine"].image, .spec.template.spec.initContainers[name="wait-for-other-migrations"].image'
                            $history = @(
                                @{ revision = 1; status = "deployed"; updated = "2026-10-15T09:00:00.0000000+02:00"; chart = $script:HelmChart; description = "Upgrade complete" },
                                @{ revision = 2; status = "failed"; updated = $script:HelmFailureTimestamp; chart = $script:HelmChart; description = $alteredDescription }
                            )
                            return ($history | ConvertTo-Json -Depth 10 -Compress)
                        }
                        if ($Program -eq "helm" -and $Arguments -contains "get" -and $Arguments -contains "metadata") { return (New-HelmMetadataJson) }
                        throw "Unexpected call"
                    }
                    { Invoke-InterruptedPromotionRecovery -ProjectRoot $script:TestRoot -RuntimeDir $script:RuntimeDir -Tag $script:RecoveryTag -Names @("research-engine") -AuthorizeRecovery } | Should -Throw "*missing required server-side-apply conflict signature markers*"
                    $script:DeletedPolicy | Should -Be $false
                }

                It "REJECTS: the real conflict wording but missing one of the two required field paths" {
                    Mock Invoke-DeploymentCommand {
                        param($Program, [string[]]$Arguments)
                        if ($Program -eq "helm" -and $Arguments -contains "status") {
                            return (@{ name = "ai-investment-platform"; version = 2; info = @{ status = "failed" } } | ConvertTo-Json -Depth 10 -Compress)
                        }
                        if ($Program -eq "helm" -and $Arguments -contains "history") {
                            $missingPathDescription = 'Upgrade "ai-investment-platform" failed: conflict occurred while applying object ai-investment/research-engine apps/v1, Kind=Deployment: Apply failed with 1 conflict: conflicts with "kubectl-patch" using apps/v1: .spec.template.spec.containers[name="research-engine"].image'
                            $history = @(
                                @{ revision = 1; status = "deployed"; updated = "2026-10-15T09:00:00.0000000+02:00"; chart = $script:HelmChart; description = "Upgrade complete" },
                                @{ revision = 2; status = "failed"; updated = $script:HelmFailureTimestamp; chart = $script:HelmChart; description = $missingPathDescription }
                            )
                            return ($history | ConvertTo-Json -Depth 10 -Compress)
                        }
                        if ($Program -eq "helm" -and $Arguments -contains "get" -and $Arguments -contains "metadata") { return (New-HelmMetadataJson) }
                        throw "Unexpected call"
                    }
                    { Invoke-InterruptedPromotionRecovery -ProjectRoot $script:TestRoot -RuntimeDir $script:RuntimeDir -Tag $script:RecoveryTag -Names @("research-engine") -AuthorizeRecovery } | Should -Throw "*missing required server-side-apply conflict signature markers*"
                    $script:DeletedPolicy | Should -Be $false
                }

                It "REJECTS: the real conflict wording but against a different workload object (not Research Engine's Deployment)" {
                    Mock Invoke-DeploymentCommand {
                        param($Program, [string[]]$Arguments)
                        if ($Program -eq "helm" -and $Arguments -contains "status") {
                            return (@{ name = "ai-investment-platform"; version = 2; info = @{ status = "failed" } } | ConvertTo-Json -Depth 10 -Compress)
                        }
                        if ($Program -eq "helm" -and $Arguments -contains "history") {
                            $otherWorkloadDescription = 'Upgrade "ai-investment-platform" failed: conflict occurred while applying object ai-investment/portfolio-api apps/v1, Kind=Deployment: Apply failed with 2 conflicts: conflicts with "kubectl-patch" using apps/v1: .spec.template.spec.containers[name="research-engine"].image, .spec.template.spec.initContainers[name="wait-for-research-migrations"].image'
                            $history = @(
                                @{ revision = 1; status = "deployed"; updated = "2026-10-15T09:00:00.0000000+02:00"; chart = $script:HelmChart; description = "Upgrade complete" },
                                @{ revision = 2; status = "failed"; updated = $script:HelmFailureTimestamp; chart = $script:HelmChart; description = $otherWorkloadDescription }
                            )
                            return ($history | ConvertTo-Json -Depth 10 -Compress)
                        }
                        if ($Program -eq "helm" -and $Arguments -contains "get" -and $Arguments -contains "metadata") { return (New-HelmMetadataJson) }
                        throw "Unexpected call"
                    }
                    { Invoke-InterruptedPromotionRecovery -ProjectRoot $script:TestRoot -RuntimeDir $script:RuntimeDir -Tag $script:RecoveryTag -Names @("research-engine") -AuthorizeRecovery } | Should -Throw "*missing required server-side-apply conflict signature markers*"
                    $script:DeletedPolicy | Should -Be $false
                }

                It "REJECTS: the real conflict wording but owned by a different field manager (not kubectl-patch)" {
                    Mock Invoke-DeploymentCommand {
                        param($Program, [string[]]$Arguments)
                        if ($Program -eq "helm" -and $Arguments -contains "status") {
                            return (@{ name = "ai-investment-platform"; version = 2; info = @{ status = "failed" } } | ConvertTo-Json -Depth 10 -Compress)
                        }
                        if ($Program -eq "helm" -and $Arguments -contains "history") {
                            $otherManagerDescription = 'Upgrade "ai-investment-platform" failed: conflict occurred while applying object ai-investment/research-engine apps/v1, Kind=Deployment: Apply failed with 2 conflicts: conflicts with "helm-controller" using apps/v1: .spec.template.spec.containers[name="research-engine"].image, .spec.template.spec.initContainers[name="wait-for-research-migrations"].image'
                            $history = @(
                                @{ revision = 1; status = "deployed"; updated = "2026-10-15T09:00:00.0000000+02:00"; chart = $script:HelmChart; description = "Upgrade complete" },
                                @{ revision = 2; status = "failed"; updated = $script:HelmFailureTimestamp; chart = $script:HelmChart; description = $otherManagerDescription }
                            )
                            return ($history | ConvertTo-Json -Depth 10 -Compress)
                        }
                        if ($Program -eq "helm" -and $Arguments -contains "get" -and $Arguments -contains "metadata") { return (New-HelmMetadataJson) }
                        throw "Unexpected call"
                    }
                    { Invoke-InterruptedPromotionRecovery -ProjectRoot $script:TestRoot -RuntimeDir $script:RuntimeDir -Tag $script:RecoveryTag -Names @("research-engine") -AuthorizeRecovery } | Should -Throw "*missing required server-side-apply conflict signature markers*"
                    $script:DeletedPolicy | Should -Be $false
                }

                It "REJECTS: a completely unrelated failure description (no conflict wording at all)" {
                    Mock Invoke-DeploymentCommand {
                        param($Program, [string[]]$Arguments)
                        if ($Program -eq "helm" -and $Arguments -contains "status") {
                            return (@{ name = "ai-investment-platform"; version = 2; info = @{ status = "failed" } } | ConvertTo-Json -Depth 10 -Compress)
                        }
                        if ($Program -eq "helm" -and $Arguments -contains "history") {
                            $history = @(
                                @{ revision = 1; status = "deployed"; updated = "2026-10-15T09:00:00.0000000+02:00"; chart = $script:HelmChart; description = "Upgrade complete" },
                                @{ revision = 2; status = "failed"; updated = $script:HelmFailureTimestamp; chart = $script:HelmChart; description = "Upgrade failed: ImagePullBackOff for research-engine" }
                            )
                            return ($history | ConvertTo-Json -Depth 10 -Compress)
                        }
                        if ($Program -eq "helm" -and $Arguments -contains "get" -and $Arguments -contains "metadata") { return (New-HelmMetadataJson) }
                        throw "Unexpected call"
                    }
                    { Invoke-InterruptedPromotionRecovery -ProjectRoot $script:TestRoot -RuntimeDir $script:RuntimeDir -Tag $script:RecoveryTag -Names @("research-engine") -AuthorizeRecovery } | Should -Throw "*missing required server-side-apply conflict signature markers*"
                    $script:DeletedPolicy | Should -Be $false
                }

                It "REJECTS: the real conflict wording but naming a different release in the upgrade-failure text" {
                    Mock Invoke-DeploymentCommand {
                        param($Program, [string[]]$Arguments)
                        if ($Program -eq "helm" -and $Arguments -contains "status") {
                            return (@{ name = "ai-investment-platform"; version = 2; info = @{ status = "failed" } } | ConvertTo-Json -Depth 10 -Compress)
                        }
                        if ($Program -eq "helm" -and $Arguments -contains "history") {
                            $otherReleaseDescription = 'Upgrade "some-other-release" failed: conflict occurred while applying object ai-investment/research-engine apps/v1, Kind=Deployment: Apply failed with 2 conflicts: conflicts with "kubectl-patch" using apps/v1: .spec.template.spec.containers[name="research-engine"].image, .spec.template.spec.initContainers[name="wait-for-research-migrations"].image'
                            $history = @(
                                @{ revision = 1; status = "deployed"; updated = "2026-10-15T09:00:00.0000000+02:00"; chart = $script:HelmChart; description = "Upgrade complete" },
                                @{ revision = 2; status = "failed"; updated = $script:HelmFailureTimestamp; chart = $script:HelmChart; description = $otherReleaseDescription }
                            )
                            return ($history | ConvertTo-Json -Depth 10 -Compress)
                        }
                        if ($Program -eq "helm" -and $Arguments -contains "get" -and $Arguments -contains "metadata") { return (New-HelmMetadataJson) }
                        throw "Unexpected call"
                    }
                    { Invoke-InterruptedPromotionRecovery -ProjectRoot $script:TestRoot -RuntimeDir $script:RuntimeDir -Tag $script:RecoveryTag -Names @("research-engine") -AuthorizeRecovery } | Should -Throw "*missing required server-side-apply conflict signature markers*"
                    $script:DeletedPolicy | Should -Be $false
                }

                It "Get-DeploymentHelmHistoryRevision: normalizes a 'revision' field regardless of how the runtime wraps it" {
                    # Reproduces the live defect this exception gate was found to have:
                    # some runtimes hand Helm history's 'revision' field back wrapped in a
                    # single-element array rather than a bare scalar int, which made the
                    # original direct-cast parsing report "latest: 0" and wrongly reject a
                    # genuinely-matching revision 2. This exercises the normalizer directly,
                    # independent of the Helm/JSON mocking above, since a JSON number can
                    # never itself produce this shape through ConvertTo-Json/ConvertFrom-Json.
                    (Get-DeploymentHelmHistoryRevision ([pscustomobject]@{ revision = @(2) })) | Should -Be 2
                    (Get-DeploymentHelmHistoryRevision ([pscustomobject]@{ revision = 2 })) | Should -Be 2
                    (Get-DeploymentHelmHistoryRevision (@{ revision = @(3) })) | Should -Be 3
                    (Get-DeploymentHelmHistoryRevision ([pscustomobject]@{ status = "deployed" }))  | Should -Be 0

                    $wrappedHistory = @(
                        [pscustomobject]@{ revision = @(1); status = "deployed" },
                        [pscustomobject]@{ revision = @(2); status = "failed" }
                    )
                    $latest = ($wrappedHistory | ForEach-Object { Get-DeploymentHelmHistoryRevision $_ } | Measure-Object -Maximum).Maximum
                    $latest | Should -Be 2
                    $matching = @($wrappedHistory | Where-Object { (Get-DeploymentHelmHistoryRevision $_) -eq 2 })
                    $matching.Count | Should -Be 1
                }

                It "Get-DeploymentNormalizedHelmHistory: ACCEPTS the exact reviewed revision-2 failure even when the raw parse result is the real Windows PowerShell NoEnumerate shape" {
                    # Reproduces the actual reported failure directly, isolating
                    # the parser's INPUT rather than mocking ConvertFrom-Json:
                    # on the real target, ConvertFrom-Json's array result
                    # reached the pipeline as a SINGLE NoEnumerate collection
                    # object rather than one object per entry. A plain
                    # assignment from such a pipeline ('$historyRaw = cmd |
                    # ConvertFrom-Json', as the fixed call site now does)
                    # binds $historyRaw directly to that one emitted object --
                    # which IS the real array -- with no extra wrapping; it
                    # is the OLD '@(cmd | ConvertFrom-Json)' pattern that used
                    # to add an unwanted extra array level on top of it
                    # (reproduced and rejected in the REJECTS test below, as
                    # the "old buggy shape"). This builds that real NoEnumerate
                    # shape directly in PowerShell and passes it straight to
                    # Get-DeploymentNormalizedHelmHistory as $HistoryRaw,
                    # exactly as it reaches the function after
                    # 'Invoke-DeploymentCommand ... | ConvertFrom-Json'. No
                    # mocking of ConvertFrom-Json, no recursion risk, and no
                    # dependency on which PowerShell/.NET build happens to
                    # reproduce -NoEnumerate pipeline emission.
                    function script:Get-NoEnumerateHelmHistory {
                        $realEntries = @(
                            [pscustomobject]@{ revision = 1; status = "deployed"; updated = "2026-10-15T09:00:00.0000000+02:00"; chart = $script:HelmChart; description = "Upgrade complete" },
                            [pscustomobject]@{ revision = 2; status = "failed"; updated = $script:HelmFailureTimestamp; chart = $script:HelmChart; description = 'Upgrade "ai-investment-platform" failed: conflict occurred while applying object ai-investment/research-engine apps/v1, Kind=Deployment: Apply failed with 2 conflicts: conflicts with "kubectl-patch" using apps/v1: .spec.template.spec.containers[name="research-engine"].image, .spec.template.spec.initContainers[name="wait-for-research-migrations"].image' }
                        )
                        Write-Output -NoEnumerate $realEntries
                    }
                    $historyRaw = Get-NoEnumerateHelmHistory
                    $historyRaw.Count | Should -Be 2
                    ,$historyRaw | Should -BeOfType [System.Object[]]

                    $flattened = Get-DeploymentNormalizedHelmHistory -HistoryRaw $historyRaw -Tag $script:RecoveryTag -ReleaseName "ai-investment-platform" -ExpectedRevision 2
                    $flattened.Count | Should -Be 2
                    $latest = ($flattened | ForEach-Object { Get-DeploymentHelmHistoryRevision $_ } | Measure-Object -Maximum).Maximum
                    $latest | Should -Be 2

                    # And the full recovery path accepts it end-to-end, via the
                    # ordinary Invoke-DeploymentCommand mock only (no
                    # ConvertFrom-Json override): this confirms the normal
                    # path still works unchanged alongside the direct
                    # defect-shape test above.
                    $result = Invoke-InterruptedPromotionRecovery -ProjectRoot $script:TestRoot -RuntimeDir $script:RuntimeDir -Tag $script:RecoveryTag -Names @("research-engine") -AuthorizeRecovery
                    $result.EvidenceRecorded | Should -Be $true
                    $result.IsolationRemoved | Should -Be $true
                    $script:DeletedPolicy | Should -Be $true
                }

                It "Get-DeploymentNormalizedHelmHistory: REJECTS a history entry that is still an array after flattening, rather than silently accepting it" {
                    # Genuinely malformed shapes built directly in PowerShell
                    # (ConvertFrom-Json collapses nested singleton arrays, so
                    # plain JSON text cannot produce these), to prove the
                    # explicit entry-shape guard rejects anything @() doesn't
                    # fully flatten, instead of ever assuming one more unwrap
                    # is always safe. Isolated to the parser's input directly,
                    # same as the ACCEPTS case above -- no ConvertFrom-Json
                    # mocking involved.

                    # The OLD buggy pattern ('@(cmd | ConvertFrom-Json)' applied
                    # directly to a live pipeline, instead of to an
                    # already-bound variable) still double-wraps a NoEnumerate
                    # array; if anyone ever reintroduced that pattern at a call
                    # site, this proves the guard below catches it rather than
                    # silently unwrapping one more level.
                    function script:Get-NoEnumerateHelmHistory2 {
                        Write-Output -NoEnumerate @([pscustomobject]@{ revision = 1 }, [pscustomobject]@{ revision = 2 })
                    }
                    $oldBuggyShape = @(Get-NoEnumerateHelmHistory2)
                    $oldBuggyShape.Count | Should -Be 1
                    ,$oldBuggyShape[0] | Should -BeOfType [System.Object[]]
                    { Get-DeploymentNormalizedHelmHistory -HistoryRaw $oldBuggyShape -Tag $script:RecoveryTag -ReleaseName "ai-investment-platform" -ExpectedRevision 2 } | Should -Throw "*returned a malformed entry*"

                    # A still-nested entry that survives one @() flatten.
                    $stillNestedRaw = @(, @([pscustomobject]@{ revision = 2; status = "failed" }))
                    { Get-DeploymentNormalizedHelmHistory -HistoryRaw $stillNestedRaw -Tag $script:RecoveryTag -ReleaseName "ai-investment-platform" -ExpectedRevision 2 } | Should -Throw "*returned a malformed entry*"

                    # Genuinely empty history (zero entries) vs. a single
                    # explicit null "entry" (@($null) is a 1-element array
                    # containing $null, not an empty array) -- each rejected
                    # with its own distinct, specific message.
                    { Get-DeploymentNormalizedHelmHistory -HistoryRaw @() -Tag $script:RecoveryTag -ReleaseName "ai-investment-platform" -ExpectedRevision 2 } | Should -Throw "*is empty*"

                    { Get-DeploymentNormalizedHelmHistory -HistoryRaw $null -Tag $script:RecoveryTag -ReleaseName "ai-investment-platform" -ExpectedRevision 2 } | Should -Throw "*is empty*"

                    # And the full recovery path rejects the equivalent live
                    # failure end-to-end: 'helm history' returning a value
                    # that, after real parsing, still leaves a malformed
                    # entry in place.
                    Mock Invoke-DeploymentCommand {
                        param($Program, [string[]]$Arguments)
                        if ($Program -eq "helm" -and $Arguments -contains "status") {
                            return (@{ name = "ai-investment-platform"; version = 2; info = @{ status = "failed" } } | ConvertTo-Json -Depth 10 -Compress)
                        }
                        if ($Program -eq "helm" -and $Arguments -contains "history") {
                            # A single JSON object (not an array) parses via
                            # ConvertFrom-Json into one bare PSCustomObject;
                            # @() around that one object is a correctly-flat
                            # 1-element $history whose lone entry is NOT an
                            # array -- so instead this returns an EMPTY JSON
                            # array, the one shape that is both genuinely
                            # producible via real JSON and already covered
                            # by the empty-history fail-closed check.
                            return "[]"
                        }
                        throw "Unexpected call: an empty Helm history must be rejected before any chart-identity lookup"
                    }
                    { Invoke-InterruptedPromotionRecovery -ProjectRoot $script:TestRoot -RuntimeDir $script:RuntimeDir -Tag $script:RecoveryTag -Names @("research-engine") -AuthorizeRecovery } | Should -Throw "*is empty*"
                    $script:DeletedPolicy | Should -Be $false
                }

                It "ACCEPTS: chart identity independently verified via 'helm get metadata', not read from 'helm status' (which reports none)" {
                    # 'helm status -o json' reports NO chart identity at all
                    # on the real target (confirmed live: name, info, config,
                    # manifest, version, namespace, apply_method only -- no
                    # 'chart' key). An earlier attempt assumed a nested
                    # chart.metadata object existed there; it didn't, which is
                    # why the reported chart always compared as empty. The
                    # fix queries Helm's own dedicated 'helm get metadata'
                    # command instead. The BeforeEach default mock already
                    # exercises this path (status has no chart field, and a
                    # separate 'get metadata' mock supplies verified identity),
                    # so this just confirms the ordinary case keeps succeeding.
                    $result = Invoke-InterruptedPromotionRecovery -ProjectRoot $script:TestRoot -RuntimeDir $script:RuntimeDir -Tag $script:RecoveryTag -Names @("research-engine") -AuthorizeRecovery
                    $result.EvidenceRecorded | Should -Be $true
                    $result.IsolationRemoved | Should -Be $true
                    $script:DeletedPolicy | Should -Be $true
                }

                It "REJECTS: a 'helm get metadata' chart/version that doesn't match the reviewed history chart" {
                    Mock Invoke-DeploymentCommand {
                        param($Program, [string[]]$Arguments)
                        if ($Program -eq "helm" -and $Arguments -contains "status") {
                            return (@{ name = "ai-investment-platform"; version = 2; info = @{ status = "failed" } } | ConvertTo-Json -Depth 10 -Compress)
                        }
                        if ($Program -eq "helm" -and $Arguments -contains "history") { return (New-GoodHelmHistoryJson) }
                        if ($Program -eq "helm" -and $Arguments -contains "get" -and $Arguments -contains "metadata") { return (New-HelmMetadataJson -Version "9.9.9") }
                        throw "Unexpected call"
                    }
                    { Invoke-InterruptedPromotionRecovery -ProjectRoot $script:TestRoot -RuntimeDir $script:RuntimeDir -Tag $script:RecoveryTag -Names @("research-engine") -AuthorizeRecovery } | Should -Throw "*does not match the independently verified release chart*"
                    $script:DeletedPolicy | Should -Be $false
                }

                It "REJECTS: a 'helm get metadata' response missing its chart or version field (genuinely missing identity, not misread)" {
                    Mock Invoke-DeploymentCommand {
                        param($Program, [string[]]$Arguments)
                        if ($Program -eq "helm" -and $Arguments -contains "status") {
                            return (@{ name = "ai-investment-platform"; version = 2; info = @{ status = "failed" } } | ConvertTo-Json -Depth 10 -Compress)
                        }
                        if ($Program -eq "helm" -and $Arguments -contains "history") { return (New-GoodHelmHistoryJson) }
                        if ($Program -eq "helm" -and $Arguments -contains "get" -and $Arguments -contains "metadata") { return (New-HelmMetadataJson -OmitChart) }
                        throw "Unexpected call"
                    }
                    { Invoke-InterruptedPromotionRecovery -ProjectRoot $script:TestRoot -RuntimeDir $script:RuntimeDir -Tag $script:RecoveryTag -Names @("research-engine") -AuthorizeRecovery } | Should -Throw "*missing or has malformed name/chart/version fields*"
                    $script:DeletedPolicy | Should -Be $false
                }

                It "REJECTS: a 'helm get metadata' response reporting a different release name than expected" {
                    Mock Invoke-DeploymentCommand {
                        param($Program, [string[]]$Arguments)
                        if ($Program -eq "helm" -and $Arguments -contains "status") {
                            return (@{ name = "ai-investment-platform"; version = 2; info = @{ status = "failed" } } | ConvertTo-Json -Depth 10 -Compress)
                        }
                        if ($Program -eq "helm" -and $Arguments -contains "history") { return (New-GoodHelmHistoryJson) }
                        if ($Program -eq "helm" -and $Arguments -contains "get" -and $Arguments -contains "metadata") { return (New-HelmMetadataJson -Name "some-other-release") }
                        throw "Unexpected call"
                    }
                    { Invoke-InterruptedPromotionRecovery -ProjectRoot $script:TestRoot -RuntimeDir $script:RuntimeDir -Tag $script:RecoveryTag -Names @("research-engine") -AuthorizeRecovery } | Should -Throw "*reports release name*which does not match the expected release*"
                    $script:DeletedPolicy | Should -Be $false
                }

                It "REJECTS: a 'helm get metadata' response reporting a different revision or status than expected" {
                    Mock Invoke-DeploymentCommand {
                        param($Program, [string[]]$Arguments)
                        if ($Program -eq "helm" -and $Arguments -contains "status") {
                            return (@{ name = "ai-investment-platform"; version = 2; info = @{ status = "failed" } } | ConvertTo-Json -Depth 10 -Compress)
                        }
                        if ($Program -eq "helm" -and $Arguments -contains "history") { return (New-GoodHelmHistoryJson) }
                        if ($Program -eq "helm" -and $Arguments -contains "get" -and $Arguments -contains "metadata") { return (New-HelmMetadataJson -Revision 5) }
                        throw "Unexpected call"
                    }
                    { Invoke-InterruptedPromotionRecovery -ProjectRoot $script:TestRoot -RuntimeDir $script:RuntimeDir -Tag $script:RecoveryTag -Names @("research-engine") -AuthorizeRecovery } | Should -Throw "*reports revision*which does not match the expected revision*"
                    $script:DeletedPolicy | Should -Be $false

                    Mock Invoke-DeploymentCommand {
                        param($Program, [string[]]$Arguments)
                        if ($Program -eq "helm" -and $Arguments -contains "status") {
                            return (@{ name = "ai-investment-platform"; version = 2; info = @{ status = "failed" } } | ConvertTo-Json -Depth 10 -Compress)
                        }
                        if ($Program -eq "helm" -and $Arguments -contains "history") { return (New-GoodHelmHistoryJson) }
                        if ($Program -eq "helm" -and $Arguments -contains "get" -and $Arguments -contains "metadata") { return (New-HelmMetadataJson -Status "deployed") }
                        throw "Unexpected call"
                    }
                    { Invoke-InterruptedPromotionRecovery -ProjectRoot $script:TestRoot -RuntimeDir $script:RuntimeDir -Tag $script:RecoveryTag -Names @("research-engine") -AuthorizeRecovery } | Should -Throw "*reports status*which does not match the expected status*"
                    $script:DeletedPolicy | Should -Be $false
                }

                It "REJECTS: when the independent 'helm get metadata' command itself fails" {
                    Mock Invoke-DeploymentCommand {
                        param($Program, [string[]]$Arguments)
                        if ($Program -eq "helm" -and $Arguments -contains "status") {
                            return (@{ name = "ai-investment-platform"; version = 2; info = @{ status = "failed" } } | ConvertTo-Json -Depth 10 -Compress)
                        }
                        if ($Program -eq "helm" -and $Arguments -contains "history") { return (New-GoodHelmHistoryJson) }
                        if ($Program -eq "helm" -and $Arguments -contains "get" -and $Arguments -contains "metadata") { throw "helm: release not found" }
                        throw "Unexpected call"
                    }
                    { Invoke-InterruptedPromotionRecovery -ProjectRoot $script:TestRoot -RuntimeDir $script:RuntimeDir -Tag $script:RecoveryTag -Names @("research-engine") -AuthorizeRecovery } | Should -Throw "*'helm get metadata' for*failed*cannot independently verify chart identity*"
                    $script:DeletedPolicy | Should -Be $false
                }
            }
        }
    }
}
