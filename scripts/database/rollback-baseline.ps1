# scripts/database/rollback-baseline.ps1
#
# Feature: externalized, versioned, explicitly-approved rollback-image
# baseline for the protected deployment path (Deploy-VerifiedRadarComponents
# / Deploy-VerifiedRadarComponentsCore in platform.ps1).
#
# Replaces the previous hardcoded $rollback = @{...} digest map with a
# persistent JSON manifest under scripts/database/rollback-baseline/, so the
# approved baseline can be advanced over time WITHOUT editing platform.ps1,
# while remaining just as fail-closed as the hardcoded map was.
#
# Design (intentionally small -- no new deployment/mutation behavior):
#   - Get-ApprovedRollbackBaseline: loads + strictly validates the current
#     manifest. Fails closed on any missing file, malformed JSON, missing
#     schema/version fields, or missing/invalid per-component digest. This
#     is a drop-in replacement for the old hardcoded hashtable: callers keep
#     using $rollback[$name] exactly as before.
#   - Save-VerifiedRollbackSnapshot: named, mockable wrapper around writing
#     the pre-deploy "rollback-$tag.json" evidence file that
#     Deploy-VerifiedRadarComponentsCore already produced inline. Behavior
#     is unchanged; this only makes the step independently testable.
#   - Save-RollbackBaselineApprovalCandidate: after a rollout has been fully
#     verified (i.e. called only once Complete-DeploymentSafety has already
#     succeeded), records the newly-running, already Get-VerifiedRegistryImage
#     -verified images as a candidate for baseline advancement. This file is
#     evidence only -- it never advances the approved baseline by itself.
#   - Approve-RollbackBaseline: the ONLY function that advances the approved
#     baseline. Requires explicit authorization, requires a candidate file
#     produced by a real prior successful rollout, and re-resolves every
#     candidate image against the registry at approval time (never trusts
#     a recorded digest string on its own) before writing a new, versioned
#     manifest. Fails closed -- and never touches the existing approved
#     manifest -- on missing authorization, a missing/malformed candidate,
#     a registry lookup failure, or a mismatch between the candidate's
#     recorded digest and what the registry resolves right now.
#
# Nothing in this file deploys, patches Kubernetes, or touches a database.

function Get-RollbackBaselineManifestPath {
    param([Parameter(Mandatory = $true)][string]$ProjectRoot)
    Join-Path $ProjectRoot "scripts\database\rollback-baseline\approved-rollback-baseline.json"
}

function Get-RollbackBaselineHistoryPath {
    param([Parameter(Mandatory = $true)][string]$ProjectRoot)
    Join-Path $ProjectRoot "scripts\database\rollback-baseline\approved-rollback-baseline-history.jsonl"
}

# The fixed set of protected components the baseline manifest must always
# cover. Intentionally duplicated here (rather than imported from
# platform.ps1) so this module has no load-order dependency on it.
$script:RollbackBaselineComponents = @('api-gateway', 'research-engine', 'portfolio-service', 'frontend')

# Strict-mode-safe optional-property lookup, local to this module. This
# module's own design is to depend on platform.ps1 ONLY through
# Get-VerifiedRegistryImage (deliberately stubbed/mocked in this file's own
# tests, per RollbackBaseline.Tests.ps1's header comment) -- every other
# dependency must be self-contained here. Approve-RollbackBaselineReconciliation
# previously called platform.ps1's Get-DeploymentOptionalProperty directly,
# which only exists when this module happens to be dot-sourced from inside
# platform.ps1's own execution; an isolated load of this module alone (as
# RollbackBaseline.Tests.ps1 deliberately does) then fails with "the term
# 'Get-DeploymentOptionalProperty' is not recognized". Defining the same,
# behavior-identical helper here removes that accidental cross-file
# dependency without changing what it returns anywhere it is called:
# $null for a missing property or a $null object, never a thrown error
# for a Kubernetes 'omitempty' field such as metadata.deletionTimestamp.
function Get-DeploymentOptionalProperty {
    param($Object, [string]$Name)
    if ($null -eq $Object) { return $null }
    if ($Object -is [System.Collections.IDictionary]) {
        if ($Object.Contains($Name)) { return $Object[$Name] }
        return $null
    }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) { return $null }
    return $property.Value
}

function Assert-ValidRollbackDigest {
    param([Parameter(Mandatory = $true)][string]$Name, $Digest)
    if ($null -eq $Digest -or $Digest -isnot [string] -or $Digest -notmatch '^sha256:[a-f0-9]{64}$') {
        throw "Approved rollback baseline is missing a valid sha256 digest for component '$Name' -- refusing to proceed with an incomplete or malformed baseline."
    }
}

function Get-ApprovedRollbackBaseline {
    # Fail-closed replacement for the old hardcoded $rollback hashtable.
    # Returns a hashtable keyed by component name -> "sha256:..." digest,
    # with the same shape callers already use ($rollback[$name]).
    param([Parameter(Mandatory = $true)][string]$ProjectRoot)
    $path = Get-RollbackBaselineManifestPath -ProjectRoot $ProjectRoot
    if (-not (Test-Path -LiteralPath $path)) {
        throw "Approved rollback baseline manifest not found at '$path'. Refusing to deploy without an explicit, versioned, approved baseline -- run 'approve-rollback-baseline' once to establish one."
    }
    $raw = Get-Content -LiteralPath $path -Raw
    try {
        $manifest = $raw | ConvertFrom-Json
    } catch {
        throw "Approved rollback baseline manifest at '$path' is not valid JSON -- refusing to proceed. $($_.Exception.Message)"
    }
    if (-not $manifest.schemaVersion -or $manifest.schemaVersion -ne 1) { throw "Approved rollback baseline manifest has an unsupported or missing schemaVersion." }
    if (-not $manifest.baselineVersion -or $manifest.baselineVersion -lt 1) { throw "Approved rollback baseline manifest has an invalid or missing baselineVersion." }
    if (-not $manifest.components) { throw "Approved rollback baseline manifest is missing its 'components' map." }
    $result = @{}
    foreach ($name in $script:RollbackBaselineComponents) {
        $digest = $manifest.components.$name
        Assert-ValidRollbackDigest -Name $name -Digest $digest
        $result[$name] = $digest
    }

    # Independently-approved init-container digests (e.g. the
    # research-engine migration-wait init container), keyed by container
    # name. This is additive/optional -- an existing manifest with no
    # 'initContainers' field (or an explicit null) is still valid and is
    # treated as "no init container has an approved baseline yet", which
    # fails closed at the call site rather than silently trusting or
    # defaulting to the main container's approved digest.
    $initResult = @{}
    if ($manifest.PSObject.Properties.Name -contains 'initContainers' -and $null -ne $manifest.initContainers) {
        foreach ($prop in $manifest.initContainers.PSObject.Properties) {
            Assert-ValidRollbackDigest -Name "init container '$($prop.Name)'" -Digest $prop.Value
            $initResult[$prop.Name] = $prop.Value
        }
    }

    [pscustomobject]@{
        Components      = $result
        InitContainers  = $initResult
        BaselineVersion = [int]$manifest.baselineVersion
        ApprovedAtUtc   = [string]$manifest.approvedAtUtc
        ApprovedBy      = [string]$manifest.approvedBy
    }
}

function Save-VerifiedRollbackSnapshot {
    # Named, mockable seam for the pre-deploy rollback-image evidence file.
    # Behavior-identical to the inline `$oldImages | ConvertTo-Json | Set-Content`
    # this replaces -- only now independently testable/mockable in Pester.
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)]$Images)
    $Images | ConvertTo-Json | Set-Content -LiteralPath $Path -Encoding ASCII
}

function Save-RollbackBaselineApprovalCandidate {
    # Called ONLY after a rollout has been fully verified (i.e. after
    # Complete-DeploymentSafety has already succeeded for every $Names
    # component). Records the newly-running, already-verified images as a
    # candidate for a LATER, separately-authorized baseline advancement.
    # Writing this file never changes the approved baseline by itself.
    param(
        [Parameter(Mandatory = $true)][string]$ProjectRoot,
        [Parameter(Mandatory = $true)][string]$RuntimeDir,
        [Parameter(Mandatory = $true)][string]$Tag,
        [Parameter(Mandatory = $true)][string[]]$Names,
        [Parameter(Mandatory = $true)][hashtable]$Images,
        # Optional: independently-verified init-container images, keyed by
        # container name -> @{ component = <name>; image = <verified image ref> }.
        # Additive and backward compatible -- omitting it records no init
        # container candidates, exactly as before this parameter existed.
        [hashtable]$InitImages = @{}
    )
    foreach ($name in $Names) {
        if (-not $Images.ContainsKey($name) -or [string]::IsNullOrWhiteSpace($Images[$name])) {
            throw "Cannot record a rollback-baseline approval candidate: missing verified image for component '$name'."
        }
    }
    foreach ($containerName in $InitImages.Keys) {
        $entry = $InitImages[$containerName]
        if (-not $entry -or [string]::IsNullOrWhiteSpace($entry.component) -or [string]::IsNullOrWhiteSpace($entry.image)) {
            throw "Cannot record a rollback-baseline approval candidate: malformed init-image entry for container '$containerName'."
        }
        if ($entry.component -notin $Names) {
            throw "Cannot record a rollback-baseline approval candidate: init container '$containerName' references component '$($entry.component)' which is not part of this deployment's components ($($Names -join ', '))."
        }
    }
    $candidate = [ordered]@{
        schemaVersion  = 1
        tag            = $Tag
        recordedAtUtc  = [DateTime]::UtcNow.ToString('o')
        names          = @($Names)
        images         = $Images
        initImages     = $InitImages
    }
    $path = Join-Path $RuntimeDir "rollback-baseline-candidate-$Tag.json"
    $candidate | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $path -Encoding ASCII
    return $path
}

function Get-LatestVerifiedPromotionEvidence {
    # Read-only. Scans the rollback-baseline-candidate-*.json evidence files
    # Save-RollbackBaselineApprovalCandidate writes after a fully-verified
    # deployment run of THIS tool -- distinct from, and never a substitute
    # for, the separately-authorized approved rollback baseline. Returns,
    # for $Name, the most recently recorded candidate whose image(s)
    # independently re-verify against the registry RIGHT NOW (never trusts
    # a recorded digest string on its own, exactly like
    # Approve-RollbackBaseline's own re-verification). A malformed,
    # unreadable, or no-longer-verifiable candidate is skipped rather than
    # thrown, so one corrupt/stale file can neither falsely grant trust nor
    # block every other candidate from being considered. Returns $null when
    # no candidate for $Name re-verifies.
    param(
        [Parameter(Mandatory = $true)][string]$RuntimeDir,
        [Parameter(Mandatory = $true)][string]$Name
    )
    if (-not (Test-Path -LiteralPath $RuntimeDir)) { return $null }
    $files = @(Get-ChildItem -LiteralPath $RuntimeDir -Filter 'rollback-baseline-candidate-*.json' -File -ErrorAction SilentlyContinue)
    $best = $null
    foreach ($file in $files) {
        try {
            $candidate = Get-Content -LiteralPath $file.FullName -Raw | ConvertFrom-Json
            if ($candidate.schemaVersion -ne 1) { continue }
            if ($Name -notin @($candidate.names)) { continue }
            $recorded = [string]$candidate.images.$Name
            if ($recorded -notmatch ('^ai-investment-registry:5000/ai-investment/' + [regex]::Escape($Name) + '@(sha256:[a-f0-9]{64})$')) { continue }
            $recordedDigest = $Matches[1]
            # Re-verify against the registry by digest right now -- never
            # trust the candidate file's own recorded string in isolation.
            $current = Get-VerifiedRegistryImage $Name $recordedDigest
            if ($current -ne $recorded) { continue }
            $recordedAt = [DateTime]::Parse([string]$candidate.recordedAtUtc, $null, [Globalization.DateTimeStyles]::RoundtripKind)
            if ($best -and $recordedAt -le $best.RecordedAtUtc) { continue }
            $initImage = $null
            if ($candidate.PSObject.Properties.Name -contains 'initImages' -and $candidate.initImages) {
                foreach ($prop in $candidate.initImages.PSObject.Properties) {
                    $entry = $prop.Value
                    if ([string]$entry.component -ne $Name) { continue }
                    $initRecorded = [string]$entry.image
                    if ($initRecorded -notmatch ('^ai-investment-registry:5000/ai-investment/' + [regex]::Escape($Name) + '@(sha256:[a-f0-9]{64})$')) { continue }
                    $initDigest = $Matches[1]
                    $initCurrent = Get-VerifiedRegistryImage $Name $initDigest
                    if ($initCurrent -eq $initRecorded) {
                        $initImage = [pscustomobject]@{ ContainerName = $prop.Name; Image = $initCurrent }
                    }
                }
            }
            $best = [pscustomobject]@{ Image = $current; InitImage = $initImage; Tag = [string]$candidate.tag; RecordedAtUtc = $recordedAt }
        } catch { continue }
    }
    return $best
}

function Approve-RollbackBaseline {
    # The ONLY function permitted to advance the approved rollback baseline.
    # Fails closed -- leaving the existing approved manifest completely
    # untouched -- on any of: missing explicit authorization, a missing or
    # malformed candidate file, a registry lookup failure while
    # re-verifying a candidate image, or a mismatch between the candidate's
    # recorded digest and what the registry resolves to right now (this is
    # the "ownership uncertainty" guard: a candidate file's own recorded
    # digest string is never trusted on its own merit).
    param(
        [Parameter(Mandatory = $true)][string]$ProjectRoot,
        [Parameter(Mandatory = $true)][string]$CandidatePath,
        [switch]$AuthorizeRollbackBaselineApproval
    )
    if (-not $AuthorizeRollbackBaselineApproval) {
        throw "Refusing to advance the approved rollback baseline without -AuthorizeRollbackBaselineApproval -- this is an explicit, separately-authorized action."
    }
    if (-not $CandidatePath -or -not (Test-Path -LiteralPath $CandidatePath)) {
        throw "Rollback-baseline approval candidate not found at '$CandidatePath'. Only a candidate produced by a real, fully-verified prior deployment run may be approved."
    }
    $raw = Get-Content -LiteralPath $CandidatePath -Raw
    try {
        $candidate = $raw | ConvertFrom-Json
    } catch {
        throw "Rollback-baseline approval candidate at '$CandidatePath' is not valid JSON -- refusing to approve. $($_.Exception.Message)"
    }
    if (-not $candidate.schemaVersion -or $candidate.schemaVersion -ne 1) { throw "Rollback-baseline approval candidate has an unsupported or missing schemaVersion." }
    if (-not $candidate.names -or -not $candidate.images) { throw "Rollback-baseline approval candidate is missing 'names' or 'images'." }
    $names = @($candidate.names)
    if ($names.Count -eq 0) { throw "Rollback-baseline approval candidate names no components." }
    $unknown = @($names | Where-Object { $_ -notin $script:RollbackBaselineComponents })
    if ($unknown.Count -gt 0) { throw "Rollback-baseline approval candidate references unknown component(s): $($unknown -join ', ')." }

    # Re-resolve every candidate image against the registry RIGHT NOW --
    # never trust the candidate file's own recorded digest in isolation.
    # Get-VerifiedRegistryImage is the exact same digest/provenance check
    # used everywhere else in the protected deployment path.
    $reverified = @{}
    foreach ($name in $names) {
        $recorded = $candidate.images.$name
        if ([string]::IsNullOrWhiteSpace($recorded)) { throw "Rollback-baseline approval candidate is missing an image for component '$name'." }
        if ($recorded -notmatch '^ai-investment-registry:5000/ai-investment/[^@]+@(sha256:[a-f0-9]{64})$') {
            throw "Rollback-baseline approval candidate has an unexpected image reference shape for component '$name' -- refusing to approve."
        }
        $recordedDigest = $Matches[1]
        # Re-verify against the registry by digest -- missing registry
        # content, a manifest/blob mismatch, or any provenance failure
        # inside Get-VerifiedRegistryImage propagates as a terminating
        # error here, which this function does not catch: the approval
        # aborts and the existing approved manifest is left untouched.
        $current = Get-VerifiedRegistryImage $name $recordedDigest
        if ($current -ne $recorded) {
            throw "Rollback-baseline approval candidate for '$name' no longer matches the registry (recorded '$recorded', registry now resolves '$current') -- refusing to approve a baseline that cannot be freshly re-verified."
        }
        $reverified[$name] = $recordedDigest
    }

    # Re-verify any independently-approved init-container images the
    # candidate recorded, exactly the same way as the main component
    # images above -- never trust the candidate's own recorded digest in
    # isolation, and never derive this from the main component's digest.
    $reverifiedInit = @{}
    if ($candidate.PSObject.Properties.Name -contains 'initImages' -and $candidate.initImages) {
        foreach ($prop in $candidate.initImages.PSObject.Properties) {
            $containerName = $prop.Name
            $entry = $prop.Value
            $component = [string]$entry.component
            $recorded = [string]$entry.image
            if ($component -notin $script:RollbackBaselineComponents) {
                throw "Rollback-baseline approval candidate references an unknown component '$component' for init container '$containerName'."
            }
            if ([string]::IsNullOrWhiteSpace($recorded) -or $recorded -notmatch ('^ai-investment-registry:5000/ai-investment/' + [regex]::Escape($component) + '@(sha256:[a-f0-9]{64})$')) {
                throw "Rollback-baseline approval candidate has an unexpected init-image reference shape for container '$containerName'."
            }
            $recordedDigest = $Matches[1]
            $current = Get-VerifiedRegistryImage $component $recordedDigest
            if ($current -ne $recorded) {
                throw "Rollback-baseline approval candidate for init container '$containerName' no longer matches the registry (recorded '$recorded', registry now resolves '$current') -- refusing to approve."
            }
            $reverifiedInit[$containerName] = $recordedDigest
        }
    }

    $current = Get-ApprovedRollbackBaseline -ProjectRoot $ProjectRoot
    $approvedComponents = @{}
    foreach ($name in $reverified.Keys) { $approvedComponents[$name] = $reverified[$name] }
    $source = @{ kind = 'candidate-approval'; candidatePath = $CandidatePath; candidateTag = [string]$candidate.tag }
    Write-AdvancedRollbackBaseline -ProjectRoot $ProjectRoot -Current $current -ApprovedComponents $approvedComponents -ApprovedInitContainers $reverifiedInit -Source $source
}

function Write-AdvancedRollbackBaseline {
    # Shared by Approve-RollbackBaseline (candidate-driven) and
    # Approve-RollbackBaselineReconciliation (already-running-image-driven):
    # the ONLY code that actually overwrites the approved manifest. Always
    # appends the manifest being superseded to the append-only history file
    # BEFORE overwriting it, always increments baselineVersion by exactly
    # one, and always updates ONLY the components present in
    # $ApprovedComponents -- every other component is carried over
    # unchanged from $Current.
    param(
        [Parameter(Mandatory = $true)][string]$ProjectRoot,
        [Parameter(Mandatory = $true)]$Current,
        [Parameter(Mandatory = $true)][hashtable]$ApprovedComponents,
        # Independently-approved init-container digests, keyed by container
        # name. Optional and additive: omitting it (or passing an empty @{})
        # changes nothing about init-container approvals -- every
        # previously-approved init digest is carried over unchanged, and no
        # init container is ever approved implicitly as a side effect of
        # approving a main component.
        [hashtable]$ApprovedInitContainers = @{},
        [Parameter(Mandatory = $true)]$Source
    )
    $merged = [ordered]@{}
    foreach ($name in $script:RollbackBaselineComponents) {
        $merged[$name] = if ($ApprovedComponents.ContainsKey($name)) { $ApprovedComponents[$name] } else { $Current.Components[$name] }
    }

    # Init-container digests are not a fixed component list -- carry every
    # previously-approved entry forward unchanged, then overlay only the
    # container names explicitly present in $ApprovedInitContainers.
    $mergedInit = [ordered]@{}
    if ($Current.InitContainers) {
        foreach ($key in $Current.InitContainers.Keys) { $mergedInit[$key] = $Current.InitContainers[$key] }
    }
    foreach ($key in $ApprovedInitContainers.Keys) { $mergedInit[$key] = $ApprovedInitContainers[$key] }

    $manifestPath = Get-RollbackBaselineManifestPath -ProjectRoot $ProjectRoot
    $historyPath = Get-RollbackBaselineHistoryPath -ProjectRoot $ProjectRoot
    # Append-only history of the manifest being superseded, before overwriting it.
    (Get-Content -LiteralPath $manifestPath -Raw) | Add-Content -LiteralPath $historyPath -Encoding UTF8

    $newManifest = [ordered]@{
        schemaVersion   = 1
        baselineVersion = $Current.BaselineVersion + 1
        approvedAtUtc   = [DateTime]::UtcNow.ToString('o')
        approvedBy      = [Environment]::UserName
        components      = $merged
        initContainers  = $mergedInit
        advancedFrom    = (@{ baselineVersion = $Current.BaselineVersion } + $Source)
    }
    $newManifest | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $manifestPath -Encoding UTF8
    [pscustomobject]@{ BaselineVersion = $newManifest.baselineVersion; Components = $merged; InitContainers = $mergedInit; ManifestPath = $manifestPath }
}

function Invoke-PlatformApproveRollbackBaseline {
    # platform.ps1 entry point for the 'approve-rollback-baseline' command.
    param(
        [Parameter(Mandatory = $true)][string]$ProjectRoot,
        [string]$RollbackBaselineCandidatePath,
        [switch]$AuthorizeRollbackBaselineApproval
    )
    if (-not $RollbackBaselineCandidatePath) {
        throw "Specify -RollbackBaselineCandidatePath <path to a rollback-baseline-candidate-*.json produced by a prior successful 'deploy'>."
    }
    $result = Approve-RollbackBaseline -ProjectRoot $ProjectRoot -CandidatePath $RollbackBaselineCandidatePath -AuthorizeRollbackBaselineApproval:$AuthorizeRollbackBaselineApproval
    Write-Host "Approved rollback baseline advanced to version $($result.BaselineVersion): $($result.ManifestPath)"
    foreach ($entry in $result.Components.GetEnumerator()) { Write-Host "  $($entry.Key) = $($entry.Value)" }
}

function Get-InterruptedPromotionAuditPath {
    # Review audit directories are named exactly like the tag
    # Deploy-VerifiedRadarComponentsCore generates for a normal run
    # ('review-<yyyyMMdd>-<HHmmss>-<32 hex chars>'). Requiring that exact
    # shape here (rather than accepting an arbitrary path) keeps this
    # recovery tool from ever being pointed at an unreviewed directory.
    param(
        [Parameter(Mandatory = $true)][string]$RuntimeDir,
        [Parameter(Mandatory = $true)][string]$Tag
    )
    if ($Tag -notmatch '^review-\d{8}-\d{6}-[0-9a-f]{32}$') {
        throw "Recovery tag '$Tag' does not match the expected 'review-<date>-<time>-<hex>' shape produced by a real deployment attempt."
    }
    Join-Path $RuntimeDir $Tag
}

function Get-DeploymentHelmHistoryRevision {
    # Reads a Helm history entry's 'revision' field directly (handling both
    # a PSCustomObject and an IDictionary shape) and explicitly normalizes
    # cardinality with @() rather than relying on Get-DeploymentOptionalProperty's
    # incidental unwrap-on-return behaviour for this field -- the same reason
    # 'policyTypes'/'podSelector' elsewhere in this file are read by direct
    # property access instead of through that function. Missing/empty
    # resolves to 0, which never matches a real Helm revision (revisions
    # start at 1), so callers comparing against an expected revision number
    # stay fail-closed with no extra handling needed.
    param($Entry)
    $raw = $null
    if ($Entry -is [System.Collections.IDictionary]) {
        if ($Entry.Contains('revision')) { $raw = $Entry['revision'] }
    } else {
        $property = $Entry.PSObject.Properties['revision']
        if ($null -ne $property) { $raw = $property.Value }
    }
    $values = @($raw)
    if ($values.Count -eq 1) { return [int]$values[0] }
    return 0
}

function Get-DeploymentNormalizedHelmHistory {
    # Takes whatever value is already bound to a variable after
    # 'Invoke-DeploymentCommand ... | ConvertFrom-Json' (never a live
    # pipeline -- see the call site) and returns a flat array of actual
    # Helm history entry objects, or throws fail-closed for anything
    # that isn't one. Isolated into its own function, with $HistoryRaw
    # as a plain parameter rather than pipeline input, specifically so
    # it can be unit-tested directly with a hand-built malformed shape
    # -- including the real, reported defect shape (an outer 1-element
    # array whose single element is itself the real System.Object[] of
    # history records) -- without needing to mock ConvertFrom-Json or
    # run it through the full recovery flow.
    #
    # ConvertFrom-Json's JSON-array result can reach the pipeline as a
    # single collection object rather than enumerated element-by-element
    # (observed, and reproduced directly with Write-Output -NoEnumerate,
    # against this recovery tool's real Windows PowerShell target).
    # Wrapping that live pipeline directly in @() then treats the whole
    # array as ONE element, nesting it: @(Invoke-DeploymentCommand ... |
    # ConvertFrom-Json) produced a 1-element $history whose single entry
    # was itself the real System.Object[] of history records, so every
    # per-entry property read operated on the wrong object and
    # 'revision' resolved to nothing. @() applied to an already-bound
    # variable, instead of to the live pipeline, flattens both that
    # shape and ordinary per-element pipeline enumeration alike.
    param($HistoryRaw, [Parameter(Mandatory = $true)][string]$Tag, [Parameter(Mandatory = $true)][string]$ReleaseName, [Parameter(Mandatory = $true)][int]$ExpectedRevision)
    # ConvertFrom-Json on a genuinely empty JSON array ('[]') returns $null,
    # not an empty array -- a well-known PowerShell quirk -- so a real "no
    # history at all" response from Helm arrives here as $HistoryRaw being
    # $null itself, not as a zero-count array. Checking that explicitly,
    # before @() ever wraps it, keeps that realistic case reported as
    # "empty" rather than folded into the malformed-entry check below
    # (which exists for a $HistoryRaw that is non-null but still contains
    # something that isn't a real, single history entry).
    if ($null -eq $HistoryRaw) {
        throw "Cannot safely recover '$Tag': Helm history for '$ReleaseName' is empty; cannot independently reverify the reviewed revision $ExpectedRevision failure."
    }
    $history = @($HistoryRaw)
    foreach ($entry in $history) {
        if ($null -eq $entry -or $entry -is [System.Array]) {
            throw "Cannot safely recover '$Tag': Helm history for '$ReleaseName' returned a malformed entry (type $(if ($null -eq $entry) { '<null>' } else { $entry.GetType().FullName })); refusing to apply the reviewed historical exception to unexpected history data."
        }
    }
    if ($history.Count -eq 0) {
        throw "Cannot safely recover '$Tag': Helm history for '$ReleaseName' is empty; cannot independently reverify the reviewed revision $ExpectedRevision failure."
    }
    return $history
}

function Get-DeploymentVerifiedHelmChartIdentity {
    # 'helm status -o json' does not report chart identity at all on
    # this Helm version -- confirmed live against the actual cluster:
    # its top-level properties are only name, info, config, manifest,
    # version, namespace, apply_method, with no 'chart' key whatsoever.
    # (An earlier attempt to read a nested chart.metadata object from
    # 'helm status' was itself based on an unverified assumption about
    # Helm's JSON shape; this function replaces that attempt entirely
    # with a source confirmed against real output.) The one verified
    # source for chart identity is Helm's own dedicated 'helm get
    # metadata' command, queried here independently -- this never trusts
    # the caller's expected revision/status as ground truth on their own,
    # it cross-checks metadata's own copies of release name, revision,
    # and status against them, and fails closed if the command itself
    # fails or returns anything missing or malformed. 'helm get metadata'
    # reports 'chart' and 'version' as separate flat strings (e.g.
    # chart="ai-investment-platform", version="0.1.0"); 'helm history
    # -o json' records the combined "<chart>-<version>" string per
    # revision, so this reconstructs that same combined shape for a
    # like-for-like comparison against history, never hardcoding or
    # guessing a match.
    param(
        [Parameter(Mandatory = $true)][string]$Tag,
        [Parameter(Mandatory = $true)][string]$ReleaseName,
        [Parameter(Mandatory = $true)][int]$ExpectedRevision,
        [Parameter(Mandatory = $true)][string]$ExpectedStatus
    )
    try {
        $metadataRaw = Invoke-DeploymentCommand 'helm' @('--kube-context', 'k3d-ai-investment-dev', '-n', 'ai-investment', 'get', 'metadata', $ReleaseName, '-o', 'json') | ConvertFrom-Json
    } catch {
        throw "Cannot safely recover '$Tag': 'helm get metadata' for '$ReleaseName' failed ($($_.Exception.Message)); cannot independently verify chart identity."
    }
    if ($null -eq $metadataRaw) {
        throw "Cannot safely recover '$Tag': 'helm get metadata' for '$ReleaseName' returned no data; cannot independently verify chart identity."
    }
    $metaName = Get-DeploymentOptionalProperty $metadataRaw 'name'
    $metaChart = Get-DeploymentOptionalProperty $metadataRaw 'chart'
    $metaVersion = Get-DeploymentOptionalProperty $metadataRaw 'version'
    $metaRevision = Get-DeploymentOptionalProperty $metadataRaw 'revision'
    $metaStatus = Get-DeploymentOptionalProperty $metadataRaw 'status'
    if ($metaName -isnot [string] -or -not $metaName -or $metaChart -isnot [string] -or -not $metaChart -or $metaVersion -isnot [string] -or -not $metaVersion) {
        throw "Cannot safely recover '$Tag': 'helm get metadata' for '$ReleaseName' is missing or has malformed name/chart/version fields; refusing to apply the reviewed historical exception without independently verified chart identity."
    }
    if ($metaName -ne $ReleaseName) {
        throw "Cannot safely recover '$Tag': 'helm get metadata' reports release name '$metaName', which does not match the expected release '$ReleaseName'; refusing to apply the reviewed historical exception."
    }
    $metaRevisionInt = $null
    if ($metaRevision -is [int] -or $metaRevision -is [long]) {
        $metaRevisionInt = [int]$metaRevision
    } elseif ($metaRevision -is [string] -and $metaRevision -match '^\d+$') {
        $metaRevisionInt = [int]$metaRevision
    }
    if ($null -eq $metaRevisionInt -or $metaRevisionInt -ne $ExpectedRevision) {
        throw "Cannot safely recover '$Tag': 'helm get metadata' reports revision '$metaRevision', which does not match the expected revision $ExpectedRevision; refusing to apply the reviewed historical exception."
    }
    if (-not $metaStatus -or $metaStatus -ne $ExpectedStatus) {
        throw "Cannot safely recover '$Tag': 'helm get metadata' reports status '$metaStatus', which does not match the expected status '$ExpectedStatus'; refusing to apply the reviewed historical exception."
    }
    return "$metaChart-$metaVersion"
}

function Assert-ReviewedHelmFailureException {
    # Narrow, independently-reverified compatibility exception for
    # exactly one pre-existing, already-reviewed Helm failure: revision
    # 2 of the 'ai-investment-platform' release in 'ai-investment',
    # which failed at 2026-10-21T17:48:10.6560239+02:00 due to a
    # Kubernetes server-side-apply field-ownership conflict -- an
    # incident that predates every review tag this recovery tool can
    # be pointed at. This NEVER treats 'not deployed' as generally
    # acceptable: every check below re-verifies live Helm history from
    # scratch, and falls through to a fail-closed throw for anything
    # that is not this exact, pinned revision -- a different release
    # name, a different revision number, a revision 2 that is no
    # longer the latest history entry (e.g. superseded by a later
    # attempt), a non-matching timestamp, a non-matching chart/release
    # identity, or a description that does not match the reviewed
    # failure's signature. It never calls helm upgrade/rollback/install,
    # and never mutates anything.
    param(
        [Parameter(Mandatory = $true)][string]$Tag,
        [Parameter(Mandatory = $true)][string]$ReleaseName,
        [Parameter(Mandatory = $true)][int]$Version,
        [string]$Status
    )
    $expectedRevision = 2
    $expectedReleaseName = 'ai-investment-platform'
    $expectedTimestamp = [DateTimeOffset]::Parse('2026-10-21T17:48:10.6560239+02:00')

    if ($ReleaseName -ne $expectedReleaseName -or $Version -ne $expectedRevision -or $Status -ne 'failed') {
        throw "Cannot safely recover '$Tag': Helm release '$ReleaseName' (revision $Version, status: $Status) is not in a settled 'deployed' state and does not match the one reviewed historical exception (release '$expectedReleaseName' revision $expectedRevision, status 'failed')."
    }

    $historyRaw = Invoke-DeploymentCommand 'helm' @('--kube-context', 'k3d-ai-investment-dev', '-n', 'ai-investment', 'history', $ReleaseName, '-o', 'json') | ConvertFrom-Json
    $history = Get-DeploymentNormalizedHelmHistory -HistoryRaw $historyRaw -Tag $Tag -ReleaseName $ReleaseName -ExpectedRevision $expectedRevision
    $latestRevision = ($history | ForEach-Object { Get-DeploymentHelmHistoryRevision $_ } | Measure-Object -Maximum).Maximum
    if ($latestRevision -ne $expectedRevision) {
        throw "Cannot safely recover '$Tag': Helm revision $expectedRevision is no longer the latest revision for '$ReleaseName' (latest: $latestRevision); refusing to apply the reviewed historical exception to a changed release history."
    }
    $matchingEntries = @($history | Where-Object { (Get-DeploymentHelmHistoryRevision $_) -eq $expectedRevision })
    if ($matchingEntries.Count -ne 1) {
        throw "Cannot safely recover '$Tag': Helm history for '$ReleaseName' does not have exactly one entry for revision $expectedRevision."
    }
    $entry = $matchingEntries[0]
    if ((Get-DeploymentOptionalProperty $entry 'status') -ne 'failed') {
        throw "Cannot safely recover '$Tag': Helm history revision $expectedRevision for '$ReleaseName' is no longer recorded as 'failed'; refusing to apply the reviewed historical exception."
    }
    $updatedRaw = Get-DeploymentOptionalProperty $entry 'updated'
    # ConvertFrom-Json auto-parses an ISO-8601-with-offset string into a
    # Kind=Local [DateTime] (observed on this PowerShell/.NET version),
    # and that Kind=Local value's own default ToString() is misleading
    # (it reads as though the offset/precision were lost) -- but the
    # underlying instant is intact, and [DateTimeOffset]::new() on a
    # Kind=Local value reconstructs it correctly regardless of this
    # machine's own local timezone. Calling [DateTimeOffset]::Parse() on
    # an already-parsed [DateTime] would instead implicitly re-stringify
    # it first (through that same misleading default format) and lose
    # precision/offset -- handle both shapes explicitly so the exact
    # pinned instant is never compared against a corrupted one.
    $updated = $null
    if ($updatedRaw -is [DateTime]) {
        $updated = [DateTimeOffset]::new($updatedRaw)
    } elseif ($updatedRaw) {
        try { $updated = [DateTimeOffset]::Parse([string]$updatedRaw) } catch { $updated = $null }
    }
    if ($null -eq $updated -or $updated.UtcTicks -ne $expectedTimestamp.UtcTicks) {
        throw "Cannot safely recover '$Tag': Helm history revision $expectedRevision for '$ReleaseName' has an unexpected or unparseable timestamp ('$updatedRaw'); refusing to apply the reviewed historical exception."
    }
    $entryChart = Get-DeploymentOptionalProperty $entry 'chart'
    $verifiedChart = Get-DeploymentVerifiedHelmChartIdentity -Tag $Tag -ReleaseName $ReleaseName -ExpectedRevision $expectedRevision -ExpectedStatus 'failed'
    if (-not $verifiedChart -or $entryChart -ne $verifiedChart) {
        throw "Cannot safely recover '$Tag': Helm history revision $expectedRevision's chart ('$entryChart') does not match the independently verified release chart ('$verifiedChart'); refusing to apply the reviewed historical exception."
    }
    # Signature match for the reviewed cause (a Kubernetes server-side-
    # apply conflict on the Research Engine Deployment, owned by
    # 'kubectl-patch', on the container and init-container image fields),
    # not a literal string pin on wording that varies by Helm/Kubernetes
    # version. Revision number, latest-ness, timestamp, and chart/
    # release identity above already pin this to the one specific
    # incident; this is an additional content check on top, not the
    # only thing standing between this and a bypass -- so it is deliberately
    # specific (the exact object, the exact field manager, the exact two
    # field paths), not a loose "mentions a conflict" check that a
    # different, unrelated SSA conflict could also satisfy. (An earlier
    # version of this check additionally required the literal English
    # word 'field', which this Helm/Kubernetes version's real error text
    # never emits -- confirmed live -- so a genuine, exact match of this
    # one reviewed incident was being rejected. That requirement is
    # replaced below with markers tied to the actual incident instead.)
    $description = Get-DeploymentOptionalProperty $entry 'description'
    $conflictSignatureMarkers = @(
        ('Upgrade "' + $ReleaseName + '" failed'),
        'conflict',
        'kubectl-patch',
        'Kind=Deployment',
        'ai-investment/research-engine',
        '.spec.template.spec.containers[name="research-engine"].image',
        '.spec.template.spec.initContainers[name="wait-for-research-migrations"].image'
    )
    $missingConflictMarkers = @($conflictSignatureMarkers | Where-Object { -not $description -or $description -notmatch [regex]::Escape($_) })
    if ($missingConflictMarkers.Count -gt 0) {
        throw "Cannot safely recover '$Tag': Helm history revision $expectedRevision's recorded description ('$description') is missing required server-side-apply conflict signature markers (missing: $($missingConflictMarkers -join '; ')); refusing to apply the reviewed historical exception."
    }
    Write-Host "Helm revision $expectedRevision for '$ReleaseName' independently reverified against live Helm history as the one reviewed pre-existing failure (server-side-apply field-ownership conflict, $($expectedTimestamp.ToString('o'))); treating Helm as settled enough to proceed with the remaining independent checks."
}

function Invoke-InterruptedPromotionRecovery {
    # One-time, explicitly-authorized recovery for a promotion that
    # reached Kubernetes rollout but was interrupted before
    # Save-RollbackBaselineApprovalCandidate could record promotion
    # evidence for it (e.g. the now-fixed StrictMode optional-label defect
    # inside Complete-DeploymentSafety). This is intentionally separate
    # from Deploy-VerifiedRadarComponentsCore: it never builds, pushes,
    # patches a workload, mutates PostgreSQL, changes a NetworkPolicy, or
    # advances the approved rollback baseline. Every check below reads
    # live cluster/registry state (kubectl get/exec, registry HTTP
    # GET/HEAD) -- nothing here can mutate anything -- and is re-verified
    # from scratch; an operator-supplied digest or the running container's
    # own image string is never trusted as ground truth on its own.
    #
    # Without -AuthorizeRecovery this performs every verification and
    # returns a preview describing exactly what would be recorded, but
    # writes nothing. With -AuthorizeRecovery, and only once every check
    # has independently passed, it writes a rollback-baseline-candidate
    # file in exactly the schema Get-LatestVerifiedPromotionEvidence
    # already reads (schemaVersion/tag/names/images/initImages/
    # recordedAtUtc), with an additional 'origin' field identifying it as
    # a recovered record. It never calls Approve-RollbackBaseline or
    # Write-AdvancedRollbackBaseline, so the approved rollback baseline is
    # always left untouched by this function.
    param(
        [Parameter(Mandatory = $true)][string]$ProjectRoot,
        [Parameter(Mandatory = $true)][string]$RuntimeDir,
        [Parameter(Mandatory = $true)][string]$Tag,
        [Parameter(Mandatory = $true)][string[]]$Names,
        [switch]$AuthorizeRecovery
    )
    $unknown = @($Names | Where-Object { $_ -notin $script:RollbackBaselineComponents })
    if ($unknown.Count -gt 0) { throw "Recovery requested for unknown component(s): $($unknown -join ', ')." }
    if ('portfolio-service' -in $Names) {
        throw "Recovery for 'portfolio-service' is out of scope for this one-time tool -- its durable-state observation requires a migration manifest this recovery path does not compute. Extend it deliberately, with its own tests, if that recovery is ever needed."
    }
    if ('research-engine' -notin $Names) {
        throw "Recovery currently supports only 'research-engine' (its durable-state observation is the only one this tool computes); got: $($Names -join ', ')."
    }

    $auditDir = Get-InterruptedPromotionAuditPath -RuntimeDir $RuntimeDir -Tag $Tag
    $requiredFiles = @('original-deployments.json', 'original-workloads.json', 'isolation.json', 'observations.jsonl')
    $missingFiles = @($requiredFiles | Where-Object { -not (Test-Path -LiteralPath (Join-Path $auditDir $_) -PathType Leaf) })
    if ($missingFiles.Count -gt 0) {
        throw "Cannot safely recover '$Tag': missing saved review artifact(s) $($missingFiles -join ', ') under '$auditDir'. Refusing to synthesize missing historical evidence."
    }
    $rollbackSnapshotPath = Join-Path $RuntimeDir "rollback-$Tag.json"
    if (-not (Test-Path -LiteralPath $rollbackSnapshotPath -PathType Leaf)) {
        throw "Cannot safely recover '$Tag': missing the pre-deployment rollback snapshot at '$rollbackSnapshotPath'."
    }

    $savedWorkloads = Get-Content -LiteralPath (Join-Path $auditDir 'original-workloads.json') -Raw | ConvertFrom-Json
    $savedDeployments = Get-Content -LiteralPath (Join-Path $auditDir 'original-deployments.json') -Raw | ConvertFrom-Json
    $observationLines = @(Get-Content -LiteralPath (Join-Path $auditDir 'observations.jsonl') | Where-Object { $_.Trim().Length -gt 0 })
    if ($observationLines.Count -eq 0) {
        throw "Cannot safely recover '$Tag': 'observations.jsonl' exists but records no durable-state samples from the interrupted run's own quiet period. Refusing to synthesize a missing observation."
    }
    $lastObservation = $observationLines[-1] | ConvertFrom-Json
    if ($lastObservation.fingerprint -notmatch '^[a-f0-9]{64}$') {
        throw "Cannot safely recover '$Tag': the last recorded durable-state observation has no valid fingerprint."
    }

    # 1. Bootstrap isolation must be either already fully removed, or be
    # exactly the single deployment-owned policy saved for this
    # interrupted attempt -- never trusted as safe to leave in place or
    # remove automatically on anything less than an exact shape match
    # against this attempt's own saved review evidence, and never
    # assumed stale merely because the saved evidence is old. A policy
    # removal, if one is needed, happens later, only once every other
    # check below has independently passed, and only under
    # -AuthorizeRecovery.
    $policies = Invoke-DeploymentKubectl @('get', 'networkpolicies', '-o', 'json') | ConvertFrom-Json
    $isolationStillActive = $false
    if (@($policies.items).Count -ne 0) {
        $savedIsolation = Get-Content -LiteralPath (Join-Path $auditDir 'isolation.json') -Raw | ConvertFrom-Json
        $savedSpec = Get-DeploymentOptionalProperty $savedIsolation 'spec'
        $savedPodSelector = $null
        $savedShapeValid = $false
        if ($savedSpec -and $savedSpec.PSObject.Properties['podSelector'] -and $savedSpec.PSObject.Properties['policyTypes']) {
            $savedPodSelector = $savedSpec.podSelector
            $savedIngressRaw = Get-DeploymentOptionalProperty $savedSpec 'ingress'
            $savedShapeValid = ((Get-DeploymentOptionalProperty $savedIsolation.metadata 'name') -eq 'research-bootstrap-isolation' -and
                $null -ne $savedPodSelector -and
                @($savedSpec.policyTypes).Count -eq 1 -and $savedSpec.policyTypes[0] -eq 'Ingress' -and
                @($savedIngressRaw | Where-Object { $null -ne $_ }).Count -eq 0)
        }
        $matchingPolicies = @($policies.items | Where-Object { $_.metadata.name -eq 'research-bootstrap-isolation' })
        $liveMatches = $false
        if ($savedShapeValid -and $matchingPolicies.Count -eq 1 -and @($policies.items).Count -eq 1) {
            $livePolicy = $matchingPolicies[0]
            $liveIngressRaw = Get-DeploymentOptionalProperty $livePolicy.spec 'ingress'
            $liveMatches = (($livePolicy.spec.podSelector | ConvertTo-Json -Depth 20 -Compress) -eq ($savedPodSelector | ConvertTo-Json -Depth 20 -Compress) -and
                @($livePolicy.spec.policyTypes).Count -eq 1 -and $livePolicy.spec.policyTypes[0] -eq 'Ingress' -and
                @($liveIngressRaw | Where-Object { $null -ne $_ }).Count -eq 0)
        }
        if (-not $liveMatches) {
            throw "Cannot safely recover '$Tag': a NetworkPolicy is still present ($(@($policies.items | ForEach-Object { $_.metadata.name }) -join ', ')) and does not verifiably match this interrupted attempt's own saved review evidence. Refusing to assume it is safe to leave in place or remove automatically."
        }
        $isolationStillActive = $true

        # Only relevant while isolation is still active: before trusting
        # the cluster as quiescent enough to remove it later, confirm no
        # scheduled or internal work is concurrently running -- the same
        # check already used before restoring research ingress from a
        # stale isolation policy (Resolve-DeploymentStaleIsolation in
        # platform.ps1).
        $jobs = Invoke-DeploymentKubectl @('get', 'jobs,cronjobs', '-o', 'json') | ConvertFrom-Json
        if (@($jobs.items | Where-Object { ($_.kind -eq 'CronJob' -and -not $_.spec.suspend) -or ($_.kind -eq 'Job' -and $_.status.active -gt 0) }).Count) {
            throw "Cannot safely recover '$Tag': scheduled/internal work is active; refusing to remove isolation or record evidence now."
        }
    }

    # 2. Every unrelated workload (anything not in $Names) must be
    # byte-identical to what was saved before the interrupted attempt.
    $currentWorkloads = Invoke-DeploymentKubectl @('get', 'deployments,statefulsets', '-o', 'json') | ConvertFrom-Json
    $before = @($savedWorkloads.items | Where-Object { $_.metadata.name -notin $Names })
    $after = @($currentWorkloads.items | Where-Object { $_.metadata.name -notin $Names })
    if ($before.Count -ne $after.Count) { throw "Cannot safely recover '$Tag': unrelated workload set changed since the interrupted attempt." }
    foreach ($item in $before) {
        $other = @($after | Where-Object { $_.kind -eq $item.kind -and $_.metadata.name -eq $item.metadata.name })
        if ($other.Count -ne 1 -or ($item.spec | ConvertTo-Json -Depth 100 -Compress) -ne ($other[0].spec | ConvertTo-Json -Depth 100 -Compress)) {
            throw "Cannot safely recover '$Tag': unrelated workload '$($item.metadata.name)' drifted since the interrupted attempt."
        }
    }

    # 3. Independently re-verify each named component: rollout fully
    # propagated, exactly one stable ready pod, running image resolved and
    # re-verified against the registry (never trusted from the deployment
    # spec string or any operator-supplied digest), and the pod's actual
    # imageID matches that independently-verified image.
    $pods = Invoke-DeploymentKubectl @('get', 'pods', '-o', 'json') | ConvertFrom-Json
    $recoveredImages = @{}
    $recoveredInitImages = @{}
    foreach ($name in $Names) {
        $deployment = Invoke-DeploymentKubectl @('get', 'deployment', $name, '-o', 'json') | ConvertFrom-Json
        $savedRecord = $savedDeployments.PSObject.Properties[$name]
        if (-not $savedRecord -or -not $savedRecord.Value -or $savedRecord.Value.metadata.uid -ne $deployment.metadata.uid) {
            throw "Cannot safely recover '$Tag': '$name' does not match the deployment object identity (uid) saved before the interrupted attempt -- it may have been deleted and recreated since."
        }
        if ($deployment.status.observedGeneration -ne $deployment.metadata.generation) {
            throw "Cannot safely recover '$Tag': '$name' has not finished rolling out (observedGeneration $($deployment.status.observedGeneration) != generation $($deployment.metadata.generation))."
        }
        $desiredRaw = Get-DeploymentOptionalProperty $deployment.spec 'replicas'
        $desired = if ($null -eq $desiredRaw) { 1 } else { [int]$desiredRaw }
        if ([int](Get-DeploymentOptionalProperty $deployment.status 'readyReplicas') -ne $desired -or
            [int](Get-DeploymentOptionalProperty $deployment.status 'updatedReplicas') -ne $desired -or
            [int](Get-DeploymentOptionalProperty $deployment.status 'availableReplicas') -ne $desired) {
            throw "Cannot safely recover '$Tag': '$name' is not fully ready/updated/available at the expected replica count ($desired)."
        }

        $matchingPods = @($pods.items | Where-Object { (Get-DeploymentOptionalProperty (Get-DeploymentOptionalProperty $_.metadata 'labels') 'app.kubernetes.io/component') -eq $name })
        if ($matchingPods.Count -ne 1 -or (Get-DeploymentOptionalProperty $matchingPods[0].metadata 'deletionTimestamp')) {
            throw "Cannot safely recover '$Tag': '$name' does not have exactly one stable, non-terminating pod."
        }
        $pod = $matchingPods[0]
        $status = @($pod.status.containerStatuses | Where-Object name -eq $name)
        if ($status.Count -ne 1 -or -not $status[0].ready) { throw "Cannot safely recover '$Tag': '$name' container is not ready." }

        $container = @($deployment.spec.template.spec.containers | Where-Object { $_.name -eq $name })
        if ($container.Count -ne 1) { throw "Cannot safely recover '$Tag': expected exactly one '$name' container." }
        $reference = $null
        foreach ($repository in @("ai-investment-registry:5000/ai-investment/$name", "localhost:5001/ai-investment/$name")) {
            $imagePattern = '^' + [regex]::Escape($repository) + '[:@](.+)$'
            if ($container[0].image -match $imagePattern) { $reference = $Matches[1]; break }
        }
        if (-not $reference) { throw "Cannot safely recover '$Tag': unexpected running image repository for '$name'." }
        $verifiedImage = Get-VerifiedRegistryImage $name $reference
        Assert-VerifiedPodImage $verifiedImage $status[0].imageID
        $recoveredImages[$name] = $verifiedImage

        if ($name -eq 'research-engine') {
            $initContainer = @($deployment.spec.template.spec.initContainers | Where-Object { $_.name -eq 'wait-for-research-migrations' })
            if ($initContainer.Count -ne 1) { throw "Cannot safely recover '$Tag': expected exactly one 'wait-for-research-migrations' init container." }
            $initStatus = @($pod.status.initContainerStatuses | Where-Object name -eq 'wait-for-research-migrations')
            if ($initStatus.Count -ne 1 -or $null -eq $initStatus[0].state.terminated -or $initStatus[0].state.terminated.exitCode -ne 0) {
                throw "Cannot safely recover '$Tag': migration-wait init container did not terminate successfully."
            }
            $initReference = $null
            foreach ($repository in @("ai-investment-registry:5000/ai-investment/$name", "localhost:5001/ai-investment/$name")) {
                $imagePattern = '^' + [regex]::Escape($repository) + '[:@](.+)$'
                if ($initContainer[0].image -match $imagePattern) { $initReference = $Matches[1]; break }
            }
            if (-not $initReference) { throw "Cannot safely recover '$Tag': unexpected migration-wait init image repository." }
            $verifiedInitImage = Get-VerifiedRegistryImage $name $initReference
            Assert-VerifiedPodImage $verifiedInitImage $initStatus[0].imageID
            $recoveredInitImages['wait-for-research-migrations'] = @{ component = $name; image = $verifiedInitImage }

            $envs = $container[0].env
            if (($envs | Where-Object name -eq 'AIP_RESEARCH_OPPORTUNITY_SCHEDULER_ENABLED').value -ne 'false') {
                throw "Cannot safely recover '$Tag': research-engine scheduler is not confirmed disabled."
            }
            if (($envs | Where-Object name -eq 'AIP_RESEARCH_OPPORTUNITY_RECOVERY_PAUSED').value -ne 'true') {
                throw "Cannot safely recover '$Tag': research-engine recovery pause is not confirmed active."
            }
        }
    }

    # 3b. While isolation is still active, also confirm Helm itself
    # considers the release settled and healthy right now -- not
    # mid-upgrade or pending -- before this recovery certifies a rollout
    # as genuinely complete from Helm's perspective too, not only
    # kubectl's.
    if ($isolationStillActive) {
        $release = Invoke-DeploymentCommand 'helm' @('--kube-context', 'k3d-ai-investment-dev', '-n', 'ai-investment', 'status', 'ai-investment-platform', '-o', 'json') | ConvertFrom-Json
        $releaseInfo = Get-DeploymentOptionalProperty $release 'info'
        $releaseStatus = if ($releaseInfo) { Get-DeploymentOptionalProperty $releaseInfo 'status' } else { $null }
        $releaseName = Get-DeploymentOptionalProperty $release 'name'
        # 'helm status -o json' reports no chart identity at all on this
        # Helm version (confirmed live: its top-level properties are only
        # name, info, config, manifest, version, namespace, apply_method)
        # -- chart identity is independently verified inside
        # Assert-ReviewedHelmFailureException itself, via Helm's own
        # dedicated 'helm get metadata' command, rather than being read
        # from (a wrong property of) this status response.
        if (-not $releaseName -or [int]$release.version -lt 1) {
            throw "Cannot safely recover '$Tag': Helm release is missing or has no revision."
        }
        if ($releaseStatus -ne 'deployed') {
            # Not a blanket bypass: this only ever accepts the one exact,
            # already-reviewed historical Helm failure (see
            # Assert-ReviewedHelmFailureException below), re-verified live
            # against Helm history and an independent 'helm get metadata'
            # chart-identity check every time. Everything else -- a
            # different release, a different revision, a revision 2 that
            # is no longer latest, a different timestamp/chart/description
            # -- still falls through to a fail-closed throw.
            Assert-ReviewedHelmFailureException -Tag $Tag -ReleaseName $releaseName -Version ([int]$release.version) -Status $releaseStatus
        }
    }

    # 4. Durable-state (PostgreSQL) consistency: a fresh observation taken
    # right now must match the LAST verified-quiet observation that was
    # actually recorded during the interrupted run's own quiet period --
    # never a freshly-taken baseline substituted for history that was
    # never actually observed back then.
    $researchPod = @($pods.items | Where-Object { (Get-DeploymentOptionalProperty (Get-DeploymentOptionalProperty $_.metadata 'labels') 'app.kubernetes.io/component') -eq 'research-engine' })
    if ($researchPod.Count -ne 1) { throw "Cannot safely recover '$Tag': ambiguous research-engine pod for durable-state observation." }
    $observationGate = @{ ResearchPod = $researchPod[0].metadata.name; Migrations = $null }
    $freshObservation = Invoke-DeploymentObservation $observationGate
    if ($freshObservation.fingerprint -ne $lastObservation.fingerprint) {
        throw "Cannot safely recover '$Tag': current durable-state fingerprint ($($freshObservation.fingerprint)) does not match the last fingerprint recorded during the interrupted run's own quiet period ($($lastObservation.fingerprint))."
    }

    if (-not $AuthorizeRecovery) {
        $isolationNote = if ($isolationStillActive) { " Its still-active 'research-bootstrap-isolation' policy verifiably matches this attempt's own saved evidence and would be removed only once authorized." } else { '' }
        Write-Host "Preview only -- every recovery check passed for '$Tag', but no promotion evidence was recorded.$isolationNote Re-run with -AuthorizeRecovery to record it."
        return [pscustomobject]@{
            Tag                   = $Tag
            Names                 = @($Names)
            VerifiedImages        = $recoveredImages
            VerifiedInitImages    = $recoveredInitImages
            DurableFingerprint    = $freshObservation.fingerprint
            AuditPath             = $auditDir
            EvidenceRecorded      = $false
            IsolationWasActive    = $isolationStillActive
            IsolationRemoved      = $false
        }
    }

    # Remove the exact, already-verified isolation policy before recording
    # evidence -- but only now, after every check above (including the
    # fresh durable-state fingerprint match) has independently passed,
    # and only because -AuthorizeRecovery was explicitly given. A run
    # where isolation was already removed leaves this as a no-op.
    if ($isolationStillActive) {
        Invoke-DeploymentKubectl @('delete', 'networkpolicy', 'research-bootstrap-isolation', '--wait=true') | Out-Null
        Write-Host "Isolation removed after verification for '$Tag'. Recovery stays paused; no automatic rollback."
    }

    $candidate = [ordered]@{
        schemaVersion = 1
        tag           = $Tag
        recordedAtUtc = [DateTime]::UtcNow.ToString('o')
        names         = @($Names)
        images        = $recoveredImages
        initImages    = $recoveredInitImages
        origin        = 'recovered-interrupted-promotion'
        recoveryNote  = 'Promotion reached Kubernetes rollout and was independently re-verified after the fact; the original attempt was interrupted before this evidence could be recorded automatically.'
    }
    $path = Join-Path $RuntimeDir "rollback-baseline-candidate-$Tag.json"
    $candidate | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $path -Encoding ASCII
    Write-Host "Recorded recovered promotion evidence for '$Tag' at '$path'. Approved rollback baseline unchanged -- run 'approve-rollback-baseline' separately if you also want to advance it."
    [pscustomobject]@{
        Tag                   = $Tag
        Names                 = @($Names)
        VerifiedImages        = $recoveredImages
        VerifiedInitImages    = $recoveredInitImages
        DurableFingerprint    = $freshObservation.fingerprint
        AuditPath             = $auditDir
        EvidenceRecorded      = $true
        EvidencePath          = $path
        IsolationWasActive    = $isolationStillActive
        IsolationRemoved      = $isolationStillActive
    }
}

function Invoke-PlatformRecoverInterruptedPromotion {
    # platform.ps1 entry point for the 'recover-interrupted-promotion' command.
    param(
        [Parameter(Mandatory = $true)][string]$ProjectRoot,
        [Parameter(Mandatory = $true)][string]$RuntimeDir,
        [string]$RecoveryTag,
        [string[]]$RecoveryComponent,
        [switch]$AuthorizeRecovery
    )
    if (-not $RecoveryTag) {
        throw "Specify -RecoveryTag <review-tag of the interrupted attempt>, e.g. -RecoveryTag 'review-20261021-210233-d3950bcdb04844a0b32078eeb5da4c24'."
    }
    if (-not $RecoveryComponent -or $RecoveryComponent.Count -eq 0) {
        throw "Specify -RecoveryComponent <component>, e.g. -RecoveryComponent research-engine."
    }
    $result = Invoke-InterruptedPromotionRecovery -ProjectRoot $ProjectRoot -RuntimeDir $RuntimeDir -Tag $RecoveryTag -Names $RecoveryComponent -AuthorizeRecovery:$AuthorizeRecovery
    foreach ($entry in $result.VerifiedImages.GetEnumerator()) { Write-Host "  $($entry.Key) = $($entry.Value)" }
    if ($result.EvidenceRecorded) { Write-Host "Evidence file: $($result.EvidencePath)" }
}

function Approve-RollbackBaselineReconciliation {
    # Fail-closed reconciliation for components that are ALREADY running
    # (no deploy-produced approval candidate exists for them) -- e.g. after
    # an out-of-band or manually-applied image change. This NEVER trusts an
    # operator's claimed tag/digest match: it independently re-derives the
    # running pod digest from Kubernetes and the registry digest for the
    # claimed tag, and only proceeds if every selected component's pods are
    # fully ready, consistent across all replicas under their EXACT
    # deployment selector, and match the freshly-resolved registry digest.
    #
    # Without -AuthorizeRollbackBaselineReconciliation this performs the
    # exact same verification and prints a preview, but never writes
    # anything -- the manifest is untouched on any validation failure or on
    # an unauthorized (preview) run.
    param(
        [Parameter(Mandatory = $true)][string]$ProjectRoot,
        [Parameter(Mandatory = $true)][string]$KubeContext,
        [Parameter(Mandatory = $true)][string]$KubeNamespace,
        [Parameter(Mandatory = $true)][hashtable]$ComponentTags,
        [switch]$AuthorizeRollbackBaselineReconciliation,
        [string]$ReconciliationReason
    )
    # Explicit, exact selection required -- never an ambient/default context.
    if ($KubeContext -cne 'k3d-ai-investment-dev' -or $KubeNamespace -cne 'ai-investment') {
        throw "Refusing to reconcile: -KubeContext/-KubeNamespace must be exactly 'k3d-ai-investment-dev'/'ai-investment' (got '$KubeContext'/'$KubeNamespace')."
    }
    if ($AuthorizeRollbackBaselineReconciliation -and [string]::IsNullOrWhiteSpace($ReconciliationReason)) {
        throw "Specify -ReconciliationReason when passing -AuthorizeRollbackBaselineReconciliation -- the reason is recorded in the manifest history."
    }
    if (-not $ComponentTags -or $ComponentTags.Count -eq 0) {
        throw "Specify at least one component=tag pair to reconcile."
    }
    # Each key is either "<component>" (the main container) or
    # "<component>/<initContainerName>" (an independently-approved init
    # container, e.g. research-engine's migration-wait container). The
    # container name is never trusted on its own -- it must actually exist
    # on the live Deployment's pod template, checked below.
    $entries = @()
    foreach ($key in $ComponentTags.Keys) {
        if ($key -notmatch '^([a-z0-9-]+)(?:/([a-z0-9-]+))?$') {
            throw "Invalid -ComponentTags key '$key' -- expected '<component>' or '<component>/<initContainerName>'."
        }
        $componentName = $Matches[1]
        $isInit = [bool]$Matches[2]
        $containerName = if ($isInit) { $Matches[2] } else { $componentName }
        $entries += [pscustomobject]@{ Component = $componentName; Container = $containerName; Tag = $ComponentTags[$key]; IsInit = $isInit }
    }
    $unknown = @(@($entries | ForEach-Object { $_.Component }) | Select-Object -Unique | Where-Object { $_ -notin $script:RollbackBaselineComponents })
    if ($unknown.Count -gt 0) { throw "Unknown component(s): $($unknown -join ', ') -- only $($script:RollbackBaselineComponents -join ', ') may be reconciled." }
    foreach ($entry in $entries) {
        if ($entry.Tag -notmatch '^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$') { throw "Invalid registry tag '$($entry.Tag)'." }
    }

    $current = Get-ApprovedRollbackBaseline -ProjectRoot $ProjectRoot
    $resolvedDigests = @{}
    $resolvedInitDigests = @{}
    $previewLines = @()
    foreach ($name in (@($entries | ForEach-Object { $_.Component }) | Select-Object -Unique | Sort-Object)) {
        $deployment = Invoke-DeploymentKubectl @('get', 'deployment', $name, '-o', 'json') | ConvertFrom-Json
        if ($deployment.metadata.name -ne $name -or $deployment.metadata.namespace -ne $KubeNamespace) {
            throw "Unexpected deployment identity for '$name'."
        }
        if (-not $deployment.spec.replicas -or $deployment.spec.replicas -lt 1 -or
            $deployment.status.readyReplicas -ne $deployment.spec.replicas -or
            $deployment.metadata.generation -ne $deployment.status.observedGeneration) {
            throw "Deployment '$name' is not in a fully ready, settled state (readyReplicas/replicas/generation must all agree) -- refusing to reconcile against a moving target."
        }

        # Exact pod selector from the live Deployment spec -- never a
        # hand-maintained label guess.
        $selectorLabels = $deployment.spec.selector.matchLabels
        if (-not $selectorLabels) { throw "Deployment '$name' has no matchLabels selector -- refusing an inexact pod lookup." }
        $selectorArg = (@($selectorLabels.PSObject.Properties | ForEach-Object { "$($_.Name)=$($_.Value)" }) -join ',')
        # Strict-mode-safe: Kubernetes omits 'metadata.deletionTimestamp'
        # entirely (omitempty) for a pod that is not terminating, so a direct
        # '.metadata.deletionTimestamp' access throws under Set-StrictMode
        # instead of evaluating falsy, exactly like Get-DeploymentOptionalProperty's
        # own doc comment describes. Use the same safe lookup used elsewhere in
        # this deployment path (platform.ps1's Get-DeploymentOptionalProperty) so a
        # terminating pod is still excluded and a non-terminating one is still kept.
        $pods = @((Invoke-DeploymentKubectl @('get', 'pods', '-l', $selectorArg, '-o', 'json') | ConvertFrom-Json).items | Where-Object { -not (Get-DeploymentOptionalProperty $_.metadata 'deletionTimestamp') })
        if ($pods.Count -ne $deployment.status.readyReplicas) {
            throw "Deployment '$name' pod count ($($pods.Count)) under its exact selector does not match readyReplicas ($($deployment.status.readyReplicas)) -- refusing to reconcile with inconsistent replicas."
        }

        foreach ($entry in ($entries | Where-Object { $_.Component -eq $name })) {
            # Dynamically discover whether the targeted container is really
            # configured on this live deployment -- never assume a
            # container name supplied on the command line actually exists.
            $containerDefined = if ($entry.IsInit) {
                @($deployment.spec.template.spec.initContainers | Where-Object { $_.name -eq $entry.Container }).Count -eq 1
            } else {
                @($deployment.spec.template.spec.containers | Where-Object { $_.name -eq $entry.Container }).Count -eq 1
            }
            if (-not $containerDefined) {
                $kind = if ($entry.IsInit) { 'init ' } else { '' }
                throw "Deployment '$name' has no ${kind}container named '$($entry.Container)' -- refusing to reconcile an undefined container."
            }

            # Tag -> registry digest, handling OCI/Docker index and manifest
            # media types exactly like every other registry resolution in
            # this file (GET-based manifest resolution; HEAD is used only
            # for blob existence inside Get-VerifiedRegistryImage, never
            # confused with tag/digest resolution here).
            $resolvedImage = Get-VerifiedRegistryImage $name $entry.Tag
            if ($resolvedImage -notmatch '(sha256:[a-f0-9]{64})$') { throw "Unexpected resolved image shape for '$name'." }
            $resolvedDigest = $Matches[1]

            foreach ($pod in $pods) {
                $statuses = if ($entry.IsInit) { $pod.status.initContainerStatuses } else { $pod.status.containerStatuses }
                $status = @($statuses | Where-Object name -eq $entry.Container)
                if ($status.Count -ne 1 -or -not $status[0].ready) { throw "Pod '$($pod.metadata.name)' container '$($entry.Container)' is missing or not ready." }
                # Every replica must independently match the freshly-resolved
                # registry digest -- this is what rejects both drift
                # (running image differs from the claimed tag's real
                # digest) and inconsistent replicas (one pod stale relative
                # to another).
                Assert-VerifiedPodImage $resolvedImage $status[0].imageID
            }

            if ($entry.IsInit) {
                $resolvedInitDigests[$entry.Container] = $resolvedDigest
                $previewLines += "  $name/$($entry.Container) [init] : $($current.InitContainers[$entry.Container]) -> $resolvedDigest (tag '$($entry.Tag)', $($pods.Count) pod(s) verified)"
            } else {
                $resolvedDigests[$name] = $resolvedDigest
                $previewLines += "  $name : $($current.Components[$name]) -> $resolvedDigest (tag '$($entry.Tag)', $($pods.Count) pod(s) verified)"
            }
        }
    }

    Write-Host "Rollback-baseline reconciliation preview (current baselineVersion $($current.BaselineVersion)):"
    foreach ($line in $previewLines) { Write-Host $line }

    if (-not $AuthorizeRollbackBaselineReconciliation) {
        Write-Host "Preview only -- no manifest written. Re-run with -AuthorizeRollbackBaselineReconciliation and -ReconciliationReason to advance the baseline."
        return [pscustomobject]@{ Previewed = $true; Authorized = $false; Components = $resolvedDigests; InitContainers = $resolvedInitDigests }
    }

    $approvedComponents = @{}
    foreach ($name in $resolvedDigests.Keys) { $approvedComponents[$name] = $resolvedDigests[$name] }
    $source = @{ kind = 'reconciliation'; reason = $ReconciliationReason; componentTags = $ComponentTags; approvedAtUtcPreviewed = [DateTime]::UtcNow.ToString('o') }
    Write-AdvancedRollbackBaseline -ProjectRoot $ProjectRoot -Current $current -ApprovedComponents $approvedComponents -ApprovedInitContainers $resolvedInitDigests -Source $source
}

function Invoke-PlatformReconcileRollbackBaseline {
    # platform.ps1 entry point for the 'reconcile-rollback-baseline' command.
    param(
        [Parameter(Mandatory = $true)][string]$ProjectRoot,
        [string[]]$ReconcileComponent,
        [string]$KubeContext,
        [string]$KubeNamespace,
        [switch]$AuthorizeRollbackBaselineReconciliation,
        [string]$RollbackBaselineReconciliationReason
    )
    if (-not $ReconcileComponent -or $ReconcileComponent.Count -eq 0) {
        throw "Specify -ReconcileComponent <component>=<tag> at least once, e.g. -ReconcileComponent 'api-gateway=etf-universe-route-20261013-01'."
    }
    $componentTags = @{}
    foreach ($entry in $ReconcileComponent) {
        # Accepts '<component>=<tag>' (main container) or
        # '<component>/<initContainerName>=<tag>' (an independently-approved
        # init container, e.g. 'research-engine/wait-for-research-migrations=<tag>').
        if ($entry -notmatch '^([a-z0-9-]+(?:/[a-z0-9-]+)?)=([A-Za-z0-9_][A-Za-z0-9_.-]{0,127})$') {
            throw "Invalid -ReconcileComponent entry '$entry' -- expected '<component>=<tag>' or '<component>/<initContainerName>=<tag>'."
        }
        $key = $Matches[1]; $tag = $Matches[2]
        if ($componentTags.ContainsKey($key)) { throw "Component/container '$key' specified more than once in -ReconcileComponent." }
        $componentTags[$key] = $tag
    }
    $result = Approve-RollbackBaselineReconciliation -ProjectRoot $ProjectRoot -KubeContext $KubeContext -KubeNamespace $KubeNamespace -ComponentTags $componentTags `
        -AuthorizeRollbackBaselineReconciliation:$AuthorizeRollbackBaselineReconciliation -ReconciliationReason $RollbackBaselineReconciliationReason
    if ($result.PSObject.Properties.Name -contains 'ManifestPath') {
        Write-Host "Approved rollback baseline advanced to version $($result.BaselineVersion) via reconciliation: $($result.ManifestPath)"
    }
}
