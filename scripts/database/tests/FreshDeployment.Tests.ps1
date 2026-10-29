# Pester 5 tests. Run from repository root with: Invoke-Pester -Path scripts\database\tests\FreshDeployment.Tests.ps1
# Only AST-extracted functions from platform.ps1 run; no platform top-level
# code or real external command ever executes. All setup lives in BeforeAll
# so nothing executes during Pester's discovery phase -- only Describe/It
# registration happens then, and every deployment call happens inside It.

Describe 'Fresh DEV deployment risk classification and rollout' {
    BeforeAll {
        function Assert($Condition, $Message) { if (-not $Condition) { throw $Message } }
        function Throws([scriptblock]$Body, $Pattern) { try { & $Body } catch { Assert ($_.Exception.Message -like $Pattern) "Unexpected error: $($_.Exception.Message)"; return }; throw "Expected error: $Pattern" }

        $platformPath = Join-Path (Get-Location) 'platform.ps1'
        $tokens = $null; $errors = $null
        $ast = [System.Management.Automation.Language.Parser]::ParseFile($platformPath, [ref]$tokens, [ref]$errors)
        if ($errors.Count) { throw ($errors | Out-String) }
        $functions = @('Get-DeploymentMigrationFingerprint', 'Get-DeploymentHelmFingerprint',
            'Get-DeploymentFreshBaselinePath', 'Get-DeploymentFreshBaseline', 'Save-DeploymentFreshBaseline',
            'Get-DeploymentRiskLevel', 'Resolve-DeploymentStaleIsolation', 'Start-DeploymentFreshRollout',
            'Deploy-SelectedComponents', 'Invoke-DeploymentKubectl', 'Get-DeploymentOptionalProperty')
        foreach ($name in $functions) {
            $node = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name }, $true)
            if (-not $node) { throw "Function not found in platform.ps1: $name" }
            . ([scriptblock]::Create($node.Extent.Text))
        }

        # Isolated temp roots so Get-DeploymentHelmFingerprint and the fresh-baseline
        # files never touch the real repository or a real .tmp directory.
        $script:TestRoot = Join-Path ([IO.Path]::GetTempPath()) ("fresh-deploy-tests-" + [guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $script:TestRoot -Force | Out-Null
        $script:ProjectRoot = $script:TestRoot
        $script:RuntimeDir = Join-Path $script:TestRoot '.tmp'
        $script:HelmChart = Join-Path $script:TestRoot 'chart'
        $script:HelmValues = Join-Path $script:TestRoot 'values-dev.yaml'
        New-Item -ItemType Directory -Path $script:HelmChart -Force | Out-Null
        Set-Content -LiteralPath (Join-Path $script:HelmChart 'deployment.yaml') -Value 'kind: Deployment' -Encoding UTF8
        Set-Content -LiteralPath $script:HelmValues -Value 'replicaCount: 1' -Encoding UTF8

        # Replace every I/O boundary before testing orchestration/classification.
        # Kubernetes, Docker, Helm and the registry are all mocked below; this
        # file never calls Get-ApprovedRollbackBaseline (Start-DeploymentFreshRollout
        # does not use the verified/recovery path), so there is nothing real to
        # mock there, but the real, unmodified function still exists and is
        # exercised by RollbackBaseline.Tests.ps1 and the orchestration tests in
        # VerifiedDeployment.Tests.ps1.
        function docker { throw 'Unmocked Docker command forbidden' }
        function kubectl { throw 'Unmocked Kubernetes command forbidden' }
        function helm { throw 'Unmocked Helm command forbidden' }
        function mvn { throw 'Unmocked Maven command forbidden' }
        function python { throw 'Unmocked python command forbidden' }
        function Assert-LocalPrerequisites {}
        function Assert-BuildPrerequisites {}
        function Ensure-RuntimeDirectory { New-Item -ItemType Directory -Path $script:RuntimeDir -Force | Out-Null }
        function Get-VerifiedRegistryImage {
            param($Name, $Reference)
            if ($script:FailRegistry) { throw 'registry verification failed' }
            $digest = if ($Reference -like 'sha256:*') { $Reference } else { 'sha256:' + ('f' * 64) }
            "ai-investment-registry:5000/ai-investment/$Name@$digest"
        }
        function New-VerifiedDeploymentPatch { param($Deployment, $Name, $Image) @{ metadata = @{ resourceVersion = $Deployment.metadata.resourceVersion }; spec = @{ template = @{ spec = @{ containers = @(@{ name = $Name; image = $Image }) } } } } }
        function Wait-ForDeployments { param($Names) $script:WaitedFor = $Names }
        function Resolve-ApplicationUrl { return 'http://localhost:18080' }
        function Set-DeploymentPhase { param($Phase) $script:Phases += $Phase }

        function New-FakeResearchDeployment {
            param([string]$RecoveryPaused = 'true', [string]$SchedulerEnabled = 'false')
            [pscustomobject]@{
                metadata = @{ name = 'research-engine'; namespace = 'ai-investment'; resourceVersion = '1' }
                spec     = @{
                    selector = @{ matchLabels = @{ 'app.kubernetes.io/component' = 'research-engine'; 'app.kubernetes.io/instance' = 'ai-investment-platform'; 'app.kubernetes.io/name' = 'ai-investment-platform' } }
                    template = @{
                        spec = @{
                            containers     = @(@{ name = 'research-engine'; image = 'ai-investment-registry:5000/ai-investment/research-engine@sha256:' + ('a' * 64); env = @(
                                    @{ name = 'AIP_RESEARCH_OPPORTUNITY_RECOVERY_PAUSED'; value = $RecoveryPaused },
                                    @{ name = 'AIP_RESEARCH_OPPORTUNITY_SCHEDULER_ENABLED'; value = $SchedulerEnabled }
                                ) })
                            initContainers = @(@{ name = 'wait-for-research-migrations'; image = 'x' })
                        }
                    }
                }
            }
        }

        function New-FakeResearchPod {
            param([bool]$InitSucceeded = $true)
            [pscustomobject]@{
                metadata = @{ labels = @{ 'app.kubernetes.io/component' = 'research-engine' } }
                status   = @{ initContainerStatuses = @(@{ name = 'wait-for-research-migrations'; state = @{ terminated = if ($InitSucceeded) { @{ exitCode = 0 } } else { @{ exitCode = 1 } } } }) }
            }
        }

        # Get-ApprovedRollbackBaseline lives in scripts/database/rollback-baseline.ps1,
        # not platform.ps1, so it is never AST-extracted here and must be
        # faked like any other external boundary. Get-DeploymentRiskLevel now
        # uses it (re-resolved through Get-VerifiedRegistryImage, never
        # trusted as a bare string) as the trust anchor for a first-time
        # bootstrap with no recorded fresh-deploy baseline. The default here
        # matches New-FakeResearchDeployment's running-image digest, so
        # existing LOW_RISK fixtures keep passing through genuine
        # verification rather than the old silent-accept behavior.
        function Get-ApprovedRollbackBaseline {
            param($ProjectRoot)
            if ($script:ApprovedBaselineUnavailable) { throw 'Approved rollback baseline manifest not found (simulated)' }
            [pscustomobject]@{
                Components      = $script:ApprovedBaselineComponents
                InitContainers  = @{}
                BaselineVersion = 1
                ApprovedAtUtc   = '2026-01-01T00:00:00Z'
                ApprovedBy      = 'fixture'
            }
        }

        function Reset-RiskFixture {
            $script:ResearchDeployment = New-FakeResearchDeployment
            $script:ResearchPod = New-FakeResearchPod
            $script:Policies = @()
            $script:Jobs = @()
            $script:MigrationFingerprints = @{ 'research-engine' = @{ 'V1.sql' = 1 }; 'portfolio-service' = @{ 'V1.sql' = 1 } }
            $script:DeletedPolicies = @()
            $script:Patched = @{}
            $script:Commands = @()
            $script:ApprovedBaselineUnavailable = $false
            $script:ApprovedBaselineComponents = @{
                'api-gateway'       = 'sha256:' + ('1' * 64)
                'research-engine'   = 'sha256:' + ('a' * 64)
                'portfolio-service' = 'sha256:' + ('2' * 64)
                'frontend'          = 'sha256:' + ('3' * 64)
            }
            $script:DryRun = $false
            $script:SkipBuild = $false
            Remove-Item -LiteralPath (Join-Path $script:RuntimeDir 'fresh-deploy-state') -Recurse -Force -ErrorAction SilentlyContinue
        }

        function Invoke-DeploymentCommand {
            param($Program, $Arguments)
            $script:Commands += @{ Program = $Program; Arguments = $Arguments }
            if ($Program -eq 'python') {
                $name = if (($Arguments[1]) -match 'portfolio-service') { 'portfolio-service' } else { 'research-engine' }
                return ($script:MigrationFingerprints[$name] | ConvertTo-Json -Compress)
            }
            if ($Program -eq 'helm') { return '{"name":"ai-investment-platform","version":10}' }
            return $null
        }

        function Invoke-DeploymentKubectl {
            param([string[]]$Arguments)
            if (($Arguments -join ' ') -match '^get --raw=/readyz') { return 'ok' }
            if ($Arguments[0] -eq 'get' -and $Arguments[1] -eq 'deployment' -and $Arguments[2] -eq 'research-engine') { return ($script:ResearchDeployment | ConvertTo-Json -Depth 20) }
            if ($Arguments[0] -eq 'get' -and $Arguments[1] -eq 'pods') { return (@{ items = @($script:ResearchPod) } | ConvertTo-Json -Depth 20) }
            if ($Arguments[0] -eq 'get' -and $Arguments[1] -eq 'networkpolicies') { return (@{ items = @($script:Policies) } | ConvertTo-Json -Depth 20) }
            if ($Arguments[0] -eq 'get' -and $Arguments[1] -eq 'jobs,cronjobs') { return (@{ items = @($script:Jobs) } | ConvertTo-Json -Depth 20) }
            if ($Arguments[0] -eq 'delete' -and $Arguments[1] -eq 'networkpolicy') { $script:DeletedPolicies += $Arguments[2]; return $null }
            throw "Unmocked kubectl invocation: $($Arguments -join ' ')"
        }

        function New-FakeIsolationPolicy {
            param($Selector)
            [pscustomobject]@{
                metadata = @{ name = 'research-bootstrap-isolation'; namespace = 'ai-investment' }
                spec     = @{ podSelector = $Selector; policyTypes = @('Ingress'); ingress = @() }
            }
        }
    }

    AfterAll {
        Remove-Item -LiteralPath $script:TestRoot -Recurse -Force -ErrorAction SilentlyContinue
    }

    Context 'Get-DeploymentRiskLevel' {
        It 'first-time bootstrap with safe worker state classifies LOW_RISK' {
            Reset-RiskFixture
            $risk = Get-DeploymentRiskLevel -Names @('research-engine')
            Assert ($risk.Level -eq 'LOW_RISK') "Expected LOW_RISK, got $($risk.Level): $($risk.Reasons -join '; ')"
        }

        It 'scheduler enabled fails closed to HIGH_RISK' {
            Reset-RiskFixture
            $script:ResearchDeployment = New-FakeResearchDeployment -SchedulerEnabled 'true'
            $risk = Get-DeploymentRiskLevel -Names @('research-engine')
            Assert ($risk.Level -eq 'HIGH_RISK') 'Expected HIGH_RISK'
            Assert (($risk.Reasons -join ';') -match 'worker state') 'Missing worker-state reason'
        }

        It 'recovery not paused fails closed to HIGH_RISK' {
            Reset-RiskFixture
            $script:ResearchDeployment = New-FakeResearchDeployment -RecoveryPaused 'false'
            $risk = Get-DeploymentRiskLevel -Names @('research-engine')
            Assert ($risk.Level -eq 'HIGH_RISK') 'Expected HIGH_RISK'
        }

        It 'unterminated migration init container fails closed to HIGH_RISK' {
            Reset-RiskFixture
            $script:ResearchPod = New-FakeResearchPod -InitSucceeded $false
            $risk = Get-DeploymentRiskLevel -Names @('research-engine')
            Assert ($risk.Level -eq 'HIGH_RISK') 'Expected HIGH_RISK'
            Assert (($risk.Reasons -join ';') -match 'init container') 'Missing init-container reason'
        }

        It 'migration fingerprint drift from recorded baseline fails closed to HIGH_RISK' {
            Reset-RiskFixture
            Save-DeploymentFreshBaseline -Name 'research-engine' -Image 'old-image' -MigrationFingerprint @{ 'V1.sql' = 1 } -HelmFingerprint (Get-DeploymentHelmFingerprint)
            $script:MigrationFingerprints['research-engine'] = @{ 'V1.sql' = 1; 'V2.sql' = 2 }
            $risk = Get-DeploymentRiskLevel -Names @('research-engine')
            Assert ($risk.Level -eq 'HIGH_RISK') 'Expected HIGH_RISK'
            Assert (($risk.Reasons -join ';') -match 'schema migration change') 'Missing schema-change reason'
        }

        It 'helm fingerprint drift from recorded baseline fails closed to HIGH_RISK' {
            Reset-RiskFixture
            Save-DeploymentFreshBaseline -Name 'research-engine' -Image 'old-image' -MigrationFingerprint @{ 'V1.sql' = 1 } -HelmFingerprint 'stale-fingerprint'
            $risk = Get-DeploymentRiskLevel -Names @('research-engine')
            Assert ($risk.Level -eq 'HIGH_RISK') 'Expected HIGH_RISK'
            Assert (($risk.Reasons -join ';') -match 'infrastructure') 'Missing infrastructure-change reason'
        }

        It 'unchanged fingerprints against a recorded baseline classify LOW_RISK' {
            Reset-RiskFixture
            $helmFingerprint = Get-DeploymentHelmFingerprint
            Save-DeploymentFreshBaseline -Name 'research-engine' -Image 'old-image' -MigrationFingerprint $script:MigrationFingerprints['research-engine'] -HelmFingerprint $helmFingerprint
            $risk = Get-DeploymentRiskLevel -Names @('research-engine')
            Assert ($risk.Level -eq 'LOW_RISK') "Expected LOW_RISK, got: $($risk.Reasons -join '; ')"
        }

        It 'classification query failure fails closed to HIGH_RISK' {
            Reset-RiskFixture
            function Invoke-DeploymentKubectl { param([string[]]$Arguments) throw 'cluster unreachable' }
            $risk = Get-DeploymentRiskLevel -Names @('research-engine')
            Assert ($risk.Level -eq 'HIGH_RISK') 'Expected HIGH_RISK on query failure'
            Assert (($risk.Reasons -join ';') -match 'could not be completed') 'Missing classification-failure reason'
            # This local redefinition is scoped to this It block only (Pester
            # gives each It a fresh child scope), so the shared Invoke-DeploymentKubectl
            # fake defined in BeforeAll is unaffected for subsequent tests; no
            # manual restore is required.
        }

        It 'first-time bootstrap fails closed to HIGH_RISK when the running image does not match the approved rollback baseline' {
            Reset-RiskFixture
            $script:ApprovedBaselineComponents['research-engine'] = 'sha256:' + ('9' * 64)
            $risk = Get-DeploymentRiskLevel -Names @('research-engine')
            Assert ($risk.Level -eq 'HIGH_RISK') "Expected HIGH_RISK, got $($risk.Level): $($risk.Reasons -join '; ')"
            Assert (($risk.Reasons -join ';') -match 'not the independently-approved rollback baseline image') 'Missing bootstrap-trust-anchor mismatch reason'
        }

        It 'first-time bootstrap fails closed to HIGH_RISK when the approved rollback baseline is unavailable' {
            Reset-RiskFixture
            $script:ApprovedBaselineUnavailable = $true
            $risk = Get-DeploymentRiskLevel -Names @('research-engine')
            Assert ($risk.Level -eq 'HIGH_RISK') "Expected HIGH_RISK, got $($risk.Level): $($risk.Reasons -join '; ')"
            Assert (($risk.Reasons -join ';') -match 'could not independently verify') 'Missing bootstrap-trust-anchor unavailable reason'
        }
    }

    Context 'Resolve-DeploymentStaleIsolation' {
        It 'no policy present is a no-op' {
            Reset-RiskFixture
            Resolve-DeploymentStaleIsolation -Names @('research-engine')
            Assert ($script:DeletedPolicies.Count -eq 0) 'Should not have deleted anything'
        }

        It 'exact-shape stale policy is removed when quiescent' {
            Reset-RiskFixture
            $script:Policies = @(New-FakeIsolationPolicy -Selector $script:ResearchDeployment.spec.selector)
            Resolve-DeploymentStaleIsolation -Names @('research-engine')
            Assert ($script:DeletedPolicies -contains 'research-bootstrap-isolation') 'Expected stale policy to be removed'
        }

        It 'mismatched selector is never touched' {
            Reset-RiskFixture
            $wrongSelector = @{ matchLabels = @{ 'app.kubernetes.io/component' = 'frontend'; 'app.kubernetes.io/instance' = 'ai-investment-platform'; 'app.kubernetes.io/name' = 'ai-investment-platform' } }
            $script:Policies = @(New-FakeIsolationPolicy -Selector $wrongSelector)
            Throws { Resolve-DeploymentStaleIsolation -Names @('research-engine') } '*expected deployment-owned shape*'
            Assert ($script:DeletedPolicies.Count -eq 0) 'Must not delete a policy that does not match exactly'
        }

        It 'unrelated additional policy blocks automatic cleanup' {
            Reset-RiskFixture
            $script:Policies = @((New-FakeIsolationPolicy -Selector $script:ResearchDeployment.spec.selector), [pscustomobject]@{ metadata = @{ name = 'unrelated-policy'; namespace = 'ai-investment' }; spec = @{ podSelector = @{}; policyTypes = @('Ingress'); ingress = @() } })
            Throws { Resolve-DeploymentStaleIsolation -Names @('research-engine') } '*Unexpected additional network policies*'
            Assert ($script:DeletedPolicies.Count -eq 0) 'Must not touch an unrelated policy'
        }

        It 'active job blocks ingress restoration' {
            Reset-RiskFixture
            $script:Policies = @(New-FakeIsolationPolicy -Selector $script:ResearchDeployment.spec.selector)
            $script:Jobs = @([pscustomobject]@{ kind = 'Job'; status = @{ active = 1 } })
            Throws { Resolve-DeploymentStaleIsolation -Names @('research-engine') } '*active*'
            Assert ($script:DeletedPolicies.Count -eq 0) 'Must not restore ingress while work is active'
        }

        It 'unterminated migration init container blocks ingress restoration' {
            Reset-RiskFixture
            $script:Policies = @(New-FakeIsolationPolicy -Selector $script:ResearchDeployment.spec.selector)
            $script:ResearchPod = New-FakeResearchPod -InitSucceeded $false
            Throws { Resolve-DeploymentStaleIsolation -Names @('research-engine') } '*has not cleanly completed*'
            Assert ($script:DeletedPolicies.Count -eq 0) 'Must not restore ingress while migration is unresolved'
        }
    }

    Context 'Fresh-baseline bookkeeping' {
        It 'fresh baseline round-trips through save/read' {
            Reset-RiskFixture
            Assert ($null -eq (Get-DeploymentFreshBaseline -Name 'research-engine')) 'Expected no baseline before first save'
            Save-DeploymentFreshBaseline -Name 'research-engine' -Image 'ai-investment-registry:5000/ai-investment/research-engine@sha256:deadbeef' -MigrationFingerprint @{ 'V1.sql' = 1 } -HelmFingerprint 'abc123'
            $baseline = Get-DeploymentFreshBaseline -Name 'research-engine'
            Assert ($baseline.image -eq 'ai-investment-registry:5000/ai-investment/research-engine@sha256:deadbeef') 'Image not preserved'
            Assert ($baseline.helmFingerprint -eq 'abc123') 'Helm fingerprint not preserved'
        }
    }

    Context 'Start-DeploymentFreshRollout isolation-restoration ordering' {
        BeforeAll {
            function New-FakeApiGatewayDeployment {
                param([string]$Image = ('ai-investment-registry:5000/ai-investment/api-gateway@sha256:' + ('1' * 64)))
                [pscustomobject]@{
                    metadata = @{ name = 'api-gateway'; namespace = 'ai-investment'; resourceVersion = '1' }
                    spec     = @{ template = @{ spec = @{ containers = @(@{ name = 'api-gateway'; image = $Image }) } } }
                }
            }

            function Reset-FreshRolloutOrderingFixture {
                Reset-RiskFixture
                $script:ApiGatewayDeployment = New-FakeApiGatewayDeployment
                $script:CallOrder = @()
                $script:FailAt = $null
            }

            # A focused kubectl fake for exactly this test's needs: it tracks
            # WHEN each step happens via $script:CallOrder rather than
            # re-verifying registry/patch mechanics already covered elsewhere.
            function Invoke-DeploymentKubectl {
                param([string[]]$Arguments)
                if (($Arguments -join ' ') -match '^get --raw=/readyz') { return 'ok' }
                if ($Arguments[0] -eq 'get' -and $Arguments[1] -eq 'deployment' -and $Arguments[2] -eq 'api-gateway') {
                    return ($script:ApiGatewayDeployment | ConvertTo-Json -Depth 20)
                }
                if ($Arguments[0] -eq 'patch') {
                    $script:CallOrder += 'patch'

                    if ($script:FailAt -eq 'patch') {
                        throw 'simulated patch failure'
                    }

                    # Simulate kubectl applying the Deployment patch. The real
                    # Start-DeploymentFreshRollout writes the patch body to a
                    # file and passes its path via --patch-file (not an inline
                    # -p/--patch JSON argument), so read and parse that file,
                    # exactly as kubectl would, rather than expecting the JSON
                    # inline on the command line.
                    $patchFileIndex = [Array]::IndexOf($Arguments, '--patch-file')

                    if ($patchFileIndex -lt 0 -or ($patchFileIndex + 1) -ge $Arguments.Count) {
                        throw 'Fixture: kubectl --patch-file argument not found'
                    }

                    $patchFilePath = $Arguments[$patchFileIndex + 1]

                    if (-not (Test-Path -LiteralPath $patchFilePath)) {
                        throw "Fixture: patch file not found at $patchFilePath"
                    }

                    $patchPayload = Get-Content -LiteralPath $patchFilePath -Raw |
                        ConvertFrom-Json -ErrorAction Stop

                    $newImage = (@($patchPayload.spec.template.spec.containers))[0].image

                    if ([string]::IsNullOrWhiteSpace($newImage)) {
                        throw 'Fixture: patched container image missing'
                    }

                    $script:ApiGatewayDeployment.spec.template.spec.containers[0].image = $newImage

                    return $null
                }
                if ($Arguments[0] -eq 'rollout') {
                    $script:CallOrder += 'rollout-status'
                    if ($script:FailAt -eq 'rollout') { throw 'simulated rollout failure' }
                    return $null
                }
                throw "Unmocked kubectl invocation: $($Arguments -join ' ')"
            }

            # The internal safety checks (ownership/selector/shape/active-work
            # verification, never touching an unrelated policy) are already
            # covered by the 'Resolve-DeploymentStaleIsolation' tests above;
            # this fake records only WHEN Start-DeploymentFreshRollout invokes
            # it, to prove the ordering relative to the rollout and the wait.
            function Resolve-DeploymentStaleIsolation {
                param([string[]]$Names)
                $script:CallOrder += 'resolve-stale-isolation'
            }

            function Wait-ForDeployments { param($Names) $script:CallOrder += 'wait-for-deployments' }
            function Resolve-ApplicationUrl { $script:CallOrder += 'resolve-application-url'; return 'http://localhost:18080' }
        }

        It 'restores isolation only after a fully successful rollout and readiness wait' {
            Reset-FreshRolloutOrderingFixture
            Start-DeploymentFreshRollout -Names @('api-gateway')
            $patchIndex = $script:CallOrder.IndexOf('patch')
            $rolloutIndex = $script:CallOrder.IndexOf('rollout-status')
            $waitIndex = $script:CallOrder.IndexOf('wait-for-deployments')
            $isolationIndex = $script:CallOrder.IndexOf('resolve-stale-isolation')
            $urlIndex = $script:CallOrder.IndexOf('resolve-application-url')
            Assert (@($patchIndex, $rolloutIndex, $waitIndex, $isolationIndex, $urlIndex) -notcontains -1) "Missing expected call(s): $($script:CallOrder -join ',')"
            Assert ($isolationIndex -gt $rolloutIndex) 'Isolation was restored before the rollout completed'
            Assert ($isolationIndex -gt $waitIndex) 'Isolation was restored before the readiness wait completed'
            Assert ($isolationIndex -lt $urlIndex) 'Isolation must be restored before resolving/verifying application connectivity'
        }

        It 'does not restore isolation when the rollout patch fails' {
            Reset-FreshRolloutOrderingFixture
            $script:FailAt = 'patch'
            Throws { Start-DeploymentFreshRollout -Names @('api-gateway') } '*simulated patch failure*'
            Assert ($script:CallOrder -notcontains 'resolve-stale-isolation') 'Isolation was touched despite a failed rollout'
        }

        It 'does not restore isolation when rollout-status verification fails' {
            Reset-FreshRolloutOrderingFixture
            $script:FailAt = 'rollout'
            Throws { Start-DeploymentFreshRollout -Names @('api-gateway') } '*simulated rollout failure*'
            Assert ($script:CallOrder -notcontains 'resolve-stale-isolation') 'Isolation was touched despite a failed rollout'
        }
    }

    Context 'Deploy-SelectedComponents dispatch' {
        It 'low-risk unflagged selection dispatches to the fresh rollout path' {
            Reset-RiskFixture
            $script:AcceptBootstrapRisk = $false
            $script:ConfirmCallerFreeze = $false
            $script:ResumeAuditTag = $null
            $script:FreshRolloutCalledWith = $null
            function Resolve-SelectedComponents { param([switch]$RequireSelection) @('research-engine') }
            function Start-DeploymentFreshRollout { param([string[]]$Names) $script:FreshRolloutCalledWith = $Names }
            function Deploy-VerifiedRadarComponents { param($Names) throw 'verified path must not run for a low-risk unflagged deploy' }
            Deploy-SelectedComponents
            Assert (($script:FreshRolloutCalledWith -join ',') -eq 'research-engine') 'Fresh rollout path was not used'
        }

        It 'high-risk classification refuses the fresh path and does not fall back silently' {
            Reset-RiskFixture
            $script:ResearchDeployment = New-FakeResearchDeployment -SchedulerEnabled 'true'
            $script:AcceptBootstrapRisk = $false
            $script:ConfirmCallerFreeze = $false
            $script:ResumeAuditTag = $null
            function Resolve-SelectedComponents { param([switch]$RequireSelection) @('research-engine') }
            function Start-DeploymentFreshRollout { param([string[]]$Names) throw 'fresh rollout must not run for a high-risk change' }
            function Deploy-VerifiedRadarComponents { param($Names) throw 'verified path must not be silently invoked either; the caller must re-run with explicit flags' }
            Throws { Deploy-SelectedComponents } '*requires the verified deployment path*'
        }
    }

    Context 'StrictMode safety in the real command/kubectl helpers' {
        # Regression coverage for the exact failure observed in the field:
        # platform.ps1 was invoked (via '&') from a caller that had already run
        # Set-StrictMode -Version Latest in its own scope -- that setting cascades
        # into this script's execution. "if ($script:DeploymentRun)" and the
        # sibling guard on $script:VerifiedDeploymentDigests relied on
        # PowerShell's normal, non-strict treatment of a never-set variable as
        # falsy/empty, which throws under strict mode instead:
        #   "The variable '$script:DeploymentRun' cannot be retrieved because
        #    it has not been set."
        #
        # The Describe-level BeforeAll above hand-mocks Invoke-DeploymentCommand
        # and Invoke-DeploymentKubectl, so it never exercises the real, buggy
        # guard. This Context AST-extracts and dot-sources the REAL
        # Invoke-DeploymentCommand / Invoke-DeploymentKubectl /
        # ConvertTo-DeploymentArgument from platform.ps1 (shadowing the outer
        # mocks for every It below) and fakes only Invoke-DeploymentNativeCommand
        # -- the genuine external-process boundary -- so no real kubectl ever runs.
        #
        # Extracting only named function bodies (the pattern this whole file
        # uses) cannot exercise platform.ps1's own top-level script-scope
        # initialization -- dot-sourcing a function's text runs it in this
        # test's script scope, not platform.ps1's. So two complementary checks
        # are used: a dynamic one pinning the exact crash when the variable is
        # genuinely never set (proving the mechanism), and a static one that
        # inspects platform.ps1's real top-level statements to confirm it does
        # initialize both variables before any function could read them -- this
        # is what actually prevents the crash in the real, full-script
        # invocation path and is what a future regression would break.
        BeforeAll {
            $strictPlatformPath = Join-Path (Get-Location) 'platform.ps1'
            $strictTokens = $null; $strictErrors = $null
            $strictAst = [System.Management.Automation.Language.Parser]::ParseFile($strictPlatformPath, [ref]$strictTokens, [ref]$strictErrors)
            if ($strictErrors.Count) { throw ($strictErrors | Out-String) }
            foreach ($name in @('Invoke-DeploymentCommand', 'Invoke-DeploymentKubectl', 'ConvertTo-DeploymentArgument')) {
                $node = $strictAst.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name }, $true)
                if (-not $node) { throw "Function not found in platform.ps1: $name" }
                . ([scriptblock]::Create($node.Extent.Text))
            }
            function Invoke-DeploymentNativeCommand {
                param([string]$Program, [string[]]$Arguments)
                [pscustomobject]@{ ExitCode = 0; StdOut = '{"kind":"Deployment","metadata":{"name":"research-engine"}}'; StdErr = '' }
            }
        }

        It 'pins the exact reported crash: a never-set $script:DeploymentRun throws under Set-StrictMode' {
            Set-StrictMode -Version Latest
            Remove-Variable -Name DeploymentRun -Scope Script -ErrorAction SilentlyContinue
            Throws { Invoke-DeploymentKubectl @('get', 'deployment', 'research-engine', '-o', 'json') } "*cannot be retrieved because it has not been set*"
        }

        It 'does not throw once $script:DeploymentRun is initialized (the state platform.ps1''s own top-level code puts it in before any function runs)' {
            Set-StrictMode -Version Latest
            $script:DeploymentRun = $null
            $result = Invoke-DeploymentKubectl @('get', 'deployment', 'research-engine', '-o', 'json')
            Assert ($result -eq '{"kind":"Deployment","metadata":{"name":"research-engine"}}') "Unexpected result: $result"
        }

        It 'platform.ps1 itself initializes $script:DeploymentRun and $script:VerifiedDeploymentDigests at top-level script scope, before any function could read them' {
            $topLevelAssignments = $strictAst.EndBlock.Statements | Where-Object {
                $_ -is [System.Management.Automation.Language.AssignmentStatementAst] -and
                $_.Left -is [System.Management.Automation.Language.VariableExpressionAst]
            }
            $names = $topLevelAssignments | ForEach-Object { $_.Left.VariablePath.UserPath }
            Assert ($names -contains 'script:DeploymentRun') 'platform.ps1 no longer initializes $script:DeploymentRun at top-level script scope -- the StrictMode crash this test guards against can recur'
            Assert ($names -contains 'script:VerifiedDeploymentDigests') 'platform.ps1 no longer initializes $script:VerifiedDeploymentDigests at top-level script scope -- the analogous StrictMode crash in Get-VerifiedRegistryImage can recur'
        }
    }

    Context 'Get-DeploymentRiskLevel tolerates pods lacking app.kubernetes.io/* labels or a non-terminated init container' {
        # Regression coverage for the second StrictMode crash reported in the
        # field: "The property 'app.kubernetes.io/component' cannot be found
        # on this object." kubectl get pods (no label selector) returns every
        # pod in the namespace, including ones with a completely different
        # label scheme (e.g. postgres-0), and a still-starting research-engine
        # pod's init container state is shaped {running:{...}} with no
        # "terminated" key at all -- both are legitimate, expected shapes, not
        # schema violations, so Get-DeploymentRiskLevel must tolerate them via
        # Get-DeploymentOptionalProperty rather than a direct dotted access.
        It 'does not throw when another pod in the namespace has no app.kubernetes.io/* labels at all (e.g. postgres-0)' {
            Reset-RiskFixture
            $helmFingerprint = Get-DeploymentHelmFingerprint
            Save-DeploymentFreshBaseline -Name 'research-engine' -Image 'old-image' -MigrationFingerprint $script:MigrationFingerprints['research-engine'] -HelmFingerprint $helmFingerprint
            $unlabeledPod = @{ metadata = @{ labels = @{ 'app' = 'postgres' } }; status = @{ initContainerStatuses = @() } }
            $script:ResearchPod = @($script:ResearchPod, $unlabeledPod)
            Set-StrictMode -Version Latest
            $result = Get-DeploymentRiskLevel -Names @('research-engine')
            Assert ($result.Level -eq 'LOW_RISK') "Expected LOW_RISK, got $($result.Level): $($result.Reasons -join '; ')"
        }

        It 'does not throw, and correctly reports HIGH_RISK, when the research-engine init container state is "running" rather than "terminated"' {
            Reset-RiskFixture
            $helmFingerprint = Get-DeploymentHelmFingerprint
            Save-DeploymentFreshBaseline -Name 'research-engine' -Image 'old-image' -MigrationFingerprint $script:MigrationFingerprints['research-engine'] -HelmFingerprint $helmFingerprint
            $script:ResearchPod = @{ metadata = @{ labels = @{ 'app.kubernetes.io/component' = 'research-engine' } }; status = @{ initContainerStatuses = @(@{ name = 'wait-for-research-migrations'; state = @{ running = @{ startedAt = '2026-01-01T00:00:00Z' } } }) } }
            Set-StrictMode -Version Latest
            $result = Get-DeploymentRiskLevel -Names @('research-engine')
            Assert ($result.Level -eq 'HIGH_RISK') "Expected HIGH_RISK, got $($result.Level)"
            Assert (@($result.Reasons) -contains 'research-engine migration/startup init container has not cleanly completed') "Missing expected reason, got: $($result.Reasons -join '; ')"
        }

        It 'still classifies the unmodified happy-path fixture as LOW_RISK (no regression)' {
            Reset-RiskFixture
            $helmFingerprint = Get-DeploymentHelmFingerprint
            Save-DeploymentFreshBaseline -Name 'research-engine' -Image 'old-image' -MigrationFingerprint $script:MigrationFingerprints['research-engine'] -HelmFingerprint $helmFingerprint
            Set-StrictMode -Version Latest
            $result = Get-DeploymentRiskLevel -Names @('research-engine')
            Assert ($result.Level -eq 'LOW_RISK') "Expected LOW_RISK, got $($result.Level): $($result.Reasons -join '; ')"
        }
    }
}
