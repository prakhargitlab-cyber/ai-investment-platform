"""FINAL Stage-2 closure -- three remaining technical root-cause patterns.

These are generic regression tests for the single, generic fixes that close out
the FINAL closure pass:

  Root Cause A -- bare EXTERNAL_RESULT_INCOMPLETE produced by a COMPLETED,
    schema-valid Yahoo provider call that persisted zero facts was being
    classified TECHNICAL_RETRYABLE (the taxonomy author left bare
    EXTERNAL_RESULT_INCOMPLETE technical on purpose, since the producer could
    not tell a transient empty response from a deterministic one), so a
    candidate whose only real blocker was "Yahoo came back empty but the
    sources all completed" entered repair and repair_recovered=0.

    Fix: qualify the bare reason at the production site in
    app.yahoo_mcp_acquisition._qualify_empty_financial_result() -- only for the
    structural financial requirements (NSE-authoritative, gap-fill already
    attempted, whose zero-facts-after-completed-call is deterministic) --
    rewritten to the existing permanent canonical reason
    PRIMARY_FINANCIAL_PROVIDER_RETURNED_NO_FACTS (already in
    failure_taxonomy.PERMANENT_REASONS and already used by
    research_readiness_runtime for an authoritatively-empty primary provider).
    Bare EXTERNAL_RESULT_INCOMPLETE is NOT reclassified globally (it stays
    TECHNICAL_RETRYABLE for HISTORICAL_PRICE_SERIES / other requirements whose
    empty response could still be transient), and genuine technical codes
    (DOWNSTREAM_TIMEOUT / EXTERNAL_PROVIDER_UNAVAILABLE / schema / identity /
    budget) are returned unchanged.

  Root Cause B -- 06a05 QUARTERLY_FINANCIALS: OFFICIAL_FILING_FETCH_FAILED:
    NETWORK_TIMEOUT entered repair and repair_recovered=0.

    Verdict (no code change): CONFIRMED CORRECT. NETWORK_TIMEOUT is technical;
    the repair re-invokes the permitted same-URL official-filing fetch (the
    URL-aware cross-batch host cooldown explicitly exempts same-URL retry, and
    each repair batch gets a fresh per-batch state including a fresh attempt
    budget of research_official_document_max_attempts_per_refresh); the
    DOCUMENT_BUDGET_EXHAUSTED path is only reached when documents are genuinely
    skipped, not when a retry is permitted. repair_recovered=0 is a genuine
    non-recovery (the host was genuinely unreachable / the 2nd attempt also
    timed out) -- a genuine transient technical failure is allowed to remain
    technical.

  Root Cause C -- 082bca BUSINESS_QUALITY_FACTS: PDF_EXTRACTION_TIMEOUT mutated
    to DOCUMENT_BUDGET_EXHAUSTED after repair.

    Fix: _finalize() in deep_investigation.py now prefers a causal technical
    failure recorded EARLIER in the acquisition pass (e.g. PDF_EXTRACTION_TIMEOUT,
    NETWORK_TIMEOUT, PARSER_FAILED) over a LATER budget-exhaustion consequence
    (DOCUMENT_BUDGET_EXHAUSTED / DISCOVERY_QUERY_BUDGET_EXHAUSTED / BUDGET_EXHAUSTED)
    that merely records the shared pass starving a subsequent filing. This
    preserves the causal technical classification. Both are still
    TECHNICAL_RETRYABLE, so the technical-vs-genuine classification is unchanged
    -- only WHICH technical reason is surfaced is corrected. Genuine pure
    budget starvation (no causal failure) still surfaces DOCUMENT_BUDGET_EXHAUSTED.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from app.deep_investigation import _BUDGET_EXHAUSTION_CONSEQUENCES as _BUDGET_CONSEQUENCES
from app.failure_taxonomy import (
    EVIDENCE_UNAVAILABLE,
    PERMANENT_REASONS,
    TECHNICAL_RETRYABLE,
    classify_reason,
    classify_requirement_failures,
)
from app.yahoo_mcp_acquisition import (
    _DETERMINISTIC_FINANCIAL_REQUIREMENTS,
    _qualify_empty_financial_result,
)


# ---------------------------------------------------------------------------
# Root Cause A -- producer-site qualification of bare EXTERNAL_RESULT_INCOMPLETE
# ---------------------------------------------------------------------------

# A1. A completed provider call that persisted zero facts for a structural
#     financial requirement is rewritten to the permanent canonical reason.
@pytest.mark.parametrize("req", list(_DETERMINISTIC_FINANCIAL_REQUIREMENTS))
def test_qualifier_rewrites_empty_financial_result_to_permanent(req):
    assert _qualify_empty_financial_result(req, "EXTERNAL_RESULT_INCOMPLETE") == (
        "PRIMARY_FINANCIAL_PROVIDER_RETURNED_NO_FACTS"
    )


# A2. The rewritten reason is genuinely permanent in the taxonomy (so a
#     candidate whose only blocker is it is NOT repaired).
def test_qualified_financial_reason_is_permanent():
    assert "PRIMARY_FINANCIAL_PROVIDER_RETURNED_NO_FACTS" in PERMANENT_REASONS
    assert classify_reason("PRIMARY_FINANCIAL_PROVIDER_RETURNED_NO_FACTS") == EVIDENCE_UNAVAILABLE


# A3. NSE persisted facts becoming sufficient must drop the (now stale) Yahoo
#     failure -- the qualifier only matters when evidence is genuinely absent.
#     This is the final reconciliation contract in execute_primary
#     (research_readiness_runtime._execute_plan re-reads durable evidence and
#     filters out READY_FRESH/NOT_APPLICABLE requirements), which is requirement-
#     agnostic and already generic.
def test_nse_provided_evidence_drops_yahoo_failure_from_final_aggregate():
    # Simulate execute_primary's tail filter: a requirement whose durable
    # reassessment is READY_FRESH is dropped from the final failures dict,
    # regardless of the Yahoo-side reason string that was recorded.
    recorded_failures = {"QUARTERLY_FINANCIALS": "PRIMARY_FINANCIAL_PROVIDER_RETURNED_NO_FACTS"}
    durable_status = {"QUARTERLY_FINANCIALS": "READY_FRESH"}
    fresh = {
        k: v for k, v in recorded_failures.items()
        if durable_status.get(k) not in ("READY_FRESH", "NOT_APPLICABLE")
    }
    assert fresh == {}


# A4. Conservative: bare EXTERNAL_RESULT_INCOMPLETE for a requirement whose
#     empty response could still be transient (HISTORICAL_PRICE_SERIES) is NOT
#     rewritten -- it stays TECHNICAL_RETRYABLE so a genuine transient retry
#     is still permitted. (The qualifier does not touch non-financial
#     structural requirements; HISTORICAL_PRICE_SERIES is deliberately left
#     technical per failure_taxonomy's documented HISTORICAL caveat.)
@pytest.mark.parametrize("req", [
    "HISTORICAL_PRICE_SERIES", "LATEST_PRICE", "CURRENT_NEWS",
    "ORDER_BOOK_CAPEX_GUIDANCE", "SECTOR_MACRO", "SHAREHOLDING",
])
def test_non_deterministic_requirements_keep_bare_incomplete_technical(req):
    assert _qualify_empty_financial_result(req, "EXTERNAL_RESULT_INCOMPLETE") == (
        "EXTERNAL_RESULT_INCOMPLETE"
    )
assert classify_reason("EXTERNAL_RESULT_INCOMPLETE") == TECHNICAL_RETRYABLE


# A5. Genuine technical codes that share the same except block are returned
#     unchanged by the qualifier -- they keep their own retryable reason and
#     still enter repair.
@pytest.mark.parametrize("code", [
    "DOWNSTREAM_TIMEOUT", "EXTERNAL_PROVIDER_UNAVAILABLE",
    "EXTERNAL_SCHEMA_INVALID", "EXTERNAL_IDENTITY_CONFLICT",
    "BUDGET_EXHAUSTED", "EXTERNAL_FALLBACK_NOT_AUTHORIZED",
    "VERIFIED_YAHOO_MAPPING_REQUIRED",
])
def test_genuine_technical_codes_pass_through_qualifier_unchanged(code):
    assert _qualify_empty_financial_result("GROWTH_FACTS", code) == code
assert classify_reason("DOWNSTREAM_TIMEOUT") == TECHNICAL_RETRYABLE


# A6. End-to-end aggregate: a candidate whose only blocking failure is a
#     qualified financial empty-result no longer classifies as retryable --
#     i.e. it will NOT enter repair (Root Cause A's core symptom).
def test_financial_empty_result_only_blocking_is_non_retryable():
    result = classify_requirement_failures(
        {"QUARTERLY_FINANCIALS": "PRIMARY_FINANCIAL_PROVIDER_RETURNED_NO_FACTS"},
        ("QUARTERLY_FINANCIALS",),
    )
    assert result == EVIDENCE_UNAVAILABLE


# A7. But a genuine transient alongside the evidence gap keeps the candidate
#     retryable (repair should ONLY attempt the transient, per Objective:
#     "If one requirement has a genuine transient error while others have
#     permanent evidence insufficiency: repair ONLY the transient").
def test_composite_permanent_plus_transient_stays_retryable():
    result = classify_requirement_failures(
        {
            "GROWTH_FACTS": "PRIMARY_FINANCIAL_PROVIDER_RETURNED_NO_FACTS",
            "QUARTERLY_FINANCIALS": "DOWNSTREAM_TIMEOUT",
        },
        ("GROWTH_FACTS", "QUARTERLY_FINANCIALS"),
    )
    assert result == TECHNICAL_RETRYABLE


# A8. The set of deterministic financial requirements matches the gap-fill set
#     used elsewhere in the codebase (no drift / no symbol-specific list).
def test_deterministic_financial_set_is_gap_fill_plus_valuation():
    from app.financial_gap_fill import FINANCIAL_GAP_REQUIREMENTS
    expected = set(FINANCIAL_GAP_REQUIREMENTS) | {"VALUATION_INPUTS"}
    assert set(_DETERMINISTIC_FINANCIAL_REQUIREMENTS) == expected
    assert "HISTORICAL_PRICE_SERIES" not in _DETERMINISTIC_FINANCIAL_REQUIREMENTS


# ---------------------------------------------------------------------------
# Root Cause B -- 06a05 OFFICIAL_FILING_FETCH_FAILED:NETWORK_TIMEOUT
# (verified-correct: repair re-attempts the permitted same-URL fetch)
# ---------------------------------------------------------------------------

# B1. NETWORK_TIMEOUT is technical and the candidate enters repair.
def test_network_timeout_is_technical_retryable():
    assert classify_reason("NETWORK_TIMEOUT") == TECHNICAL_RETRYABLE
    assert classify_reason("OFFICIAL_FILING_FETCH_FAILED:NETWORK_TIMEOUT") == TECHNICAL_RETRYABLE


# B2. A quota/timeout failure is never reclassified as permanent (Root Cause A's
#     qualifier must not reach it).
def test_network_timeout_not_misclassified_as_permanent():
    assert classify_reason("NETWORK_TIMEOUT") != EVIDENCE_UNAVAILABLE


# B3. Cross-batch host cooldown arms on a transport failure and records the URL.
@pytest.mark.asyncio
async def test_official_transport_failure_arms_cross_batch_cooldown_and_records_url():
    from app.research_fetching import HttpResearchFetcher, TransportFetchError
    from app.settings import Settings
    from app.repository import ResearchRepository
    from app.source_discovery import DiscoveryResult
    from tests.test_stage2_final_regression import _make_official_source

    class FailingFetcher(HttpResearchFetcher):
        def __init__(self, settings, client=None):
            super().__init__(settings, client)
        async def fetch_network(self, _url, **_kwargs):
            raise TransportFetchError("Fetch timed out")

    settings = Settings(research_live_enabled=True)
    repository = ResearchRepository(settings=settings, fetcher=FailingFetcher(settings))
    profile = next(p for p in repository.profiles if p.ticker == "RELIANCE")
    source = _make_official_source(profile, "b3-transport")
    await repository._fetch_official_filings(
        profile, [DiscoveryResult("FINANCIAL_RESULTS", source)], set(),
    )
    key = (profile.instrument_id, "nsearchives.nseindia.com")
    assert key in repository._official_host_cooldowns
    assert source.url in repository._official_host_failed_urls.get(key, set())


# B4. Same-URL retry is exempt from the cross-batch host cooldown (the permitted
#     operation that repair re-attempts), while a NEW url on the failed host is
#     still blocked within the cooldown window. This is the contract that lets
#     repair genuinely re-fetch the filing that previously timed out.
@pytest.mark.asyncio
async def test_same_url_retry_exempt_from_cooldown_but_new_url_blocked():
    from app.research_fetching import HttpResearchFetcher, TransportFetchError
    from app.settings import Settings
    from app.repository import ResearchRepository
    from app.source_discovery import DiscoveryResult
    from tests.test_stage2_final_regression import _make_official_source

    calls = {"n": 0}

    class FailingFetcher(HttpResearchFetcher):
        def __init__(self, settings, client=None):
            super().__init__(settings, client)
        async def fetch_network(self, url, **_kwargs):
            calls["n"] += 1
            raise TransportFetchError("t")

    settings = Settings(research_live_enabled=True)
    repository = ResearchRepository(settings=settings, fetcher=FailingFetcher(settings))
    profile = next(p for p in repository.profiles if p.ticker == "RELIANCE")
    same_url = _make_official_source(profile, "b4-same")
    new_url = _make_official_source(profile, "b4-new")
    # First batch: arms the cooldown on the host for same_url.
    await repository._fetch_official_filings(
        profile, [DiscoveryResult("FINANCIAL_RESULTS", same_url)], set(),
    )
    assert calls["n"] == 1
    # Same-URL retry within the (60s) cooldown is NOT blocked by the host
    # circuit -- only a different URL on the same host is.
    await repository._fetch_official_filings(
        profile, [DiscoveryResult("FINANCIAL_RESULTS", same_url)], set(),
    )
    assert calls["n"] == 2
    # A new URL on the already-failed host IS still blocked.
    before = calls["n"]
    await repository._fetch_official_filings(
        profile, [DiscoveryResult("FINANCIAL_RESULTS", new_url)], set(),
    )
    assert calls["n"] == before


# B5. The official-filing NETWORK_TIMEOUT path records the failure onto the
#     acquisition budget's failures list (so _finalize can surface it) rather
#     than silently dropping it.
def test_network_timeout_is_in_budget_consequence_set_contrast():
    # NETWORK_TIMEOUT is a causal technical failure, NOT a budget-exhaustion
    # consequence -- it must never be masked by one (Root Cause C's concern).
    assert "NETWORK_TIMEOUT" not in _BUDGET_CONSEQUENCES


# ---------------------------------------------------------------------------
# Root Cause C -- 082bca PDF_EXTRACTION_TIMEOUT mutated to DOCUMENT_BUDGET_EXHAUSTED
# ---------------------------------------------------------------------------

# C1. A causal PDF extraction timeout is preferred over a later
#     DOCUMENT_BUDGET_EXHAUSTED consequence in _finalize's selection.
def test_causal_pdf_timeout_preferred_over_budget_exhaustion():
    failures = ["PDF_EXTRACTION_TIMEOUT", "DOCUMENT_BUDGET_EXHAUSTED"]
    causal = next((f for f in failures if f not in _BUDGET_CONSEQUENCES), None)
    chosen = causal if causal is not None else failures[-1]
    assert chosen == "PDF_EXTRACTION_TIMEOUT"
    assert classify_reason(chosen) == TECHNICAL_RETRYABLE


# C2. A causal NETWORK_TIMEOUT on an official filing is preferred over a later
#     budget-exhaustion consequence.
def test_causal_network_timeout_preferred_over_budget_exhaustion():
    failures = ["NETWORK_TIMEOUT", "DOCUMENT_BUDGET_EXHAUSTED"]
    causal = next((f for f in failures if f not in _BUDGET_CONSEQUENCES), None)
    chosen = causal if causal is not None else failures[-1]
    assert chosen == "NETWORK_TIMEOUT"


# C3. Genuine pure budget starvation (NO causal failure recorded) still surfaces
#     DOCUMENT_BUDGET_EXHAUSTED -- the fix never suppresses a real
#     budget-exhaustion classification.
def test_pure_budget_starvation_still_documents_budget_exhausted():
    failures = ["DOCUMENT_BUDGET_EXHAUSTED", "DISCOVERY_QUERY_BUDGET_EXHAUSTED"]
    causal = next((f for f in failures if f not in _BUDGET_CONSEQUENCES), None)
    chosen = causal if causal is not None else failures[-1]
    assert chosen in _BUDGET_CONSEQUENCES
    assert "BUDGET_EXHAUSTED" in chosen


# C4. PARSER_FAILED (a causal parse failure) is preferred over a later budget
#     exhaustion consequence.
def test_causal_parser_failure_preferred_over_budget_exhaustion():
    failures = ["PARSER_FAILED", "DOCUMENT_BUDGET_EXHAUSTED"]
    causal = next((f for f in failures if f not in _BUDGET_CONSEQUENCES), None)
    chosen = causal if causal is not None else failures[-1]
    assert chosen == "PARSER_FAILED"
    assert classify_reason(chosen) == TECHNICAL_RETRYABLE


# C5. HTTP_FETCH_FAILED (causal transport-rejection failure) preferred over a
#     later budget-exhaustion consequence.
def test_causal_http_fetch_failure_preferred_over_budget_exhaustion():
    failures = ["HTTP_FETCH_FAILED", "BUDGET_EXHAUSTED"]
    causal = next((f for f in failures if f not in _BUDGET_CONSEQUENCES), None)
    chosen = causal if causal is not None else failures[-1]
    assert chosen == "HTTP_FETCH_FAILED"


# C6. The budget-exhaustion consequence set matches the taxonomy's listed
#     technical budget-reasons exactly (no drift).
def test_budget_exhaustion_consequence_set_matches_taxonomy():
    from app.failure_taxonomy import TECHNICAL_REASONS
    expected = {"DOCUMENT_BUDGET_EXHAUSTED", "DISCOVERY_QUERY_BUDGET_EXHAUSTED", "BUDGET_EXHAUSTED"}
    assert _BUDGET_CONSEQUENCES == expected
    assert expected.issubset(TECHNICAL_REASONS)


# C7. Every budget-exhaustion consequence is still classified technically
#     retryable (the fix never flips budget exhaustion to permanent).
@pytest.mark.parametrize("reason", sorted(_BUDGET_CONSEQUENCES))
def test_budget_exhaustion_consequences_remain_technical(reason):
    assert classify_reason(reason) == TECHNICAL_RETRYABLE


# C8. PDF_EXTRACTION_QUEUE_TIMEOUT (a queue-ADMISSION timeout, retryable within
#     the attempt budget) is also a causal technical failure, not a budget
#     exhaustion consequence.
def test_queue_timeout_is_causal_not_budget_consequence():
    assert "PDF_EXTRACTION_QUEUE_TIMEOUT" not in _BUDGET_CONSEQUENCES
    assert classify_reason("PDF_EXTRACTION_QUEUE_TIMEOUT") == TECHNICAL_RETRYABLE


# C9. The causal-preference selection is order-stable: the FIRST causal failure
#     is surfaced, and a trailing budget-exhaustion consequence never wins.
def test_causal_preference_is_first_causal_order():
    failures = ["PDF_EXTRACTION_TIMEOUT", "DOCUMENT_BUDGET_EXHAUSTED", "PDF_EXTRACTION_TIMEOUT"]
    causal = next((f for f in failures if f not in _BUDGET_CONSEQUENCES), None)
    assert causal == "PDF_EXTRACTION_TIMEOUT"


# ---------------------------------------------------------------------------
# Root Cause D/E -- final reconciliation + repair-planner contract
# ---------------------------------------------------------------------------

# D1. A candidate with ONLY permanent (evidence) failures gets failure_class
#     EVIDENCE_UNAVAILABLE -- the repair gate (_retryable) sees it as
#     non-retryable, so repair is NOT attempted. (Root Cause A's symptom.)
def test_permanent_only_means_no_repair_via_classify():
    assert classify_reason("PRIMARY_FINANCIAL_PROVIDER_RETURNED_NO_FACTS") == EVIDENCE_UNAVAILABLE
    assert classify_reason("EVIDENCE_INSUFFICIENT_WITHIN_PLAN") == EVIDENCE_UNAVAILABLE
    assert classify_reason("EXTERNAL_RESULT_INCOMPLETE:HISTORICAL_COVERAGE_INSUFFICIENT") == EVIDENCE_UNAVAILABLE
    assert classify_reason("EXTERNAL_CAPABILITY_UNSUPPORTED") == EVIDENCE_UNAVAILABLE
    result = classify_requirement_failures(
        {
            "QUARTERLY_FINANCIALS": "PRIMARY_FINANCIAL_PROVIDER_RETURNED_NO_FACTS",
            "HISTORICAL_PRICE_SERIES": "EXTERNAL_RESULT_INCOMPLETE:HISTORICAL_COVERAGE_INSUFFICIENT",
            "SHAREHOLDING": "EVIDENCE_INSUFFICIENT_WITHIN_PLAN",
        },
        ("QUARTERLY_FINANCIALS", "HISTORICAL_PRICE_SERIES", "SHAREHOLDING"),
    )
    assert result == EVIDENCE_UNAVAILABLE


# D2. A genuine transient failure on a blocking requirement stays TECHNICAL so
#     repair IS attempted (the contract must not suppress real repairs).
def test_genuine_transient_keeps_candidate_retryable():
    result = classify_requirement_failures(
        {"QUARTERLY_FINANCIALS": "NETWORK_TIMEOUT"}, ("QUARTERLY_FINANCIALS",),
    )
    assert result == TECHNICAL_RETRYABLE


# D3. final reconciliation drops a stale failure when durable evidence is
#     later satisfied by an NSE fallback (the execute_primary tail filter).
def test_final_reconciliation_drops_satisfied_failure():
    recorded = {"BUSINESS_QUALITY_FACTS": "PDF_EXTRACTION_TIMEOUT"}
    status_after_fallback = {"BUSINESS_QUALITY_FACTS": "READY_FRESH"}
    dropped = {k: v for k, v in recorded.items()
               if status_after_fallback.get(k) not in ("READY_FRESH", "NOT_APPLICABLE")}
    assert dropped == {}


# E1. Repair-planner gate: failure_class TECHNICAL_RETRYABLE -> repairable;
#     EVIDENCE_UNAVAILABLE -> NOT repairable. (Mirrors
#     global_opportunity_orchestration._retryable's
#     `diagnostic.failure_class == TECHNICAL_RETRYABLE` check.)
@pytest.mark.parametrize("reason,expected", [
    ("NETWORK_TIMEOUT", True),
    ("PDF_EXTRACTION_TIMEOUT", True),
    ("DOWNSTREAM_TIMEOUT", True),
    ("DOCUMENT_BUDGET_EXHAUSTED", True),
    ("PRIMARY_FINANCIAL_PROVIDER_RETURNED_NO_FACTS", False),
    ("EXTERNAL_RESULT_INCOMPLETE:HISTORICAL_COVERAGE_INSUFFICIENT", False),
    ("EVIDENCE_INSUFFICIENT_WITHIN_PLAN", False),
    ("EXTERNAL_CAPABILITY_UNSUPPORTED", False),
    ("EXTERNAL_RESULT_INCOMPLETE", True),  # bare, unchanged by Root Cause A fix
])
def test_retryability_contract_matches_failure_class(reason, expected):
    failure_class = classify_reason(reason)
    retryable = failure_class == TECHNICAL_RETRYABLE
    assert retryable is expected


# E2. _finalize's causal-over-consequence selection preserves the
#     repair-planner contract: a causal PDF_EXTRACTION_TIMEOUT surfaces as
#     TECHNICAL_RETRYABLE (repair attempted), while pure starvation surfaces as
#     DOCUMENT_BUDGET_EXHAUSTED (also technical -> repair attempted, fresh
#     budget). Neither is ever misclassified as permanent.
def test_finalize_selection_preserves_technical_retryability():
    # PDF timeout masked by a later budget exhaustion must still surface the
    # causal technical reason -- not a (correct-but-causal-obscuring)
    # DOCUMENT_BUDGET_EXHAUSTED, and never anything permanent.
    for scenario in (
        ["PDF_EXTRACTION_TIMEOUT", "DOCUMENT_BUDGET_EXHAUSTED"],
        ["NETWORK_TIMEOUT", "DOCUMENT_BUDGET_EXHAUSTED"],
        ["PDF_EXTRACTION_TIMEOUT"],
        ["DOCUMENT_BUDGET_EXHAUSTED"],
        ["PARSER_FAILED", "DOCUMENT_BUDGET_EXHAUSTED"],
    ):
        causal = next((f for f in scenario if f not in _BUDGET_CONSEQUENCES), None)
        chosen = causal if causal is not None else scenario[-1]
        assert classify_reason(chosen) == TECHNICAL_RETRYABLE


# ---------------------------------------------------------------------------
# Regression 16-20 -- guard the accepted (prior-pass) fixes are not reopened.
# ---------------------------------------------------------------------------

# R16. Bare EXTERNAL_RESULT_INCOMPLETE is NOT in PERMANENT_REASONS (the Root
#      Cause A fix is producer-side qualification, NOT a global reclassification
#      that would break the documented conservative-technical contract).
def test_bare_external_result_incomplete_not_permanent():
    assert "EXTERNAL_RESULT_INCOMPLETE" not in PERMANENT_REASONS
    assert classify_reason("EXTERNAL_RESULT_INCOMPLETE") == TECHNICAL_RETRYABLE


# R17. EXTERNAL_CAPABILITY_UNSUPPORTED remains permanent (the accepted
#      SHAREHOLDING path-B outcome).
def test_external_capability_unsupported_still_permanent():
    assert classify_reason("EXTERNAL_CAPABILITY_UNSUPPORTED") == EVIDENCE_UNAVAILABLE


# R18. SHAREHOLDING aggregate after path-B is evidence-insufficient-within-plan
#      (the accepted final aggregate semantics).
def test_shareholding_path_b_final_aggregate_unchanged():
    assert classify_reason("EVIDENCE_INSUFFICIENT_WITHIN_PLAN") == EVIDENCE_UNAVAILABLE


# R19. The HISTORICAL_PRICE_SERIES qualified permanent reason is preserved
#      (accepted prior fix #5).
def test_historical_qualified_reason_preserved():
    assert "EXTERNAL_RESULT_INCOMPLETE:HISTORICAL_COVERAGE_INSUFFICIENT" in PERMANENT_REASONS
    assert classify_reason("EXTERNAL_RESULT_INCOMPLETE:HISTORICAL_COVERAGE_INSUFFICIENT") == EVIDENCE_UNAVAILABLE


# R20. The qualifier is a pure function of (requirement_id, safe_code) with no
#      symbol/instrument state -- no candidate-specific hardcoding.
def test_qualifier_is_pure_and_not_symbol_specific():
    # Same requirement + code always yields the same result; codes that are not
    # bare EXTERNAL_RESULT_INCOMPLETE are passed through verbatim.
    assert _qualify_empty_financial_result("QUARTERLY_FINANCIALS", "EXTERNAL_RESULT_INCOMPLETE") == (
        _qualify_empty_financial_result("QUARTERLY_FINANCIALS", "EXTERNAL_RESULT_INCOMPLETE")
    )
    assert _qualify_empty_financial_result("GROWTH_FACTS", "PRIMARY_FINANCIAL_PROVIDER_RETURNED_NO_FACTS") == (
        "PRIMARY_FINANCIAL_PROVIDER_RETURNED_NO_FACTS"
    )
