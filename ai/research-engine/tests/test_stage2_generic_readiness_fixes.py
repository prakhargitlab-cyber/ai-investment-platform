"""AI INVESTMENT PLATFORM -- Stage-2 readiness failure-matrix follow-up.

Scope of THIS task (per the task's own explicit guidance to fix generic root
causes, not hand-tune one example stock, and to STOP and report rather than
force a fix where the current model genuinely cannot represent it safely):

FIXED (this file's tests 1-4):
  - EXTERNAL_CAPABILITY_UNSUPPORTED (the MCP gateway genuinely has no
    provider tool registered for a requirement/region at all -- verified by
    direct trace: SHAREHOLDING is hard-defaulted UNSUPPORTED in the Yahoo
    MCP capability registry, no tool, no yfinance call, nothing to retry)
    is now classified EVIDENCE_UNAVAILABLE/permanent in
    app.failure_taxonomy, instead of defaulting to TECHNICAL_RETRYABLE.
    This was the confirmed root cause of repair_attempted=12,
    repair_recovered=0 for any candidate whose only remaining blocker was a
    genuinely-unsupported Yahoo capability: repair kept re-attempting an
    acquisition path that cannot ever succeed, burning the bounded repair
    budget for zero possible recovery, while the actually-useful next step
    (the NSE official-document fallback, or reporting the real unresolved
    input) was available the whole time.

VERIFIED ALREADY CORRECT (no change needed -- confirmed by direct trace,
documented here so this investigation is not repeated):
  - Objective 7 (stale failure reverification) is already GENERIC: at least
    three independent evidence-only reassessment layers exist
    (McpFirstResearchCapabilityExecutor.execute_primary's own tail block;
    ResearchReadinessRuntime.ensure()'s wakeup-loop reassessment;
    app.deep_investigation.investigate()'s _finalize/_requirement_sufficient,
    which is the one that actually governs the final deep_readiness_failed/
    technical_failure classification and supersedes execute_primary's own
    filtered result). None of these are requirement_id-specific.
  - Objective 9 (failure semantics) already keeps RULE_AREA_UNSCORABLE in
    app.failure_taxonomy.TECHNICAL_REASONS with an explicit comment that
    readiness/scorer disagreement must never become a terminal failure on
    its own -- already the correct, non-blocking behavior.
  - The SHAREHOLDING three-phase acquisition sequencing itself (NSE
    structured -> reassess -> Yahoo only if a gap remains -> reassess -> NSE
    document only if a gap still remains -> reassess) already runs the
    document fallback regardless of the Yahoo leg's outcome -- confirmed by
    direct trace of app.yahoo_mcp_acquisition.py's India SHAREHOLDING
    pre-pass. The symptom described in the task (SHAREHOLDING reported as a
    terminal EXTERNAL_CAPABILITY_UNSUPPORTED failure) was therefore not a
    sequencing bug; it was this file's fix (the classification bug above)
    compounded by the Objective 4 blocker documented below for cases where
    the document fallback also could not fully satisfy the rule engine's
    OWN scoring needs.

DELIBERATELY NOT FIXED THIS PASS -- precise blocker reported, per the
task's own "STOP and report the precise model blocker" instruction, rather
than ship a readiness change that could just as easily flip a genuinely
scorable candidate to incorrectly-not-ready:
  - Objective 4 (POLICYBZR-style READY vs RULE_AREA_UNSCORABLE disagreement
    on SHAREHOLDING). Root cause, confirmed by direct trace:
    app.research_applicability.shareholding_input_coverage() marks the
    mandatory input PROMOTER_INSTITUTIONAL_PUBLIC_CATEGORIES covered from
    ANY ONE of {PROMOTER, FII_FPI, DII, PUBLIC_RETAIL} being present in a
    SINGLE snapshot. app.stock_rule_engine.StockRuleEngine._shareholding()
    can score a candidate two different ways: (a) from a single snapshot,
    but ONLY via the PROMOTER category (PROMOTER_HOLDING metric, weight 25)
    -- FII_FPI/DII/PUBLIC_RETAIL alone produce no single-snapshot metric at
    all; or (b) from a PROMOTER/FII_FPI/DII period-over-period TREND metric,
    which requires TWO persisted snapshots sharing that category, not one.
    Disagreement condition: a candidate's latest snapshot has only
    FII_FPI/DII/PUBLIC_RETAIL (no PROMOTER) AND fewer than two persisted
    snapshots share a scorable category for a trend metric -- readiness says
    READY_FRESH, the rule engine's _finish() returns AreaScoreStatus.
    UNSCORABLE (missing input "STRUCTURED_OWNERSHIP_VALUES", a THIRD,
    differently-named identifier from readiness's own
    PROMOTER_INSTITUTIONAL_PUBLIC_CATEGORIES -- the two layers do not even
    share a missing-input vocabulary today).
    The blocker: shareholding_input_coverage()'s signature takes a SINGLE
    snapshot, so it structurally cannot see whether a second persisted
    period exists to justify the rule engine's trend-based scoring path.
    Narrowing coverage to require PROMOTER specifically (the naive fix)
    would correctly resolve the single-snapshot case but would WRONGLY
    report NOT_READY for a candidate that is genuinely scorable today via
    the two-snapshot FII_FPI/DII trend path -- i.e. it would trade one
    false READY for a new false NOT_READY, not actually make the two layers
    agree. Correctly fixing this needs shareholding_input_coverage() (or a
    sibling function sharing its contract, per its own docstring "Use the
    same mandatory ownership semantics for readiness and recovery") to
    become period-group-aware (operate on the same shareholding_period_
    groups() the field merge already uses, not one isolated snapshot), and
    for the rule engine's own missing-input identifier to be reconciled
    with readiness's vocabulary. That is a real, multi-file behavioral
    change to a mandatory-input contract, not a one-line fix, and shipping
    it without the dedicated test coverage this deserves risks exactly the
    "do not weaken the investment rule merely to make it pass" / "do not
    invent a pledge mapping" outcomes this task explicitly warns against.
  - Objective 5 (field/metric-level gap targeting for financial
    requirements outside the narrow NSE-financial-gap-fill path) and
    Objective 6 (historical price series) were investigated (see the
    failure-matrix report delivered alongside this patch) and found to
    already have reasonable, evidence-first contracts
    (FinancialGapState.supported_inputs()/accepts();
    has_year_historical_coverage() gap-range computation) -- the remaining
    gap (whole-requirement-id Yahoo calls outside the NSE-gap-fill path;
    EXTERNAL_RESULT_INCOMPLETE's blanket, ambiguous retry classification)
    is real but was judged too broad and too risky to reclassify blindly
    within this pass without separate, deliberate test coverage -- doing so
    could just as easily suppress a genuinely transient retry as it would
    stop a wasted one. Not changed; reported as a remaining blocker.
"""
from __future__ import annotations

import pytest

from app.failure_taxonomy import (
    EVIDENCE_UNAVAILABLE,
    PERMANENT_REASONS,
    TECHNICAL_RETRYABLE,
    classify_reason,
    classify_requirement_failures,
)


# 1. EXTERNAL_CAPABILITY_UNSUPPORTED alone classifies as genuinely
#    unavailable (permanent), not technical/retryable -- this is the fix.
def test_external_capability_unsupported_classifies_as_evidence_unavailable() -> None:
    assert "EXTERNAL_CAPABILITY_UNSUPPORTED" in PERMANENT_REASONS
    assert classify_reason("EXTERNAL_CAPABILITY_UNSUPPORTED") == EVIDENCE_UNAVAILABLE


# 2. A composite reason string combining EXTERNAL_CAPABILITY_UNSUPPORTED
#    with any still-technical component remains TECHNICAL_RETRYABLE overall
#    -- the conservative "any non-permanent component wins" rule already in
#    classify_reason must not be loosened by this addition.
@pytest.mark.parametrize("composite", [
    "EXTERNAL_CAPABILITY_UNSUPPORTED|NETWORK_TIMEOUT",
    "NETWORK_TIMEOUT|EXTERNAL_CAPABILITY_UNSUPPORTED",
])
def test_capability_unsupported_combined_with_technical_component_stays_retryable(composite) -> None:
    assert classify_reason(composite) == TECHNICAL_RETRYABLE


# 3. classify_requirement_failures: a candidate whose ONLY blocking failure
#    is EXTERNAL_CAPABILITY_UNSUPPORTED is now genuinely unavailable
#    (terminal, not repaired) rather than technical/retryable -- this is the
#    exact shape of the repair_recovered=0 symptom this fix targets: the
#    orchestrator's repair-eligibility check (_retryable) must now see this
#    candidate as non-retryable and stop spending repair attempts on it.
def test_capability_unsupported_as_sole_blocking_failure_is_genuine_not_retryable() -> None:
    result = classify_requirement_failures(
        {"SHAREHOLDING": "EXTERNAL_CAPABILITY_UNSUPPORTED"},
        blocking_requirements=("SHAREHOLDING",),
    )
    assert result == EVIDENCE_UNAVAILABLE


# 4. Regression guard: every reason this module's own audit (Slice 9) and
#    this engagement's Slice 2/Slice 2-follow-up already rely on being
#    TECHNICAL_RETRYABLE must remain exactly that -- this fix must not have
#    widened PERMANENT_REASONS to catch anything beyond the one new code.
@pytest.mark.parametrize("reason", [
    "NETWORK_TIMEOUT",
    "EXTERNAL_PROVIDER_UNAVAILABLE",
    "AUTHORITATIVE_UNAVAILABLE",
    "NSE_SHAREHOLDING_OFFICIAL_UNAVAILABLE",
    "RULE_AREA_UNSCORABLE",
    "DOWNSTREAM_TIMEOUT",
    "VERIFIED_YAHOO_MAPPING_REQUIRED",
])
def test_previously_technical_reasons_remain_technical_retryable(reason) -> None:
    assert classify_reason(reason) == TECHNICAL_RETRYABLE
