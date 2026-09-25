"""DI-18: Financial readiness hardening regressions.

Covers four test-only corrections (reporting-basis separation, NSE SUCCESS_EMPTY
must not claim readiness, bounded official-filing fetch timeout with Yahoo
fallback retained, ORDER_BOOK evidence retained while CAPEX/GUIDANCE stay missing)
plus the OCF-summary-adapter fallthrough exercised against the Correction 5
production fix in ``portfolio_orchestration._market_fundamentals_from_record``.

Fixtures are reused from the sibling DI-15 / DI-12A / test_official_nse
modules exactly as those suites already do (sibling test modules are importable
because pytest runs with ``ai/research-engine`` as rootdir).
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import httpx

from app.fact_precedence import FactSourceTier, FinancialFact, FinancialFactKey
from app.models import (
    EventImpact,
    ProvenancedValue,
    ResearchEvent,
    ResearchEventType,
    ResearchLifecycleStatus,
    ReliabilityLevel,
    SourceClassification,
    SourceMode,
    SourceType,
    TimeHorizon,
)
from app.portfolio_orchestration import _market_fundamentals_from_record
from app.repository import ResearchRepository
from app.research_readiness import (
    ResearchReadinessService,
    ResearchRequirementRegistry,
    ResearchRequirementStatus,
)
from app.research_readiness_runtime import RepositoryResearchReadinessAdapter
from app.settings import Settings
from app.source_discovery import DiscoveryResult, OfficialFilingDiscovery
from app.structured_research import financial_statement_history_from_facts

from test_official_nse_financial_parsing import JUNE, _document, _profile, _trusted_source
from test_di15_financial_authority_upgrade import _facts, _repo, _seed, _stored
from test_di12a_quarterly_financial_readiness_integrity import (
    INSTRUMENT_ID as CASTROL_INSTRUMENT_ID,
    NOW as DI12A_NOW,
    _Repo as _Di12aRepo,
    _adapter as _di12a_adapter,
    _profile as _castrol_profile,
)


NOW = datetime.now(timezone.utc)


def _major_contract_event(profile) -> ResearchEvent:
    event_at = DI12A_NOW - timedelta(days=2)
    return ResearchEvent(
        instrument_id=profile.instrument_id,
        company_id=profile.company_id,
        event_type=ResearchEventType.MAJOR_CONTRACT,
        event_date=event_at,
        detected_at=event_at,
        title="Major contract award Q2 2026",
        summary="Board awarded a major multi-year supply contract.",
        source_document_id=__import__("uuid").uuid4(),
        source_url="https://www.nseindia.com/corporate/castrol-contract.pdf",
        source_type=SourceType.NEWS,
        source_classification=SourceClassification.REPUTABLE_NEWS,
        reliability=ReliabilityLevel.LEVEL_B,
        source_mode=SourceMode.REAL,
        confidence=0.85,
        impact=EventImpact.POSITIVE,
        time_horizon=TimeHorizon.MEDIUM_TERM,
        status=ResearchLifecycleStatus.VALIDATED,
        raw_evidence_reference="Major contract award Q2 2026",
        published_at=event_at,
        retrieved_at=event_at,
    )


# --------------------------------------------------------------------------------------
# Correction 1: a reporting basis of UNKNOWN/None must not collapse into an explicit
# CONSOLIDATED basis for the same period/metric series.
# --------------------------------------------------------------------------------------
def test_reporting_basis_unknown_not_collapsed_to_consolidated() -> None:
    repo, profile = _repo()
    official = _facts(repo, official=True)
    assert official, "JUNE fixture must yield official NSE facts"
    # JUNE text carries no "consolidated"/"standalone" qualifier -> unknown basis.
    assert all(fact.key.reporting_basis is None for fact in official)

    consolidated = [
        replace(
            fact,
            key=replace(fact.key, reporting_basis="CONSOLIDATED"),
            value=fact.value.model_copy(update={"value": Decimal("99999")}),
        )
        for fact in official
    ]
    _seed(repo, official + consolidated)

    persisted = _stored(repo.financial_facts_for(profile.instrument_id))
    # Unknown-basis and explicit CONSOLIDATED rows survive persistence as distinct
    # keys (period/type/basis/metric) -- they are not collapsed into one series.
    assert len(persisted) == len(official) + len(consolidated)

    history = financial_statement_history_from_facts(
        list(persisted.values()),
        period_type={"QUARTERLY"},
        metrics={"revenue"},
        limit=4,
    )
    assert history, "expected a projected QUARTERLY revenue history"
    latest = history[0]
    assert latest.reporting_basis == "CONSOLIDATED"
    # The explicit CONSOLIDATED value is projected, proving the unknown-basis fact
    # did NOT satisfy (collapse into) the CONSOLIDATED series.
    assert latest.metrics["revenue"].value == Decimal("99999")


# --------------------------------------------------------------------------------------
# Correction 2: an NSE discovery that fetches a filing but parses ZERO official facts
# is SUCCESS_EMPTY -- it must not claim QUARTERLY_FINANCIALS readiness. The secondary
# Yahoo fallback remains untouched and authoritative NSE is never claimed.
# --------------------------------------------------------------------------------------
def test_di18_nse_success_empty_does_not_claim_nse_readiness() -> None:
    repo, profile = _repo()

    official_source = _trusted_source(profile)

    async def discover(_profile, _categories, _seen):
        # A real FINANCIAL_RESULTS candidate exists on NSE archives, but ...
        return [DiscoveryResult("FINANCIAL_RESULTS", official_source)]

    async def stub_fetch(_profile, filings, seen_urls):
        # ... the announcement carries no parseable numerical table -> 0 facts.
        document = _document(
            "Official financial results announcement, but no valid numerical table."
        )
        document = document.model_copy(update={
            "instrument_id": profile.instrument_id,
            "company_id": profile.company_id,
            "canonical_url": official_source.url,
            "original_url": official_source.url,
        })
        repo.documents[document.document_id] = document
        repo._reconcile_persisted_official_financial_document(document)
        return True

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("authority-upgrade path must not invoke broad discovery")

    repo._official_filing_discovery.discover = discover
    repo._fetch_official_filings = stub_fetch
    repo._fetch_registered_source = forbidden

    asyncio.run(repo._refresh_live(profile.instrument_id, {"FINANCIAL_RESULTS"}))

    # NSE found a filing but extracted zero OFFICIAL_NSE facts -> SUCCESS_EMPTY.
    observations = repo.acquisition_observations_for(profile.instrument_id)
    quarterly_obs = [
        obs
        for obs in observations
        if obs["requirement_id"] == "QUARTERLY_FINANCIALS" and obs["provider"] == "NSE"
    ]
    assert quarterly_obs, "expected a QUARTERLY_FINANCIALS acquisition observation"
    nse_obs = quarterly_obs[-1]
    assert nse_obs["outcome"] == "SUCCESS_EMPTY"
    assert nse_obs["evidence_count"] == 0
    assert nse_obs["failure_reason"] is None

    # No facts were persisted at all, so readiness must be MISSING with no source --
    # NSE NEVER claimed authoritative readiness on an empty parse.
    adapter = RepositoryResearchReadinessAdapter(repo)
    result = ResearchReadinessService(adapter).assess(
        profile.instrument_id, jurisdiction="INDIA"
    )
    readiness = result.for_requirement("QUARTERLY_FINANCIALS")
    assert readiness.status == ResearchRequirementStatus.MISSING
    assert readiness.source is None

    # The Yahoo fallback is independent: seeding it preserves its provenance and the
    # readiness source is never the (empty) NSE acquisition.
    _seed(repo, _facts(repo))
    after = _stored(repo.financial_facts_for(profile.instrument_id))
    assert after == _stored(_facts(repo))
    assert not any(fact.source_tier == FactSourceTier.OFFICIAL_NSE for fact in after.values())
    fallback = ResearchReadinessService(adapter).assess(
        profile.instrument_id, jurisdiction="INDIA"
    )
    assert fallback.for_requirement("QUARTERLY_FINANCIALS").source != "NSE"


# --------------------------------------------------------------------------------------
# Correction 3: a bounded official-filing fetch timeout must not propagate, must mark
# the host transport failure (bounding further retries), and must leave the Yahoo
# fallback intact as the QUARTERLY_FINANCIALS source.
# --------------------------------------------------------------------------------------
def test_official_filing_fetch_timeout_is_bounded_and_fallback_retained() -> None:
    repo, profile = _repo()
    # Seed a Yahoo fallback so readiness survives the NSE fetch timeout.
    _seed(repo, _facts(repo))
    before = _stored(repo.financial_facts_for(profile.instrument_id))

    nse_source = _trusted_source(profile)
    # Two discovery results from the SAME host: the first times out and the second
    # must be skipped by the host transport-failure budget, not retried.
    filings = [
        DiscoveryResult("FINANCIAL_RESULTS", nse_source),
        DiscoveryResult("FINANCIAL_RESULTS", nse_source),
    ]
    attempted_hosts: list[str] = []

    async def boom(_profile, source):
        attempted_hosts.append(source.url)
        raise asyncio.TimeoutError

    repo._single_flight_official_filing = boom

    result = asyncio.run(repo._fetch_official_filings(profile, filings, set()))

    assert result is False  # completed_without_failure == False
    assert repo.last_live_error[profile.instrument_id].startswith(
        "OFFICIAL_FILING_FETCH_FAILED:"
    )
    # Host transport-failure budget bounded the retry: only the first same-host
    # filing was attempted.
    assert len(attempted_hosts) == 1

    # Yahoo fallback is untouched and remains the readiness source (never NSE).
    after = _stored(repo.financial_facts_for(profile.instrument_id))
    assert after == before
    adapter = RepositoryResearchReadinessAdapter(repo)
    readiness = ResearchReadinessService(adapter).assess(
        profile.instrument_id, jurisdiction="INDIA"
    )
    assert readiness.for_requirement("QUARTERLY_FINANCIALS").source != "NSE"


# --------------------------------------------------------------------------------------
# Correction 5 (production fix): the OCF summary adapter resolves operating_cash_flow
# from an NSE fact under the alternate metric name (cash_flow_from_operating_activities)
# when the Yahoo snapshot lacks operatingCashFlow, while a present Yahoo value wins.
# --------------------------------------------------------------------------------------
def _yahoo_snapshot_record(*, has_operating_cash_flow: bool) -> SimpleNamespace:
    facts: dict[str, ProvenancedValue] = {
        "marketCap": ProvenancedValue(
            value=Decimal("8261"),
            unit="INR crore",
            as_of_date=NOW,
            source_url="https://finance.yahoo.com/quote/EXAMPLE.NS",
            source_name="Yahoo Finance",
            source_type="STRUCTURED_MARKET_PROVIDER",
            published_at=NOW,
            retrieved_at=NOW,
            confidence=0.9,
        ),
    }
    if has_operating_cash_flow:
        facts["operatingCashFlow"] = ProvenancedValue(
            value=Decimal("123"),
            unit="INR crore",
            as_of_date=NOW,
            source_url="https://finance.yahoo.com/quote/EXAMPLE.NS",
            source_name="Yahoo Finance",
            source_type="STRUCTURED_MARKET_PROVIDER",
            published_at=NOW,
            retrieved_at=NOW,
            confidence=0.9,
        )
    return SimpleNamespace(
        snapshot=SimpleNamespace(
            facts=facts,
            resolution=SimpleNamespace(company_name="Example Ltd."),
        ),
        provider="YAHOO_FINANCE",
        provider_instrument_id="EXAMPLE",
        market_as_of=NOW,
        retrieved_at=NOW,
        last_fundamentals_at=None,
        last_valuation_at=None,
    )


def _nse_operating_cash_flow_fact(instrument_id) -> FinancialFact:
    return FinancialFact(
        FinancialFactKey(
            instrument_id,
            "cash_flow_from_operating_activities",
            "2026-06-30",
            "QUARTERLY",
            None,
        ),
        ProvenancedValue(
            value=Decimal("500"),
            unit="INR crore",
            as_of_date=datetime(2026, 6, 30, tzinfo=timezone.utc),
            source_url="https://nsearchives.nseindia.com/corporate/result.pdf",
            source_name="NSE",
            source_type="EXCHANGE_ANNOUNCEMENT",
            published_at=NOW - timedelta(days=3),
            retrieved_at=NOW - timedelta(days=1),
            confidence=0.98,
        ),
        FactSourceTier.OFFICIAL_NSE,
        "NSE",
        "nse-cash-flow-from-operating-activities-2026-06-30",
        SourceMode.REAL,
    )


def test_summary_adapter_resolves_nse_ocf_via_alternate_metric_name() -> None:
    profile = _castrol_profile()
    nse_ocf = _nse_operating_cash_flow_fact(profile.instrument_id)

    # Yahoo snapshot lacks operatingCashFlow -> OCF backfilled from the NSE fact
    # (under the alternate metric name cash_flow_from_operating_activities).
    fundamentals = _market_fundamentals_from_record(
        _yahoo_snapshot_record(has_operating_cash_flow=False),
        NOW,
        Settings(),
        financial_facts=[nse_ocf],
    )
    assert fundamentals is not None
    assert fundamentals.operating_cash_flow == Decimal("500")

    # A present Yahoo operatingCashFlow must win over the NSE backfill.
    with_yahoo = _market_fundamentals_from_record(
        _yahoo_snapshot_record(has_operating_cash_flow=True),
        NOW,
        Settings(),
        financial_facts=[nse_ocf],
    )
    assert with_yahoo is not None
    assert with_yahoo.operating_cash_flow == Decimal("123")


def _yahoo_stale_operating_cash_flow_fact(instrument_id, value: Decimal) -> FinancialFact:
    """A *stale* Yahoo secondary OCF fact (basis "UNKNOWN", tier YAHOO) persisted
    from an earlier refresh, coexisting with an authoritative NSE cash-flow fact.
    """
    return FinancialFact(
        FinancialFactKey(
            instrument_id,
            "operating_cash_flow",
            "2026-06-30",
            "QUARTERLY",
            "UNKNOWN",
        ),
        ProvenancedValue(
            value=value,
            unit="INR crore",
            as_of_date=datetime(2026, 6, 30, tzinfo=timezone.utc),
            source_url="https://finance.yahoo.com/quote/EXAMPLE.NS",
            source_name="Yahoo Finance",
            source_type="STRUCTURED_MARKET_PROVIDER",
            published_at=NOW - timedelta(days=10),
            retrieved_at=NOW - timedelta(days=9),
            confidence=0.9,
        ),
        FactSourceTier.YAHOO,
        "YAHOO_FINANCE",
        f"EXAMPLE:operating_cash_flow:2026-06-30:QUARTERLY",
        SourceMode.REAL,
    )


def test_summary_adapter_nse_ocf_outranks_stale_yahoo_ocf_same_period() -> None:
    """limit=1 must not select a lower-authority Yahoo OCF fact when a compatible
    authoritative NSE OCF fact exists for the same period.

    NSE persists cash_flow_from_operating_activities (tier OFFICIAL_NSE=4, basis
    None/CONSOLIDATED/STANDALONE); Yahoo secondary facts persist operating_cash_flow
    (tier YAHOO=2, basis "UNKNOWN"). The two therefore land in distinct basis
    buckets, and ``basis_rank`` (ordered by max authority) always selects the NSE
    basis bucket -- so the Yahoo slot is excluded before the metric-precedence
    lookup, and the authoritative NSE value wins.
    """
    profile = _castrol_profile()
    nse_ocf = _nse_operating_cash_flow_fact(profile.instrument_id)
    yahoo_ocf = _yahoo_stale_operating_cash_flow_fact(
        profile.instrument_id, Decimal("123")
    )

    # Current Yahoo snapshot lacks operatingCashFlow -> backfill triggers.
    fundamentals = _market_fundamentals_from_record(
        _yahoo_snapshot_record(has_operating_cash_flow=False),
        NOW,
        Settings(),
        financial_facts=[nse_ocf, yahoo_ocf],
    )
    assert fundamentals is not None
    # NSE (authority 4) wins over stale Yahoo (authority 2): no accidental
    # lower-authority selection.
    assert fundamentals.operating_cash_flow == Decimal("500")


# --------------------------------------------------------------------------------------
# Correction 6: an ORDER_BOOK event (MAJOR_CONTRACT) is retained as evidence for
# ORDER_BOOK_CAPEX_GUIDANCE, but CAPEX / MANAGEMENT_GUIDANCE remain missing -> not READY.
# --------------------------------------------------------------------------------------
def test_order_book_evidence_retained_while_capex_and_guidance_remain_missing() -> None:
    profile = _castrol_profile()
    repo = _Di12aRepo(profile, events=[_major_contract_event(profile)])
    adapter = _di12a_adapter(repo)

    snapshot = adapter.load_by_global_instrument_id(
        profile.instrument_id, ResearchRequirementRegistry.default().requirements
    )
    evidence = snapshot.evidence_for("ORDER_BOOK_CAPEX_GUIDANCE")
    assert evidence, "MAJOR_CONTRACT event must populate ORDER_BOOK_CAPEX_GUIDANCE evidence"

    covered = {
        input_id for item in evidence for input_id in item.covered_input_ids
    }
    assert "ORDER_BOOK_OR_MAJOR_CONTRACT" in covered
    assert "MATERIAL_CATALYST_EVIDENCE" in covered
    # A single order event does not satisfy CAPEX or guidance inputs.
    assert "CAPACITY_OR_CAPEX_OR_COMMISSIONING" not in covered
    assert "MANAGEMENT_GUIDANCE" not in covered

    result = ResearchReadinessService(adapter).assess(
        profile.instrument_id, jurisdiction="INDIA", now=DI12A_NOW
    )
    readiness = result.for_requirement("ORDER_BOOK_CAPEX_GUIDANCE")
    # Correction 6 locks the ORDER_BOOK_CAPACITY_CATALYSTS dispatch unchanged: a
    # single order event retains ORDER_BOOK evidence (the mandatory catalyst)
    # while CAPEX / MANAGEMENT_GUIDANCE concepts remain MISSING. Order-book
    # evidence alone is sufficient for the requirement to be non-missing, so the
    # overall status is READY (not MISSING) -- the dispatch is the invariant, not
    # a suppression of order-book coverage.
    assert readiness.status != ResearchRequirementStatus.MISSING
    # Full-research contract: CAPEX is a required concept input unless explicitly
    # NOT_APPLICABLE, so order-book evidence alone is PARTIAL (not READY, not MISSING).
    assert readiness.status == ResearchRequirementStatus.PARTIAL
    assert readiness.missing_reason == "MISSING_REQUIRED_INPUTS:CAPACITY_OR_CAPEX_OR_COMMISSIONING"
    assert readiness.concept_evidence_states["ORDER_BOOK"] in (
        "READY_FRESH",
        "READY_STALE",
    )
    assert readiness.concept_evidence_states["CAPEX"] == "MISSING"
    assert readiness.concept_evidence_states["GUIDANCE"] == "MISSING"
    assert "CAPACITY_OR_CAPEX_OR_COMMISSIONING" in readiness.missing_input_ids
    assert "MANAGEMENT_GUIDANCE" in readiness.missing_input_ids


# --------------------------------------------------------------------------------------
# DI-18 Blockers #1: the FIRST boundary at which authoritative NSE financial evidence
# can become empty is discovery-level announcement-title classification. An NSE results
# announcement whose title/desc carries no financial-results phrase (and no subtype
# keyword) is filtered out by ``OfficialFilingDiscovery`` before any document is
# fetched, so ``official_filings`` is empty, ``official_count`` is 0, and the
# acquisition is recorded as ``SUCCESS_EMPTY`` -- never a false NSE readiness claim.
# This uses ``httpx.MockTransport`` (no live network) and does NOT loosen instrument
# identity matching (the ISIN/symbol filter inside discover is unchanged).
# --------------------------------------------------------------------------------------
# A genuine non-financial announcement: no period-cadence indicator, no financial-
# content indicator, no subtype keyword — so it is NOT classified as FINANCIAL_RESULTS
# even by the broadened semantic classifier.  (Titles like "Quarterly Results" now
# DO classify via the period+content co-occurrence rule, so they are no longer a
# valid "unclassified" fixture.)
_FALLBACK_UNCLASSIFIED_ANNOUNCEMENT_TITLE = "Share Transfer Approval"


def _nse_announcement_rows(title: str) -> list[dict]:
    return [
        {
            "symbol": "EXAMPLE",
            "isin": "INE000A01010",
            "desc": title,
            "attchmntText": title,
            "attchmntFile": "https://nsearchives.nseindia.com/corporate/results.pdf",
            "an_dt": "01-Aug-2026 10:00:00",
        }
    ]


def test_di18_discovery_unclassified_title_yields_success_empty() -> None:
    repo, profile = _repo()
    rows = _nse_announcement_rows(_FALLBACK_UNCLASSIFIED_ANNOUNCEMENT_TITLE)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["symbol"] == "EXAMPLE"
        return httpx.Response(200, json=rows, request=request)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            # Real discovery runs against the faked NSE announcements API -- no
            # network, no identity-matcher changes.
            repo._official_filing_discovery = OfficialFilingDiscovery(client=client)

            async def forbidden(*_args, **_kwargs):
                raise AssertionError(
                    "authority-upgrade path must not invoke non-authoritative sources"
                )

            repo._fetch_registered_source = forbidden

            # Narrow proof AT the discovery boundary: the announcement row exists on
            # NSE archives, but its title does not classify as FINANCIAL_RESULTS.
            discovered = await repo._official_filing_discovery.discover(
                profile, {"FINANCIAL_RESULTS"}, set()
            )
            assert discovered == []

            await repo._refresh_live(profile.instrument_id, {"FINANCIAL_RESULTS"})

    asyncio.run(scenario())

    # discover yielded no FINANCIAL_RESULTS filing -> official_filings == [] ->
    # official_count == 0 -> SUCCESS_EMPTY (not FAILED, not SUCCESS).
    observations = repo.acquisition_observations_for(profile.instrument_id)
    quarterly_obs = [
        obs
        for obs in observations
        if obs["requirement_id"] == "QUARTERLY_FINANCIALS" and obs["provider"] == "NSE"
    ]
    assert quarterly_obs, "expected a QUARTERLY_FINANCIALS acquisition observation"
    nse_obs = quarterly_obs[-1]
    assert nse_obs["outcome"] == "SUCCESS_EMPTY"
    assert nse_obs["evidence_count"] == 0
    assert nse_obs["failure_reason"] is None

    # No official facts and no fetch failure -> readiness stays MISSING, no source:
    # NSE never claimed authoritative readiness on an empty discovery.
    adapter = RepositoryResearchReadinessAdapter(repo)
    readiness = ResearchReadinessService(adapter).assess(
        profile.instrument_id, jurisdiction="INDIA"
    )
    fin = readiness.for_requirement("QUARTERLY_FINANCIALS")
    assert fin.status == ResearchRequirementStatus.MISSING
    assert fin.source is None
