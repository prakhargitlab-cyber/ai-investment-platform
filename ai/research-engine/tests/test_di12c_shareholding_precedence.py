"""DI-12C FIX 1 regression tests -- SHAREHOLDING authoritative evidence precedence.

Provider-free: no real providers, no real network, no real disk persistence
(the in-memory SqliteResearchPersistence is used only as a durable store
double; no external DB is opened for these paths).

Invariants enforced:
* A non-authoritative SEARCH_PROVIDER_DEGRADED fallback for SHAREHOLDING_PATTERN
  cannot mark SHAREHOLDING as blocking when durable authoritative NSE
  shareholding evidence exists or becomes available.
* A group capability budget is NOT whole-group-stopped merely because
  SHAREHOLDING was resolved: the other group members
  (ORDER_BOOK_CAPEX_GUIDANCE / GOVERNANCE_HISTORY) still run their own search,
  and a failure from their search is NOT attributed to SHAREHOLDING.
* An actual authoritative NSE failure with NO durable evidence remains a
  truthful, retryable technical failure.

Attribution note (traced before changing behaviour):
``_refresh_search_discovery`` receives a GROUP ``RequirementAcquisitionBudget``
(e.g. ``ORDER_BOOK_CAPEX_GUIDANCE+SHAREHOLDING+GOVERNANCE_HISTORY``).  Its
``failures`` list is shared across every member, and ``deep_investigation.
_finalize`` attributes ``budget.failures[-1]`` to each member that is not yet
sufficient.  Therefore a single group-level search degradation previously
could be copied to every unresolved member INCLUDING SHAREHOLDING.  The
repository-side category filter below removes SHAREHOLDING_PATTERN from the
searched set when durable evidence exists, so that degradation can no longer
be attributed to SHAREHOLDING; and when SHAREHOLDING is actually sufficient,
``_finalize``'s own ``_requirement_sufficient`` check (unchanged) already
short-circuits before appending a member failure.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.deep_investigation import RequirementAcquisitionBudget, _scope
from app.models import (
    ReliabilityLevel,
    ShareholdingCategory,
    ShareholdingSnapshot,
    ShareholdingSnapshotValue,
    SourceMode,
    SourceType,
)
from app.persistence import SqliteResearchPersistence
from app.repository import ResearchRepository
from app.settings import Settings
from app.source_discovery import SearchProviderError


INSTRUMENT_ID = UUID("00000000-0000-0000-0000-000000000001")
QUARTER_END = datetime(2026, 3, 31, tzinfo=timezone.utc)


def _shareholding_snapshot() -> ShareholdingSnapshot:
    """A single qualifying, durable, REAL NSE shareholding snapshot."""
    return ShareholdingSnapshot(
        instrument_id=INSTRUMENT_ID,
        period_end=QUARTER_END,
        source_provider="NSE",
        source_type=str(SourceType.EXCHANGE_ANNOUNCEMENT),
        source_identity_key="NSE:EXAMPLE",
        source_url="https://nseindia.com/shareholding/EXAMPLE",
        research_document_id=uuid4(),
        published_at=QUARTER_END,
        retrieved_at=QUARTER_END,
        confidence=Decimal("0.99"),
        reliability_level=ReliabilityLevel.LEVEL_A,
        source_mode=SourceMode.REAL,
        values=[
            ShareholdingSnapshotValue(
                category=ShareholdingCategory.PROMOTER,
                percentage=Decimal("12.5"),
            ),
        ],
    )


class _FakeSearchDiscovery:
    """Records discover() invocation and returns a configurable result."""

    def __init__(self, *, discover_result=()):
        self.discover_result = discover_result
        self.discover_called = False
        self.discover_categories: set[str] | None = None
        self.profile: Any = None
        self.provider = SimpleNamespace(provider_name="SEARCH_FAKE")
        self.last_stats = SimpleNamespace(
            reject=lambda *a, **k: None,
            candidate_count=0,
            accepted_count=0,
            rejected_reasons=[],
            documents_fetched=0,
            events_extracted=0,
            provider_failure_count=0,
        )

    async def discover(self, profile, categories, seen_urls):
        self.discover_called = True
        self.discover_categories = set(categories)
        self.profile = profile
        return self.discover_result


class _DegradedSearchDiscovery(_FakeSearchDiscovery):
    """discover() raises a SEARCH_PROVIDER_DEGRADED-style failure."""

    async def discover(self, profile, categories, seen_urls):
        self.discover_called = True
        self.discover_categories = set(categories)
        self.profile = profile
        raise SearchProviderError(
            "SEARCH_PROVIDER_UNAVAILABLE:SEARCH_PROVIDER_DEGRADED:unresponsive_engines=3"
        )


class _FakePersistence(SqliteResearchPersistence):
    """In-memory persistence double (':memory:') used so no external DB
    service is touched. Inherits the real schema + upsert_acquisition_observation."""

    def __init__(self):
        super().__init__()


def _repo(*, discovery, shareholding_snapshots=()):
    repo = ResearchRepository(
        settings=Settings(research_live_enabled=True, research_demo_enabled=False),
        persistence=_FakePersistence(),
        search_discovery=discovery,
    )
    repo.profiles = [SimpleNamespace(
        instrument_id=INSTRUMENT_ID, company_name="HFC Example", company_id=uuid4(),
        jurisdiction="INDIA", country="IN", exchange="NSE",
    )]
    if shareholding_snapshots:
        repo.shareholding_snapshots[INSTRUMENT_ID] = shareholding_snapshots[0]
    return repo


def _profile():
    return SimpleNamespace(
        instrument_id=INSTRUMENT_ID, company_name="HFC Example", company_id=uuid4(),
        jurisdiction="INDIA", country="IN", exchange="NSE", mic="XNSE", currency="INR",
    )


def _missing_with_shareholding():
    # Display-category strings the plan produces for the
    # ORDER_BOOK_CAPEX_GUIDANCE + SHAREHOLDING + GOVERNANCE_HISTORY group.
    return {
        "Shareholding Pattern",
        "Orders & Backlog",
        "Regulatory",
        "Management",
    }


def _group_budget(label="ORDER_BOOK_CAPEX_GUIDANCE+SHAREHOLDING+GOVERNANCE_HISTORY"):
    async def _sufficient():
        return False
    return RequirementAcquisitionBudget(
        instrument_id=INSTRUMENT_ID,
        requirement_id=label,
        as_of=datetime(2026, 9, 26, tzinfo=timezone.utc),
        sufficient=_sufficient,
        member_requirement_ids=(
            "ORDER_BOOK_CAPEX_GUIDANCE", "SHAREHOLDING", "GOVERNANCE_HISTORY"
        ),
    )


def _run(coro):
    return asyncio.run(coro)


def _record_observation(instrument_id, requirement_id, provider, outcome, failure_reason=None):
    return SimpleNamespace(
        global_instrument_id=instrument_id,
        requirement_id=requirement_id,
        provider=provider,
        outcome=outcome,
        observed_at=datetime(2026, 9, 20, tzinfo=timezone.utc),
        failure_reason=failure_reason,
        source_url=None,
        evidence_count=0,
    )


def test_fix1_qualifying_durable_shareholding_evidence_excludes_shareholding_from_search():
    """TEST 1: qualifying durable real NSE shareholding snapshot exists ->
    generic search discovery is NOT invoked for SHAREHOLDING_PATTERN ->
    budget.failures is not populated with a SHAREHOLDING-attributable failure."""
    discovery = _DegradedSearchDiscovery()
    repo = _repo(discovery=discovery, shareholding_snapshots=[_shareholding_snapshot()])
    budget = _group_budget()
    token = _scope.set(budget)
    try:
        result = _run(repo._refresh_search_discovery(
            _profile(), _missing_with_shareholding(), set(), protect_error=None,
        ))
    finally:
        _scope.reset(token)

    # The group budget is NOT whole-group stopped (other members still sought).
    assert budget.stopped is False
    # search WAS invoked (for OB/Governance), but SHAREHOLDING_PATTERN category
    # was filtered out of the searched set before hitting the provider.
    assert discovery.discover_called is True
    assert "Shareholding Pattern" not in discovery.discover_categories
    # The provider degradation, if recorded, is attributable only to the
    # searched categories and never to SHAREHOLDING.
    assert not any("SHAREHOLDING" in f for f in budget.failures)


def test_fix1_stale_failed_search_observation_does_not_block_shareholding_with_durable_evidence():
    """TEST 2: a stale, non-authoritative FAILED search observation persisted
    from a prior refresh, PLUS a qualifying durable NSE shareholding snapshot,
    leaves SHAREHOLDING correctly classifiable from durable evidence."""
    repo = _repo(discovery=_FakeSearchDiscovery(), shareholding_snapshots=[_shareholding_snapshot()])
    # Simulate a stale, non-authoritative FAILED observation from a prior
    # degraded search (the ABCAPIAL shape), recorded via the persistence layer.
    from app.repository import _canonical_refresh_category
    repo._persistence.upsert_acquisition_observation(
        INSTRUMENT_ID, "SHAREHOLDING", "READINESS_EXECUTOR", "FAILED",
        datetime(2026, 9, 20, tzinfo=timezone.utc), None,
        "SOURCE_UNAVAILABLE:SEARCH_PROVIDER_UNAVAILABLE:SEARCH_PROVIDER_DEGRADED:unresponsive_engines=3",
        0,
    )
    # _category_has_qualifying_evidence checks durable shareholding, NOT the
    # stale observation -> returns True.
    assert repo._category_has_qualifying_evidence(
        INSTRUMENT_ID, _canonical_refresh_category("Shareholding Pattern")
    ) is True
    # And the repository-side correction would skip SHAREHOLDING search in a
    # fresh refresh (no provider call for it).
    discovery = _FakeSearchDiscovery()
    repo._search_discovery = discovery
    budget = _group_budget()
    token = _scope.set(budget)
    try:
        _run(repo._refresh_search_discovery(
            _profile(), _missing_with_shareholding(), set(), protect_error=None,
        ))
    finally:
        _scope.reset(token)
    assert "Shareholding Pattern" not in discovery.discover_categories
    assert not any("SHAREHOLDING" in f for f in budget.failures)


def test_fix1_no_durable_evidence_real_search_failure_stays_truthful():
    """TEST 3: NO qualifying shareholding evidence + a real search/provider
    failure -> failure remains truthful/retryable and is NOT swallowed."""
    discovery = _DegradedSearchDiscovery()
    repo = _repo(discovery=discovery, shareholding_snapshots=())  # no durable evidence
    budget = _group_budget()
    token = _scope.set(budget)
    try:
        result = _run(repo._refresh_search_discovery(
            _profile(), _missing_with_shareholding(), set(), protect_error=None,
        ))
    finally:
        _scope.reset(token)

    # With durable evidence for NOTHING, the search runs for every category
    # (including SHAREHOLDING) and the provider degradation is recorded.
    assert discovery.discover_called is True
    assert "Shareholding Pattern" in discovery.discover_categories
    assert any("SEARCH_PROVIDER_DEGRADED" in f for f in budget.failures)
    assert result is False


def test_fix1_group_not_whole_terminated_when_only_shareholding_resolved():
    """The SHAREHOLDING-only resolution must NOT stop the whole group budget;
    the other members continue and a search failure they incur stays with them."""
    discovery = _DegradedSearchDiscovery()
    repo = _repo(discovery=discovery, shareholding_snapshots=[_shareholding_snapshot()])
    budget = _group_budget()
    token = _scope.set(budget)
    try:
        _run(repo._refresh_search_discovery(
            _profile(), _missing_with_shareholding(), set(), protect_error=None,
        ))
    finally:
        _scope.reset(token)
    assert budget.stopped is False
    assert discovery.discover_called is True


def test_fix1_shareholding_failure_not_attributed_from_other_category_degradation():
    """A SEARCH_PROVIDER_DEGRADED from an ORDER_BOOK / GOVERNANCE search in the
    same group must not be copied onto the SHAREHOLDING member. With durable
    shareholding evidence present, SHAREHOLDING receives no failure at all."""
    discovery = _DegradedSearchDiscovery()
    repo = _repo(discovery=discovery, shareholding_snapshots=[_shareholding_snapshot()])
    budget = _group_budget()
    token = _scope.set(budget)
    try:
        _run(repo._refresh_search_discovery(
            _profile(), _missing_with_shareholding(), set(), protect_error=None,
        ))
    finally:
        _scope.reset(token)
    assert not any("SHAREHOLDING" in f for f in budget.failures)


# --------------------------------------------------------------------------- #
# BLOCKER 2: SHAREHOLDING qualifying evidence requires mandatory categories
# -----------
# A snapshot must contain at least one recognized ownership category to
# qualify as SHAREHOLDING_PATTERN evidence. Empty snapshots (no recognized
# categories) do NOT qualify and should NOT suppress recovery/search.
# --------------------------------------------------------------------------- #

def _empty_shareholding_snapshot() -> ShareholdingSnapshot:
    """A snapshot with no recognized ownership categories - not qualifying."""
    return ShareholdingSnapshot(
        instrument_id=INSTRUMENT_ID,
        period_end=QUARTER_END,
        source_provider="NSE",
        source_type=str(SourceType.EXCHANGE_ANNOUNCEMENT),
        source_identity_key="NSE:EMPTY",
        source_url="https://nseindia.com/shareholding/EMPTY",
        research_document_id=uuid4(),
        published_at=QUARTER_END,
        retrieved_at=QUARTER_END,
        confidence=Decimal("0.99"),
        reliability_level=ReliabilityLevel.LEVEL_A,
        source_mode=SourceMode.REAL,
        values=[],  # Empty - no recognized categories
    )


def _snapshot_with_category(category: ShareholdingCategory) -> ShareholdingSnapshot:
    """A snapshot with a specific recognized ownership category."""
    # PROMOTER_PLEDGE requires metric_basis
    if category == ShareholdingCategory.PROMOTER_PLEDGE:
        return ShareholdingSnapshot(
            instrument_id=INSTRUMENT_ID,
            period_end=QUARTER_END,
            source_provider="NSE",
            source_type=str(SourceType.EXCHANGE_ANNOUNCEMENT),
            source_identity_key="NSE:WITH_CATEGORY",
            source_url="https://nseindia.com/shareholding/WITH_CATEGORY",
            research_document_id=uuid4(),
            published_at=QUARTER_END,
            retrieved_at=QUARTER_END,
            confidence=Decimal("0.99"),
            reliability_level=ReliabilityLevel.LEVEL_A,
            source_mode=SourceMode.REAL,
            values=[
                ShareholdingSnapshotValue(
                    category=category,
                    percentage=Decimal("10.0"),
                    metric_basis="PROMOTER_PLEDGE_VALUE"  # Required for PROMOTER_PLEDGE
                ),
            ],
        )
    return ShareholdingSnapshot(
        instrument_id=INSTRUMENT_ID,
        period_end=QUARTER_END,
        source_provider="NSE",
        source_type=str(SourceType.EXCHANGE_ANNOUNCEMENT),
        source_identity_key="NSE:WITH_CATEGORY",
        source_url="https://nseindia.com/shareholding/WITH_CATEGORY",
        research_document_id=uuid4(),
        published_at=QUARTER_END,
        retrieved_at=QUARTER_END,
        confidence=Decimal("0.99"),
        reliability_level=ReliabilityLevel.LEVEL_A,
        source_mode=SourceMode.REAL,
        values=[
            ShareholdingSnapshotValue(
                category=category,
                percentage=Decimal("10.0"),
            ),
        ],
    )


def test_blocker2_snapshot_with_recognized_category_qualifies():
    """A snapshot containing a recognized ownership category (e.g. PROMOTER)
    IS qualifying evidence for SHAREHOLDING_PATTERN."""
    from app.repository import _canonical_refresh_category
    repo = _repo(discovery=_FakeSearchDiscovery(), shareholding_snapshots=[_snapshot_with_category(ShareholdingCategory.PROMOTER)])
    assert repo._category_has_qualifying_evidence(
        INSTRUMENT_ID, _canonical_refresh_category("Shareholding Pattern")
    ) is True


def test_blocker2_empty_snapshot_not_qualifying():
    """A snapshot with ZERO recognized categories is NOT qualifying evidence.
    This prevents suppressing recovery/search when snapshot is essentially empty."""
    from app.repository import _canonical_refresh_category
    repo = _repo(discovery=_DegradedSearchDiscovery(), shareholding_snapshots=[_empty_shareholding_snapshot()])
    # Empty snapshot should NOT qualify
    assert repo._category_has_qualifying_evidence(
        INSTRUMENT_ID, _canonical_refresh_category("Shareholding Pattern")
    ) is False


def test_blocker2_snapshot_without_category_triggers_search():
    """When no qualifying shareholding evidence exists, search is triggered."""
    discovery = _DegradedSearchDiscovery()
    repo = _repo(discovery=discovery, shareholding_snapshots=[_empty_shareholding_snapshot()])
    # Verify _category_has_qualifying_evidence returns False
    from app.repository import _canonical_refresh_category
    assert repo._category_has_qualifying_evidence(
        INSTRUMENT_ID, _canonical_refresh_category("Shareholding Pattern")
    ) is False
    # The degraded search should still run for SHAREHOLDING_PATTERN
    budget = _group_budget()
    token = _scope.set(budget)
    try:
        _run(repo._refresh_search_discovery(
            _profile(), _missing_with_shareholding(), set(), protect_error=None,
        ))
    finally:
        _scope.reset(token)
    assert "Shareholding Pattern" in discovery.discover_categories


def test_blocker2_any_recognized_category_qualifies():
    """Any recognized ownership category (PROMOTER, FII_FPI, DII, etc.)
    should qualify the shareholding pattern."""
    from app.repository import _canonical_refresh_category
    recognized_categories = [
        ShareholdingCategory.PROMOTER, ShareholdingCategory.FII_FPI,
        ShareholdingCategory.DII, ShareholdingCategory.PUBLIC_RETAIL,
    ]
    for category in recognized_categories:
        repo = _repo(discovery=_FakeSearchDiscovery(), shareholding_snapshots=[_snapshot_with_category(category)])
        assert repo._category_has_qualifying_evidence(
            INSTRUMENT_ID, _canonical_refresh_category("Shareholding Pattern")
        ), f"Expected {category} to qualify"


# --------------------------------------------------------------------------- #
# BLOCKER 3: PLEDGE SEMANTICS - explicit 0% pledge != missing pledge
# -----------
# Three states must be preserved:
# 1. explicit 0% pledge -> PRESENT evidence with value 0
# 2. explicit >0% pledge -> PRESENT evidence with that value
# 3. pledge absent/unreported -> MISSING, never fabricated as 0
# --------------------------------------------------------------------------- #

def _snapshot_with_explicit_pledge(percentage: Decimal) -> ShareholdingSnapshot:
    """A snapshot with PROMOTER and PROMOTER_PLEDGE categories.
    Metric_basis is required for PROMOTER_PLEDGE."""
    return ShareholdingSnapshot(
        instrument_id=INSTRUMENT_ID,
        period_end=QUARTER_END,
        source_provider="NSE",
        source_type=str(SourceType.EXCHANGE_ANNOUNCEMENT),
        source_identity_key="NSE:PLEDGE_TEST",
        source_url="https://nseindia.com/shareholding/PLEDGE_TEST",
        research_document_id=uuid4(),
        published_at=QUARTER_END,
        retrieved_at=QUARTER_END,
        confidence=Decimal("0.99"),
        reliability_level=ReliabilityLevel.LEVEL_A,
        source_mode=SourceMode.REAL,
        values=[
            ShareholdingSnapshotValue(
                category=ShareholdingCategory.PROMOTER,
                percentage=Decimal("50.0"),
            ),
            ShareholdingSnapshotValue(
                category=ShareholdingCategory.PROMOTER_PLEDGE,
                percentage=percentage,
                metric_basis="PROMOTER_PLEDGE_VALUE"  # Required for PROMOTER_PLEDGE
            ),
        ],
    )


def _snapshot_with_promoter_only() -> ShareholdingSnapshot:
    """A snapshot with PROMOTER but NO PROMOTER_PLEDGE category."""
    return ShareholdingSnapshot(
        instrument_id=INSTRUMENT_ID,
        period_end=QUARTER_END,
        source_provider="NSE",
        source_type=str(SourceType.EXCHANGE_ANNOUNCEMENT),
        source_identity_key="NSE:PROMOTER_ONLY",
        source_url="https://nseindia.com/shareholding/PROMOTER_ONLY",
        research_document_id=uuid4(),
        published_at=QUARTER_END,
        retrieved_at=QUARTER_END,
        confidence=Decimal("0.99"),
        reliability_level=ReliabilityLevel.LEVEL_A,
        source_mode=SourceMode.REAL,
        values=[
            ShareholdingSnapshotValue(
                category=ShareholdingCategory.PROMOTER,
                percentage=Decimal("50.0"),
            ),
            # No PROMOTER_PLEDGE - pledge is missing
        ],
    )



import pytest

@pytest.mark.parametrize("percentage", [Decimal("0"), Decimal("5"), None])
def test_blocker3_pledge_presence_and_value(percentage):
    from dataclasses import replace
    from test_di12c_financial_sector_balance_sheet import _financial_inputs
    from app.stock_rule_engine import StockRuleEngineV1
    from app.research_readiness_runtime import RepositoryResearchReadinessAdapter
    snapshot = (_snapshot_with_promoter_only() if percentage is None
                else _snapshot_with_explicit_pledge(percentage))
    result = StockRuleEngineV1()._shareholding(replace(_financial_inputs(), shareholding=(snapshot,)))
    pledge = next((m for m in result.metrics if m.metric == "PROMOTER_PLEDGE"), None)
    evidence = {"SHAREHOLDING": []}
    RepositoryResearchReadinessAdapter._append_shareholding(evidence, [snapshot])
    covered = evidence["SHAREHOLDING"][0].covered_input_ids
    if percentage is None:
        assert pledge is None
        assert "PROMOTER_PLEDGE" not in covered
    else:
        assert pledge is not None
        assert pledge.value == percentage
        assert "PROMOTER_PLEDGE" in covered


def test_pledge_only_snapshot_does_not_cover_mandatory_ownership():
    snapshot = _snapshot_with_explicit_pledge(Decimal("0"))
    snapshot = snapshot.model_copy(update={"values": [snapshot.values[1]]})
    repo = _repo(discovery=_FakeSearchDiscovery(), shareholding_snapshots=[snapshot])
    assert not repo._category_has_qualifying_evidence(INSTRUMENT_ID, "SHAREHOLDING_PATTERN")


@pytest.mark.parametrize("category", list(ShareholdingCategory))
def test_qualifying_shareholding_matches_readiness_ownership_coverage(category):
    from app.research_readiness_runtime import RepositoryResearchReadinessAdapter
    snapshot = _snapshot_with_explicit_pledge(Decimal("0")) if category == ShareholdingCategory.PROMOTER_PLEDGE else _snapshot_with_category(category)
    if category == ShareholdingCategory.PROMOTER_PLEDGE:
        snapshot = snapshot.model_copy(update={"values": [snapshot.values[1]]})
    evidence = {"SHAREHOLDING": []}
    RepositoryResearchReadinessAdapter._append_shareholding(evidence, [snapshot])
    repo = _repo(discovery=_FakeSearchDiscovery(), shareholding_snapshots=[snapshot])
    mandatory_present = "PROMOTER_INSTITUTIONAL_PUBLIC_CATEGORIES" in evidence["SHAREHOLDING"][0].covered_input_ids
    assert repo._category_has_qualifying_evidence(INSTRUMENT_ID, "SHAREHOLDING_PATTERN") == mandatory_present
    assert mandatory_present == (category in {ShareholdingCategory.PROMOTER, ShareholdingCategory.FII_FPI,
                                             ShareholdingCategory.DII, ShareholdingCategory.PUBLIC_RETAIL})
