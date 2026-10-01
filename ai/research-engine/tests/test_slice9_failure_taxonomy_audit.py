"""Guardian Review Slice 9 -- failure classification / terminal accounting audit.

This is a verification pass (like Slice 5): every classification named in the
Guardian Review's checklist was traced in current source and found ALREADY
correct. No production classification table was changed for this slice.

Traced and confirmed in app/failure_taxonomy.py's PERMANENT_REASONS /
TECHNICAL_REASONS / classify_reason:
  - VERIFIED_YAHOO_MAPPING_REQUIRED, VERIFIED_HISTORICAL_MAPPING_REQUIRED (not
    listed in PERMANENT_REASONS) -> TECHNICAL_RETRYABLE by the module's own
    documented default ("a failure we cannot positively identify as 'the
    evidence does not exist' must not be turned into a terminal outcome").
  - VERIFIED_HISTORICAL_IDENTITY_MISMATCH:<reason> (the Slice 1 INFY/HCL-INSYS
    identity guard's own raised reason, app.historical_market_data's
    HistoricalPriceIdentityConflict) -> TECHNICAL_RETRYABLE for the same
    reason. (This is distinct from the business-level PROFILE_IDENTITY_
    MISMATCH disposition in app.cycle_checkpoint.DISPOSITION_STATE, which is
    intentionally TERMINAL_OUTCOME -- a company-profile eligibility decision,
    never a member of the technical/genuine dichotomy at all; the instrument
    was never a candidate for research accounting in the first place. Both
    are correct; they are not the same "identity mismatch.")
  - PDF_EXTRACTION_TIMEOUT, PDF_EXTRACTION_QUEUE_TIMEOUT (this task's own
    Slice 4 addition), NETWORK_TIMEOUT, PARSER_FAILED, DOCUMENT_PERSIST_FAILED,
    SEARCH_PROVIDER_UNAVAILABLE -> explicitly listed in TECHNICAL_REASONS.
  - DOCUMENT_BUDGET_EXHAUSTED -> explicitly listed in TECHNICAL_REASONS, with
    the module's own comment: "A spent per-refresh budget says nothing about
    whether evidence exists; a repair attempt gets a fresh budget."
  - DISCOVERY_NO_FINANCIAL_RESULTS_CANDIDATES: traced in
    app.deep_investigation.investigate()'s _finalize -- its priority order
    checks `budget.failures` (any technical failure recorded during
    acquisition) FIRST, and only reaches the discovery-outcome branch
    (DISCOVERY_NO_FINANCIAL_RESULTS_CANDIDATES / DISCOVERY_NO_CANDIDATES) when
    no technical failure was recorded; that branch is reached only past a
    check that discovery was actually attempted (`budget.discovery_attempted`)
    -- so this reason is only ever assigned when authoritative discovery
    genuinely ran and completed (no exception), never when discovery itself
    failed. Tests 5-6 below exercise this priority directly through
    investigate()'s real _finalize (not a mocked ensure_result), which the
    pre-existing tests in test_di20h_discovery_investigation.py had not done.
  - CURRENT_NEWS: app.research_applicability never places CURRENT_NEWS in any
    blocking-requirement set, and every classify_requirement_failures call
    site in app.global_opportunity_orchestration.py passes only
    eligibility.blocking_requirements (never the full requirement set) --
    confirmed already correct across the whole engagement; test 7 pins this
    module-boundary contract at the classify_requirement_failures level
    directly (a technical failure recorded against CURRENT_NEWS alone must
    never surface when CURRENT_NEWS is excluded from blocking_requirements).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.deep_investigation import acquisition_budget, investigate
from app.failure_taxonomy import (
    EVIDENCE_UNAVAILABLE,
    TECHNICAL_RETRYABLE,
    classify_reason,
    classify_requirement_failures,
)
from app.research_readiness import ResearchRequirementStatus as Status
from app.research_readiness_runtime import TargetedEnsureResult
from test_stock_rule_engine import _readiness

# 1 -- every named technical reason from the Guardian Review checklist
# classifies as TECHNICAL_RETRYABLE, individually -----------------------------
@pytest.mark.parametrize("reason", [
    "VERIFIED_YAHOO_MAPPING_REQUIRED",
    "VERIFIED_HISTORICAL_MAPPING_REQUIRED",
    "VERIFIED_HISTORICAL_IDENTITY_MISMATCH:INFY_MAPPED_TO_HCL-INSYS.NS",
    "PDF_EXTRACTION_TIMEOUT",
    "PDF_EXTRACTION_QUEUE_TIMEOUT",
    "NETWORK_TIMEOUT",
    "PARSER_FAILED",
    "DOCUMENT_PERSIST_FAILED",
    "DOCUMENT_BUDGET_EXHAUSTED",
    "SEARCH_PROVIDER_UNAVAILABLE",
])
def test_named_technical_reasons_classify_as_technical_retryable(reason):
    assert classify_reason(reason) == TECHNICAL_RETRYABLE


# 2 -- DISCOVERY_NO_FINANCIAL_RESULTS_CANDIDATES classifies as genuinely
# unavailable in isolation (the bare classifier has no way to know whether
# discovery actually ran -- that guarantee lives in _finalize's priority
# order, proven by tests 5-6 below, not in this leaf function) --------------
def test_discovery_no_financial_results_candidates_classifies_as_genuine():
    assert classify_reason("DISCOVERY_NO_FINANCIAL_RESULTS_CANDIDATES") == EVIDENCE_UNAVAILABLE


# 3 -- a candidate with ONE technical requirement failure among several is
# TECHNICAL_RETRYABLE overall, never downgraded to genuine by the presence of
# other, genuinely-unavailable requirements ----------------------------------
def test_one_technical_failure_among_mixed_reasons_wins_overall():
    failures = {
        "QUARTERLY_FINANCIALS": "NETWORK_TIMEOUT",
        "SHAREHOLDING": "DISCOVERY_NO_CANDIDATES",
    }
    assert classify_requirement_failures(failures, {"QUARTERLY_FINANCIALS", "SHAREHOLDING"}) == TECHNICAL_RETRYABLE


# 4 -- a technical failure on a NON-blocking (optional) requirement must not
# make an otherwise-genuine blocking gap retryable ---------------------------
def test_technical_failure_outside_blocking_set_does_not_override_genuine():
    failures = {
        "QUARTERLY_FINANCIALS": "DISCOVERY_NO_FINANCIAL_RESULTS_CANDIDATES",
        "CURRENT_NEWS": "NETWORK_TIMEOUT",  # optional, never blocking
    }
    assert classify_requirement_failures(failures, {"QUARTERLY_FINANCIALS"}) == EVIDENCE_UNAVAILABLE


# 5 -- integration: a technical failure recorded on the acquisition budget
# (simulating a discovery-layer exception, e.g. the NSE endpoint itself
# failing) wins over the discovery-outcome classification even when that same
# budget's discovery_outcome sums to zero -- exercised through investigate()'s
# real _finalize, not a mocked ensure_result -------------------------------
@pytest.mark.asyncio
async def test_technical_budget_failure_preempts_discovery_no_candidates_classification():
    readiness = _readiness({"QUARTERLY_FINANCIALS": Status.MISSING})

    async def ensure(key, *, requirement_ids, **kwargs):
        budget = acquisition_budget(key)
        # Discovery ran (so the "not attempted" / ACQUISITION_NOT_DUE branch
        # is not what's being tested) and found nothing -- but ALSO hit a
        # technical failure along the way (e.g. the NSE endpoint itself
        # failed after already recording zero candidates, or the technical
        # failure happened while examining an already-empty response).
        budget.discovery_attempted = True
        budget.discovery_outcome["FINANCIAL_RESULTS"] = 0
        budget.failures.append("SOURCE_UNAVAILABLE:NSE:TimeoutError")
        return TargetedEnsureResult(readiness, tuple(requirement_ids), ())

    runtime = SimpleNamespace(read=AsyncMock(return_value=readiness), ensure=ensure,
                              repository=SimpleNamespace(record_acquisition_observation=AsyncMock()))
    _, _, matrix = await investigate(runtime, readiness.global_instrument_id, jurisdiction="INDIA")
    assert matrix["QUARTERLY_FINANCIALS"]["failure"] == "SOURCE_UNAVAILABLE:NSE:TimeoutError"
    assert matrix["QUARTERLY_FINANCIALS"]["failure"] != "DISCOVERY_NO_FINANCIAL_RESULTS_CANDIDATES"


# 6 -- contrast: the SAME zero-candidates discovery outcome, with NO
# technical budget failure recorded, DOES classify as
# DISCOVERY_NO_FINANCIAL_RESULTS_CANDIDATES -- proving the technical branch in
# test 5 truly took priority rather than the classification being
# unconditional -------------------------------------------------------------
@pytest.mark.asyncio
async def test_zero_candidates_with_no_technical_failure_classifies_as_discovery_no_candidates():
    readiness = _readiness({"QUARTERLY_FINANCIALS": Status.MISSING})

    async def ensure(key, *, requirement_ids, **kwargs):
        budget = acquisition_budget(key)
        budget.discovery_attempted = True
        budget.discovery_outcome["FINANCIAL_RESULTS"] = 0
        return TargetedEnsureResult(readiness, tuple(requirement_ids), ())

    runtime = SimpleNamespace(read=AsyncMock(return_value=readiness), ensure=ensure,
                              repository=SimpleNamespace(record_acquisition_observation=AsyncMock()))
    _, _, matrix = await investigate(runtime, readiness.global_instrument_id, jurisdiction="INDIA")
    assert matrix["QUARTERLY_FINANCIALS"]["failure"] == "DISCOVERY_NO_FINANCIAL_RESULTS_CANDIDATES"


# 7 -- CURRENT_NEWS stays optional/non-blocking at the classification
# boundary: a technical (or any) failure recorded against CURRENT_NEWS alone
# must never surface once CURRENT_NEWS is excluded from blocking_requirements
# (exactly how every real call site in global_opportunity_orchestration.py
# invokes this function -- see module docstring) ----------------------------
def test_current_news_failure_alone_is_invisible_once_excluded_from_blocking():
    failures = {"CURRENT_NEWS": "SEARCH_PROVIDER_UNAVAILABLE"}
    assert classify_requirement_failures(failures, {"QUARTERLY_FINANCIALS"}) is None
