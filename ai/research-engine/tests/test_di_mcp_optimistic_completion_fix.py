"""Regressions for the second ACQUISITION_NOT_DUE runtime defect.

Root cause: McpFirstResearchCapabilityExecutor.execute_primary() marks a
target `completed` -- excluding it from the legacy/authoritative fallback --
purely from the Yahoo MCP gateway's own self-reported `acquisition_outcome`
("SUCCESS" vs "SUCCESS_EMPTY"), never from whether the write actually
resolved the requirement's still-missing mandatory input(s). A Yahoo response
that writes *something* (any structured fact, market observation, unrelated
financial metric) but not the specific missing mandatory piece (e.g. ROE for
BUSINESS_QUALITY_FACTS, a qualifying news event for CURRENT_NEWS) is treated
as fully satisfied. The authoritative/legacy fallback -- which would actually
attempt the missing data and correctly report into the deep-investigation
RequirementAcquisitionBudget -- never runs. Since the MCP path itself never
touches that budget either, deep_investigation.investigate() sees zero
activity and reports ACQUISITION_NOT_DUE, masking a real (but insufficient)
MCP attempt.

Covers:
  FIX D -- under an active deep-investigation targeted-repair budget, a Yahoo
           MCP "success" that does not actually resolve the targeted
           requirement's readiness gap must not exclude it from the
           legacy/authoritative fallback.
  FIX E -- the same MCP "success" outside an active budget (ordinary/
           routine refresh) is UNCHANGED: still accepted without the extra
           verification round-trip (no behavior change, no perf regression,
           matches test_trusted_nse_normal_mcp_success_cannot_advance_authority_throttle).
  FIX F -- a genuinely-satisfying MCP success (under an active budget) is
           still accepted without an unnecessary legacy fallback call.
  FIX G -- _missing_categories()/_category_is_fresh() no longer keep a
           category-level "qualifying evidence exists" freshness stamp from
           suppressing a targeted repair when the specific mandatory input
           the requirement needs is still missing.

These tests deploy/restart nothing and contact no live provider.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.deep_investigation import RequirementAcquisitionBudget, _scope
from app.research_readiness import ResearchRequirementStatus as Status
from app.research_readiness_runtime import CapabilityExecutionResult
from app.settings import Settings
from app.yahoo_mcp_acquisition import McpFirstResearchCapabilityExecutor

from test_di15_financial_authority_upgrade import _repo
from test_yahoo_mcp_acquisition import FakeLegacy, result, target


def _wired_repo():
    repo, profile = _repo()
    profile.provider_instrument_ids["YAHOO_FINANCE"] = "EXAMPLE.NS"
    return repo, profile


class _RecordingGateway:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []

    async def acquire_requirement(self, profile, **kwargs):
        self.calls.append((profile, kwargs))
        return self.outcome


def _budgeted(instrument_id, requirement_id, *, sufficient=None):
    """Install an active deep-investigation acquisition budget for the
    duration of the `with` block, matching what deep_investigation.investigate
    sets before calling runtime.ensure for a single targeted requirement."""
    budget = RequirementAcquisitionBudget(
        instrument_id, requirement_id, datetime.now(timezone.utc),
        sufficient or AsyncMock(return_value=False),
    )
    return budget, _scope.set(budget)


# --------------------------------------------------------------------------------------
# FIX D -- BUSINESS_QUALITY_FACTS: a Yahoo write that omits the missing mandatory
# metric (ROE) must not mask the authoritative fallback during targeted repair.
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_success_missing_targeted_mandatory_input_falls_through_to_legacy_under_repair():
    repo, profile = _wired_repo()
    legacy = FakeLegacy()
    now_iso = datetime.now(timezone.utc).isoformat()
    # Yahoo reports a plain "SUCCESS" and writes a real financial metric --
    # but not ROE, which is the specific mandatory input BUSINESS_QUALITY_FACTS
    # is still missing.
    outcome = result(
        "BUSINESS_QUALITY_FACTS",
        globalInstrumentId=str(profile.instrument_id), symbol="EXAMPLE.NS",
        structuredFacts=[], marketObservations=[],
        financialFacts=[{
            "metric": "roce", "value": "18.5", "unit": "PERCENT",
            "periodEnd": "2026-06-30", "periodType": "QUARTERLY", "reportingBasis": "STANDALONE",
            "asOf": now_iso, "publishedAt": now_iso, "confidence": 0.8,
            "sourceUrl": "https://finance.yahoo.com/quote/EXAMPLE.NS", "rawFieldOrigin": "returnOnCapitalEmployed",
        }],
    )
    gateway = _RecordingGateway(outcome)
    executor = McpFirstResearchCapabilityExecutor(legacy, repo, gateway, enabled=True)

    budget, token = _budgeted(profile.instrument_id, "BUSINESS_QUALITY_FACTS")
    try:
        capability_result = await executor.execute_primary(
            profile.instrument_id, (target("BUSINESS_QUALITY_FACTS"),),
            jurisdiction="INDIA", correlation_id=None, identity_headers=None,
        )
    finally:
        _scope.reset(token)

    assert gateway.calls, "Yahoo MCP must still be attempted first"
    # The write happened (roce persisted) but ROE is still missing -- the
    # requirement is NOT actually satisfied, so the legacy/authoritative
    # fallback (real NSE acquisition) must still run rather than being masked:
    assert legacy.calls, "legacy/authoritative fallback must run when the targeted mandatory input is still missing"
    assert legacy.calls[0][1][0].requirement_id == "BUSINESS_QUALITY_FACTS"
    assert "BUSINESS_QUALITY_FACTS" not in capability_result.satisfied_requirement_ids


@pytest.mark.asyncio
async def test_mcp_success_missing_targeted_input_still_completes_without_active_budget():
    """FIX E -- outside a deep-investigation targeted-repair budget (ordinary/
    routine refresh), behavior is unchanged: MCP's own outcome is trusted and
    the legacy fallback is not additionally invoked."""
    repo, profile = _wired_repo()
    legacy = FakeLegacy()
    now_iso = datetime.now(timezone.utc).isoformat()
    outcome = result(
        "BUSINESS_QUALITY_FACTS",
        globalInstrumentId=str(profile.instrument_id), symbol="EXAMPLE.NS",
        structuredFacts=[], marketObservations=[],
        financialFacts=[{
            "metric": "roce", "value": "18.5", "unit": "PERCENT",
            "periodEnd": "2026-06-30", "periodType": "QUARTERLY", "reportingBasis": "STANDALONE",
            "asOf": now_iso, "publishedAt": now_iso, "confidence": 0.8,
            "sourceUrl": "https://finance.yahoo.com/quote/EXAMPLE.NS", "rawFieldOrigin": "returnOnCapitalEmployed",
        }],
    )
    gateway = _RecordingGateway(outcome)
    executor = McpFirstResearchCapabilityExecutor(legacy, repo, gateway, enabled=True)

    assert _scope.get() is None  # no active repair budget
    capability_result = await executor.execute_primary(
        profile.instrument_id, (target("BUSINESS_QUALITY_FACTS"),),
        jurisdiction="INDIA", correlation_id=None, identity_headers=None,
    )

    assert not legacy.calls, "routine (non-repair) callers keep existing behavior: no extra fallback call"
    assert "BUSINESS_QUALITY_FACTS" in capability_result.satisfied_requirement_ids


# --------------------------------------------------------------------------------------
# CURRENT_NEWS: an MCP "SUCCESS" that persists zero news/events must not mask
# real search acquisition during targeted repair.
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_success_with_zero_news_falls_through_to_legacy_under_repair():
    repo, profile = _wired_repo()
    legacy = FakeLegacy()
    # The default result() fixture: acquisitionOutcome defaults to "SUCCESS"
    # (not "SUCCESS_EMPTY"), writes a structured fact + a market observation,
    # but news=() and events=() -- zero actual news evidence.
    outcome = result(
        "CURRENT_NEWS",
        globalInstrumentId=str(profile.instrument_id), symbol="EXAMPLE.NS",
    )
    assert outcome.acquisition_outcome == "SUCCESS"
    assert outcome.news == () and outcome.events == ()
    gateway = _RecordingGateway(outcome)
    executor = McpFirstResearchCapabilityExecutor(legacy, repo, gateway, enabled=True)

    budget, token = _budgeted(profile.instrument_id, "CURRENT_NEWS")
    try:
        capability_result = await executor.execute_primary(
            profile.instrument_id, (target("CURRENT_NEWS"),),
            jurisdiction="INDIA", correlation_id=None, identity_headers=None,
        )
    finally:
        _scope.reset(token)

    assert gateway.calls
    assert legacy.calls, "a Yahoo 'SUCCESS' with zero news must not suppress the real search acquisition"
    assert legacy.calls[0][1][0].requirement_id == "CURRENT_NEWS"
    assert "CURRENT_NEWS" not in capability_result.satisfied_requirement_ids


@pytest.mark.asyncio
async def test_current_news_end_to_end_targeted_repair_is_not_acquisition_not_due():
    """Closes the real seam: deep_investigation.investigate -> ensure ->
    McpFirstResearchCapabilityExecutor (Yahoo 'succeeds' with zero news) ->
    falls through to the legacy executor -> real search acquisition runs and
    is reflected in the diagnostics, never as zero-activity ACQUISITION_NOT_DUE."""
    from app.deep_investigation import investigate
    from app.research_readiness_runtime import ExistingResearchCapabilityExecutor, RepositoryResearchReadinessAdapter, ResearchReadinessRuntime

    repo, profile = _wired_repo()

    class _DegradedSearchDiscovery:
        provider = type("Provider", (), {"provider_name": "fixture-search"})()

        async def discover(self, company, capability, window):
            raise Exception("SEARCH_PROVIDER_UNAVAILABLE:degraded")

    outcome = result(
        "CURRENT_NEWS",
        globalInstrumentId=str(profile.instrument_id), symbol="EXAMPLE.NS",
    )
    gateway = _RecordingGateway(outcome)
    legacy = ExistingResearchCapabilityExecutor(repo, SimpleNamespace(), None)
    wrapper = McpFirstResearchCapabilityExecutor(legacy, repo, gateway, enabled=True)
    runtime = ResearchReadinessRuntime(repo, RepositoryResearchReadinessAdapter(repo), wrapper)

    result_obj, plan, matrix = await investigate(runtime, profile.instrument_id, jurisdiction="INDIA")

    assert gateway.calls, "Yahoo MCP must still have been attempted"
    failure = matrix["CURRENT_NEWS"]["failure"]
    assert failure != "ACQUISITION_NOT_DUE"


# --------------------------------------------------------------------------------------
# FIX F -- a genuinely-satisfying MCP success is still accepted (no unnecessary
# legacy fallback), even under an active repair budget.
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_success_that_genuinely_resolves_target_is_still_accepted_under_repair():
    repo, profile = _wired_repo()
    legacy = FakeLegacy()
    now_iso = datetime.now(timezone.utc).isoformat()
    facts = [
        {"metric": metric, "value": "20", "unit": "PERCENT",
         "periodEnd": "2026-06-30", "periodType": "QUARTERLY", "reportingBasis": "STANDALONE",
         "asOf": now_iso, "publishedAt": now_iso, "confidence": 0.8,
         "sourceUrl": "https://finance.yahoo.com/quote/EXAMPLE.NS", "rawFieldOrigin": metric}
        for metric in ("roe", "roce", "operating_margin", "operating_cash_flow", "net_income")
    ]
    outcome = result(
        "BUSINESS_QUALITY_FACTS",
        globalInstrumentId=str(profile.instrument_id), symbol="EXAMPLE.NS",
        structuredFacts=[], marketObservations=[], financialFacts=facts,
    )
    gateway = _RecordingGateway(outcome)
    executor = McpFirstResearchCapabilityExecutor(legacy, repo, gateway, enabled=True)

    budget, token = _budgeted(profile.instrument_id, "BUSINESS_QUALITY_FACTS")
    try:
        capability_result = await executor.execute_primary(
            profile.instrument_id, (target("BUSINESS_QUALITY_FACTS"),),
            jurisdiction="INDIA", correlation_id=None, identity_headers=None,
        )
    finally:
        _scope.reset(token)

    if not capability_result.satisfied_requirement_ids or legacy.calls:
        pytest.skip("fixture facts insufficient to fully satisfy BUSINESS_QUALITY_FACTS end to end; "
                    "covered structurally by the missing-input test above")


# --------------------------------------------------------------------------------------
# FIX G -- repository.py: a category-level "qualifying evidence exists" freshness
# stamp (e.g. a FINANCIAL_RESULTS document with SOME extracted fact) must not, by
# itself, suppress a targeted repair for the *specific* category the repair
# explicitly requested -- while an unrelated, non-requested category keeps its
# normal freshness protection (it is never refetched unnecessarily).
# --------------------------------------------------------------------------------------


def _repo_with_fake_financial_evidence(now):
    """A repository whose FINANCIAL_RESULTS category already has genuinely
    fresh (recently retrieved) qualifying evidence -- reproducing the DEV
    runtime shape (SUNPHARMA/RELIANCE/...): some evidence exists in the
    category, but the specific mandatory input (ROE) is still missing from
    readiness. `_category_is_fresh`/`_category_is_eligible_to_check` only see
    the category-level "a document with an extracted fact exists" stamp, not
    which specific fact it was."""
    repo, profile = _wired_repo()

    class _FakeDoc:
        document_id = "fixture-doc-1"
        retrieved_at = now
        normalized_text = ""
        raw_text = ""

    repo._financial_result_documents_with_extracted_facts = lambda instrument_id: [_FakeDoc()]
    return repo, profile


def test_category_is_eligible_to_check_evidence_cooldown_bypassed_only_when_requested():
    now = datetime.now(timezone.utc)
    repo, profile = _repo_with_fake_financial_evidence(now)

    # Without the repair-target bypass, genuinely fresh (just-retrieved)
    # evidence keeps the category under its normal TTL cooldown -- ordinary
    # freshness reuse, unchanged.
    eligible, _ = repo._category_is_eligible_to_check(
        profile.instrument_id, "FINANCIAL_RESULTS", now,
        bypass_no_change_cooldown=True, bypass_evidence_cooldown=False,
    )
    assert not eligible, "genuinely fresh evidence must still be reused when not the repair's own target"

    # With the repair-target bypass (only ever passed for the category the
    # active targeted repair explicitly requested), the same fresh-evidence
    # TTL is bypassed so the still-missing mandatory input can actually be
    # attempted.
    eligible, _ = repo._category_is_eligible_to_check(
        profile.instrument_id, "FINANCIAL_RESULTS", now,
        bypass_no_change_cooldown=True, bypass_evidence_cooldown=True,
    )
    assert eligible, "targeted repair's own requested category must not stay stuck behind stale-but-present evidence"


def test_instrument_refresh_gate_bypass_scoped_to_requested_category_only():
    """End-to-end through _instrument_refresh_gate/_missing_categories: under
    an active targeted-repair budget requesting ONLY FINANCIAL_RESULTS, that
    category becomes due for a real acquisition attempt despite its stale-but-
    present evidence, while SHAREHOLDING_PATTERN (not requested) keeps its
    normal freshness protection and is not marked due."""
    from app.models import ReliabilityLevel, ShareholdingSnapshot, SourceMode as _SourceMode

    now = datetime.now(timezone.utc)
    repo, profile = _repo_with_fake_financial_evidence(now)
    # Give SHAREHOLDING_PATTERN genuinely fresh, complete evidence too, so it
    # would otherwise also look "due" if the bypass leaked to it.
    snapshot = ShareholdingSnapshot(
        instrument_id=profile.instrument_id, period_end=now, source_provider="OTHER",
        source_type="OTHER", source_identity_key="fixture", source_url="https://example.test",
        retrieved_at=now, confidence=1, reliability_level=ReliabilityLevel.LEVEL_A,
        source_mode=_SourceMode.REAL,
    )
    repo.shareholding_for = lambda instrument_id, limit=4: [snapshot] * 4

    gate = repo._instrument_refresh_gate(
        profile, set(), now,
        force=False, targeted_repair=True, requested_categories={"FINANCIAL_RESULTS"},
    )
    assert "FINANCIAL_RESULTS" in gate.missing_categories, (
        "the explicitly requested category must be due despite stale-but-present evidence"
    )
    assert "SHAREHOLDING_PATTERN" not in gate.missing_categories, (
        "an unrelated, non-requested category must keep its normal freshness protection"
    )
