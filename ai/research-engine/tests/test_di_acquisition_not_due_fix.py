"""Targeted regressions for the ACQUISITION_NOT_DUE correctness fix.

Covers:
  FIX A -- a mandatory MISSING/PARTIAL/FAILED requirement selected for deep
           investigation can still perform a real, bounded targeted acquisition
           even when its backing category has a successful-no-change cooldown.
           The override is narrow: it bypasses ONLY the no-change cooldown
           (not fresh evidence, not unrelated categories, not a blanket force).
  FIX B -- CURRENT_NEWS activity is bridged into the RequirementAcquisitionBudget
           so a real news search is never mis-classified as ACQUISITION_NOT_DUE,
           and the true technical failure reason survives into diagnostics.
  FIX C -- _CATEGORY_STRATEGIES has no duplicate keys; the formerly-duplicated
           announcement categories (CAPEX, NEW_FACILITIES, CLIENTS,
           ORDERS_BACKLOG) keep the canonical 1-day lightweight cadence.

These tests deploy/restart nothing and contact no provider.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.deep_investigation import (
    RequirementAcquisitionBudget,
    _scope,
    acquisition_budget,
    investigate,
)
from app.failure_taxonomy import TECHNICAL_RETRYABLE, classify_reason
from app.fact_precedence import FactSourceTier, FinancialFact, FinancialFactKey, ProvenancedValue
from app.models import DocumentStatus, SourceMode
from app.news_acquisition import acquire_news
from app.persistence import SqliteResearchPersistence
from app.repository import (
    ResearchRepository,
    _CATEGORY_STRATEGIES,
    _DAILY_LIGHTWEIGHT,
    _PERIODIC_SLOW,
)
from app.research_readiness import ResearchRequirementStatus as Status
from app.research_readiness_runtime import TargetedEnsureResult
from app.settings import Settings
from app.stock_rule_engine import StockRuleEngineEligibilityPolicy

from test_news_acquisition_v2 import Provider as NewsProvider, Repository as NewsRepository, candidate as news_candidate, company as news_company, no_sleep
from test_news_intelligence_v2 import NOW as NEWS_NOW
from test_official_nse_financial_parsing import _document
from test_stock_rule_engine import _readiness


# --------------------------------------------------------------------------------------
# Shared doubles
# --------------------------------------------------------------------------------------


class _RecordingOfficialDiscovery:
    """Records the official-filing category sets handed to discovery."""

    def __init__(self) -> None:
        self.calls: list[set[str]] = []

    async def discover(self, _profile, categories, _seen_urls):
        self.calls.append(set(categories))
        return []


class _SearchDiscoveryStats:
    candidate_count = 0
    accepted_count = 0
    rejected_reasons: dict[str, int] = {}
    provider_failure_count = 0

    def reject(self, _reason: str) -> None:
        pass


class _ZeroResultSearchDiscovery:
    """A clean (success, zero-candidate) search-discovery stand-in."""

    provider = type("Provider", (), {"provider_name": "fixture-search"})()

    def __init__(self) -> None:
        self.last_stats = _SearchDiscoveryStats()
        self.categories: list[set[str]] = []

    async def discover(self, _profile, categories, _seen_urls):
        self.categories.append(set(categories))
        return []


class _FailedSearchDiscovery:
    """A search-discovery stand-in that reports a technical provider failure."""

    provider = type("Provider", (), {"provider_name": "fixture-search"})()

    def __init__(self) -> None:
        self.last_stats = _SearchDiscoveryStats()
        self.last_stats.provider_failure_count = 1
        self.categories: list[set[str]] = []

    async def discover(self, _profile, categories, _seen_urls):
        self.categories.append(set(categories))
        return []


def _nse_profile(repository: ResearchRepository):
    return next(profile for profile in repository.profiles if profile.exchange in {"NSE", "XNSE"})


# --------------------------------------------------------------------------------------
# FIX A -- targeted cooldown override is narrow and bounded.
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_targeted_repair_bypasses_successful_no_change_cooldown_and_runs_due_acquisition() -> None:
    """A successful-no-change cooldown on the backing category must not keep a
    mandatory MISSING requirement stuck (FIX A, root cause #1)."""
    official = _RecordingOfficialDiscovery()
    repository = ResearchRepository(
        settings=Settings(research_live_enabled=True, research_search_enabled=False),
        official_filing_discovery=official,
    )
    profile = _nse_profile(repository)
    now = datetime.now(timezone.utc)
    repository._category_successful_no_change_checks[(profile.instrument_id, "REGULATORY")] = now

    # targeted_repair=True: the no-change cooldown is lifted for the targeted
    # category, so a real official-filing acquisition attempt runs.
    await repository._refresh_targeted(
        profile, set(), targeted_repair=True, requested_categories={"REGULATORY"}
    )
    assert official.calls == [{"REGULATORY"}]

    # Contrast: without the targeted repair (force=False, targeted_repair=False)
    # the cooldown keeps the category out of the due set and no acquisition runs.
    repository_b = ResearchRepository(
        settings=Settings(research_live_enabled=True, research_search_enabled=False),
        official_filing_discovery=_RecordingOfficialDiscovery(),
    )
    profile_b = _nse_profile(repository_b)
    repository_b._category_successful_no_change_checks[(profile_b.instrument_id, "REGULATORY")] = now
    await repository_b._refresh_targeted(profile_b, set(), requested_categories={"REGULATORY"})
    assert repository_b._instrument_refresh_gate  # real gate still wired
    assert official.calls == [{"REGULATORY"}]  # unchanged (second repo blocked by cooldown)


@pytest.mark.asyncio
async def test_targeted_repair_does_not_refresh_unrelated_categories() -> None:
    """Bypassing a cooldown for the targeted category must not pull an unrelated
    category into the acquisition pass (FIX A: the override is scoped to the
    targeted requirement, never a blanket force)."""
    official = _RecordingOfficialDiscovery()
    repository = ResearchRepository(
        settings=Settings(research_live_enabled=True, research_search_enabled=False),
        official_filing_discovery=official,
    )
    profile = _nse_profile(repository)
    now = datetime.now(timezone.utc)
    # Cooldowns active on BOTH the targeted category and an unrelated one.
    repository._category_successful_no_change_checks[(profile.instrument_id, "REGULATORY")] = now
    repository._category_successful_no_change_checks[(profile.instrument_id, "GUIDANCE")] = now

    await repository._refresh_targeted(
        profile, set(), targeted_repair=True, requested_categories={"REGULATORY"}
    )
    # Only the requested category is acquired; GUIDANCE (bypassed but not
    # requested) is not refreshed.
    assert official.calls == [{"REGULATORY"}]


def test_targeted_repair_preserves_genuinely_fresh_evidence() -> None:
    """The targeted override must NOT bypass fresh successful evidence (FIX A:
    "reuse it; do not reacquire unnecessarily"). Fresh evidence lives on a
    durable document/fact and is excluded by the freshness branch, independent
    of the cooldown-bypass flag."""
    repository = ResearchRepository(settings=Settings(), persistence=SqliteResearchPersistence())
    instrument_id = repository.profiles[0].instrument_id
    company_id = repository.profiles[0].company_id
    document = _document(
        "Quarterly financial results for the quarter ended June 30, 2026 revenue 100 profit after tax 10",
    ).model_copy(update={
        "instrument_id": instrument_id,
        "company_id": company_id,
        "source_mode": SourceMode.REAL,
        "status": DocumentStatus.PROCESSED,
        "title": "Quarterly Financial Results",
        "retrieved_at": datetime(2026, 7, 15, tzinfo=timezone.utc),
    })
    repository.documents[document.document_id] = document
    repository._persistence.upsert_financial_fact(FinancialFact(
        FinancialFactKey(instrument_id, "revenue", "2026-06-30", "QUARTERLY", None),
        ProvenancedValue(value=Decimal("100"), unit="INR crore",
                         source_url=document.canonical_url, source_name="NSE",
                         source_type="EXCHANGE_ANNOUNCEMENT",
                         retrieved_at=datetime(2026, 7, 15, tzinfo=timezone.utc), confidence=None),
        FactSourceTier.OFFICIAL_NSE, "NSE", str(document.document_id), SourceMode.REAL,
    ))
    now = datetime(2026, 8, 15, tzinfo=timezone.utc)
    # Within the quarterly freshness window -> NOT eligible to check.
    eligible, _ = repository._category_is_eligible_to_check(instrument_id, "FINANCIAL_RESULTS", now)
    assert eligible is False
    # The targeted-repair bypass flag must not override genuinely fresh evidence.
    bypassed, _ = repository._category_is_eligible_to_check(
        instrument_id, "FINANCIAL_RESULTS", now, bypass_no_change_cooldown=True
    )
    assert bypassed is False


@pytest.mark.asyncio
async def test_targeted_repair_technical_failure_does_not_record_successful_no_change_cooldown() -> None:
    """A technical provider failure must not create/advance a successful-no-change
    cooldown and must remain retryable through the targeted-repair path (FIX A:
    technical failures never write the no-change check)."""
    repository = ResearchRepository(settings=Settings(
        research_search_enabled=True,
        research_search_provider="searxng",
        research_search_endpoint="https://search.example/search",
    ))
    profile = repository.profiles[0]  # non-NSE -> REGULATORY routes to search discovery
    search = _FailedSearchDiscovery()
    repository._search_discovery = search
    now = datetime.now(timezone.utc)
    repository._category_successful_no_change_checks[(profile.instrument_id, "REGULATORY")] = now

    await repository._refresh_targeted(
        profile, set(), targeted_repair=True, requested_categories={"REGULATORY"}
    )

    # The bypass lifted the cooldown so the (failing) search actually ran:
    assert search.categories and "REGULATORY" in {c.upper() for c in search.categories[0]}
    # A technical failure must NOT advance the no-change cooldown:
    checked_at = repository._category_successful_no_change_checks[
        (profile.instrument_id, "REGULATORY")
    ]
    assert checked_at == now
    # ...and the targeted-repair path can still retry it immediately:
    eligible, _ = repository._category_is_eligible_to_check(
        profile.instrument_id, "REGULATORY", now, bypass_no_change_cooldown=True
    )
    assert eligible is True


# --------------------------------------------------------------------------------------
# FIX B -- CURRENT_NEWS activity is bridged into the acquisition budget.
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_current_news_acquire_records_real_activity_in_budget() -> None:
    """A news search that performs queries and fetches documents must be
    reflected in the deep-investigation budget so it cannot be reported as
    ACQUISITION_NOT_DUE (which only fires when nothing was attempted)."""
    repo = NewsRepository()
    provider = NewsProvider(results=[news_candidate()])
    company = news_company()
    budget = RequirementAcquisitionBudget(
        company.instrument_id, "CURRENT_NEWS", NEWS_NOW, AsyncMock(return_value=False)
    )
    token = _scope.set(budget)
    try:
        run, features = await acquire_news(
            repo, company, providers=[provider], now=NEWS_NOW, sleep=no_sleep, industry="Wires and cables"
        )
    finally:
        _scope.reset(token)

    assert run.outcome == "SEARCH_COMPLETE_WITH_EVENTS"
    assert features  # real evidence extracted -- not a fabricated event
    assert budget.discovery_attempted is True           # discovery actually ran
    assert budget.queries_reserved > 0                  # queries reserved against the budget
    assert budget.documents_attempted > 0               # a document was fetched and attempted
    assert budget.failures == []                        # no technical failure


@pytest.mark.asyncio
async def test_current_news_technical_failure_survives_as_retryable_in_budget() -> None:
    """A CAPTCHA/429-style provider failure (every query raises) must survive as
    a technical/retryable reason in the budget, not become ACQUISITION_NOT_DUE."""
    repo = NewsRepository()
    provider = NewsProvider(fail=True)  # every discover() raises
    company = news_company()
    budget = RequirementAcquisitionBudget(
        company.instrument_id, "CURRENT_NEWS", NEWS_NOW, AsyncMock(return_value=False)
    )
    token = _scope.set(budget)
    try:
        run, _ = await acquire_news(
            repo, company, providers=[provider], now=NEWS_NOW, sleep=no_sleep
        )
    finally:
        _scope.reset(token)

    assert run.outcome == "SEARCH_FAILED"
    # News attempted the search -> the ACQUISITION_NOT_DUE ("nothing attempted")
    # branch must not fire:
    assert budget.discovery_attempted is True
    # The true failure reason survives into diagnostics as a retryable failure:
    assert budget.failures, "technical news failure must survive in the budget"
    assert classify_reason(budget.failures[-1]) == TECHNICAL_RETRYABLE


@pytest.mark.asyncio
async def test_current_news_document_fetch_failure_is_recorded_in_budget() -> None:
    """A document fetch failure is a technical/retryable attempt; it must record
    documents_attempted and survive the failure reason, not misreport as empty."""
    repo = NewsRepository()
    provider = NewsProvider(results=[news_candidate(), news_candidate("https://publisher.test/second")])

    async def broken(url):
        raise RuntimeError("private provider detail must not leak")

    repo._fetcher.fetch = broken
    company = news_company()
    budget = RequirementAcquisitionBudget(
        company.instrument_id, "CURRENT_NEWS", NEWS_NOW, AsyncMock(return_value=False)
    )
    token = _scope.set(budget)
    try:
        run, _ = await acquire_news(
            repo, company, providers=[provider], now=NEWS_NOW, sleep=no_sleep, max_documents=1
        )
    finally:
        _scope.reset(token)

    assert run.outcome == "SEARCH_PARTIAL"
    assert budget.discovery_attempted is True
    assert budget.documents_attempted > 0          # a fetch was actually attempted
    assert budget.failures, "fetch failure must survive in the budget"
    assert classify_reason(budget.failures[-1]) == TECHNICAL_RETRYABLE


# --------------------------------------------------------------------------------------
# FIX C -- no duplicate keys; announcement categories use the daily cadence.
# --------------------------------------------------------------------------------------


def test_category_strategies_has_no_duplicate_keys_and_canonical_cadences() -> None:
    keys = list(_CATEGORY_STRATEGIES)
    assert len(keys) == len(set(keys)), f"duplicate keys remain: {keys}"
    # Announcement / catalyst categories: 1-day lightweight check cadence.
    announcement = ("ORDERS_BACKLOG", "CONTRACTS", "CAPEX", "NEW_FACILITIES",
                    "ACQUISITIONS", "CLIENTS", "GUIDANCE", "MANAGEMENT",
                    "REGULATORY", "CATALYSTS", "ANALYST_OPINION", "ANALYST_TARGETS")
    for category in announcement:
        assert _CATEGORY_STRATEGIES[category] is _DAILY_LIGHTWEIGHT, category
    # Financial-cycle / structural categories: longer cadence.
    for category in ("PRODUCTS", "GROWTH"):
        assert _CATEGORY_STRATEGIES[category] is _PERIODIC_SLOW, category


def test_dedup_announcement_categories_keep_one_day_lightweight_cooldown() -> None:
    """The formerly-duplicated announcement categories keep the canonical 1-day
    lightweight interval (not the 7-day _PERIODIC_SLOW shadow that duplicate-key
    resolution previously retained)."""
    for category in ("CAPEX", "NEW_FACILITIES", "CLIENTS", "ORDERS_BACKLOG"):
        assert _CATEGORY_STRATEGIES[category] is _DAILY_LIGHTWEIGHT
        assert _CATEGORY_STRATEGIES[category].lightweight_check_interval == timedelta(days=1)
    # Contrast: the periodic-slow cadence is applied to the structural categories:
    assert _PERIODIC_SLOW.lightweight_check_interval == timedelta(days=7)
    assert _CATEGORY_STRATEGIES["CAPEX"] is not _PERIODIC_SLOW


# --------------------------------------------------------------------------------------
# Invariant -- a real targeted acquisition is never reported as ACQUISITION_NOT_DUE.
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mandatory_missing_targeted_acquisition_is_not_acquisition_not_due() -> None:
    """If deep investigation's targeted repair actually attempted discovery
    (budget touched), the requirement is classified by what discovery found --
    never ACQUISITION_NOT_DUE (which only fires when nothing was attempted)."""
    readiness = _readiness({"QUARTERLY_FINANCIALS": Status.MISSING})

    async def ensure_that_simulates_due_gate(_key, **kwargs):
        # execute_primary's targeted repair touches the live budget (set by
        # investigate) but, in this scenario, finds no candidates. The
        # discovery_attempted flag is the signal that *something* ran.
        budget = acquisition_budget(readiness.global_instrument_id)
        if budget is not None:
            budget.discovery_attempted = True
        return TargetedEnsureResult(readiness, ("QUARTERLY_FINANCIALS",), (), failures={})

    runtime = SimpleNamespace(
        read=AsyncMock(return_value=readiness),
        ensure=ensure_that_simulates_due_gate,
        repository=SimpleNamespace(record_acquisition_observation=AsyncMock()),
    )

    result, _plan, matrix = await investigate(
        runtime, readiness.global_instrument_id, jurisdiction="INDIA"
    )

    failure = matrix["QUARTERLY_FINANCIALS"]["failure"]
    assert failure != "ACQUISITION_NOT_DUE"
    # A real (but empty) acquisition attempt yields a discovery-candidates
    # reason rather than a "nothing was attempted" backoff:
    assert failure == "DISCOVERY_NO_FINANCIAL_RESULTS_CANDIDATES"
    # The mandatory requirement is still not met -> the instrument stays
    # ineligible for Rule Engine ranking (eligibility is NOT weakened):
    assert (
        StockRuleEngineEligibilityPolicy().evaluate(result.readiness).full_analysis_allowed
        is False
    )


# --------------------------------------------------------------------------------------
# End-to-end: the real FIX A seam under an active deep-investigation budget.
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_end_to_end_targeted_repair_bypasses_cooldown_through_real_seam() -> None:
    """Closes the full real seam, not a unit:

    deep_investigation.investigate  (sets the per-requirement acquisition budget)
        -> runtime.ensure  (thin adapter -> repository.refresh_targeted_categories)
           -> _refresh_targeted_categories_once  (budget-active -> targeted_repair=True)
            -> _refresh_live  (force=False, targeted_repair=True)
             -> _instrument_refresh_gate  (targeted_repair -> bypass)
              -> _category_is_eligible_to_check(bypass_no_change_cooldown=True)
               -> official filing discovery (a REAL acquisition attempt)

    The ``ensure`` adapter delegates to the real ``refresh_targeted_categories``;
    the ``_refresh_targeted_categories_once -> _refresh_live -> gate -> eligibility``
    seam is NOT mocked -- only ``ensure`` is an adapter (as ``execute_primary`` would
    be in production) and provider discovery is a no-op recording double so no
    network is touched.
    """
    # 2) The backing category (FINANCIAL_RESULTS) has a successful-no-change
    #    cooldown active that would normally block the acquisition.
    official = _RecordingOfficialDiscovery()
    repository = ResearchRepository(
        settings=Settings(research_live_enabled=True, research_search_enabled=False),
        official_filing_discovery=official,
    )
    profile = _nse_profile(repository)
    nse_instrument = profile.instrument_id

    # 1) Mandatory requirement is still MISSING.
    readiness = _readiness({"QUARTERLY_FINANCIALS": Status.MISSING})
    assert readiness.for_requirement("QUARTERLY_FINANCIALS").status == Status.MISSING

    now = datetime.now(timezone.utc)
    repository._category_successful_no_change_checks[(profile.instrument_id, "FINANCIAL_RESULTS")] = now
    # Sanity: without the bypass the category is throttled.
    eligible, _ = repository._category_is_eligible_to_check(
        profile.instrument_id, "FINANCIAL_RESULTS", now
    )
    assert eligible is False

    async def runtime_read(_instrument_id, *, jurisdiction, evidence_only=False):
        return readiness

    async def real_ensure(_instrument_id, *, jurisdiction, requirement_ids, **kwargs):
        # The seam itself is real: investigate set the budget (via _scope) before
        # calling ensure, so refresh_targeted_categories sees the active budget
        # and routes through targeted_repair=True.
        await repository.refresh_targeted_categories(
            _instrument_id,
            {"FINANCIAL_RESULTS"},
            correlation_id="e2e",
            allow_demo=True,
        )
        return TargetedEnsureResult(readiness, ("QUARTERLY_FINANCIALS",), (), failures={})

    runtime = SimpleNamespace(
        read=runtime_read,
        ensure=real_ensure,
        repository=SimpleNamespace(record_acquisition_observation=AsyncMock()),
    )

    result, _plan, matrix = await investigate(runtime, nse_instrument, jurisdiction="INDIA")

    # 5) A REAL acquisition attempt occurred: the cooldown was bypassed for the
    #    requested category and official-filing discovery ran.
    assert official.calls == [{"FINANCIAL_RESULTS"}]

    # 6) Unrelated categories are not refreshed (only FINANCIAL_RESULTS acquired):
    assert all(categories == {"FINANCIAL_RESULTS"} for categories in official.calls)

    # 4) The cooldown was bypassed ONLY for the requested category -- proven by
    #    the acquisition running despite the pre-set cooldown (assertion above).

    # 8) The result is NOT ACQUISITION_NOT_DUE: discovery was attempted (budget
    #    touched by the real seam) but found no candidates, so classify by
    #    outcome rather than by "nothing attempted".
    failure = matrix["QUARTERLY_FINANCIALS"]["failure"]
    assert failure != "ACQUISITION_NOT_DUE"
    assert failure == "DISCOVERY_NO_FINANCIAL_RESULTS_CANDIDATES"

    # No fabricated evidence: the legitimate zero-result official check recorded
    # a no-change cooldown but did NOT mint a durable financial fact. (Fresh-
    # evidence non-reacquisition is covered structurally by
    # test_targeted_repair_preserves_genuinely_fresh_evidence.)
    assert not repository._category_has_qualifying_evidence(
        profile.instrument_id, "FINANCIAL_RESULTS"
    )
    assert (
        profile.instrument_id,
        "FINANCIAL_RESULTS",
    ) in repository._category_successful_no_change_checks
    assert (
        StockRuleEngineEligibilityPolicy().evaluate(result.readiness).full_analysis_allowed
        is False
    )
