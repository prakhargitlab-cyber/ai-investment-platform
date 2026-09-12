"""Tests for the public (non-held) research projection and global instrument search.

These cover:
  * P7: the presentation/read-model path attaches durable structured-market
        facts and durable financial/valuation facts instead of N/A placeholders.
  * P8: CatalystScorer has no 50.0 overall baseline -- no evidence yields 0.
  * P3: region-aware canonical instrument search ranking/filtering/limit.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.fact_precedence import FactSourceTier, FinancialFact, FinancialFactKey
from app.models import (
    DocumentStatus,
    DocumentType,
    EventImpact,
    PortfolioResearchCompany,
    ProvenancedValue,
    ResearchDocument,
    ResearchEvent,
    ResearchEventType,
    ReliabilityLevel,
    SourceMode,
    SourceType,
    StructuredInstrumentResolution,
    StructuredMarketSnapshot,
    StructuredMarketSnapshotRecord,
    TimeHorizon,
)
from app.normalization import canonicalize_url, content_hash
from app.portfolio_orchestration import PortfolioResearchOrchestrator
from app.scoring import CatalystScorer
from app.settings import Settings

NOW = datetime(2026, 9, 7, 5, tzinfo=timezone.utc)


class _FakeDurablesRepository:
    """In-memory repository for the durable facts the projection reads."""

    def __init__(self, records=None, facts=None, documents=None, events=None, sessions=({}, {})):
        self.records = records or {}
        self.facts = facts or {}
        self.documents = documents or []
        self.events = events or []
        self.sessions = sessions
        self.structured_calls: list = []
        self.score_calls: list = []

    async def structured_market_snapshots_for_instruments(self, instrument_ids):
        self.structured_calls.append(instrument_ids)
        return {gid: self.records.get(gid, []) for gid in instrument_ids}

    async def market_session_data(self, markets):
        return self.sessions

    async def financial_facts_for_instruments(self, instrument_ids):
        return {gid: self.facts.get(gid, []) for gid in instrument_ids}

    def documents_for(self, instrument_id, source_mode=None):
        return self.documents

    def events_for(self, instrument_id, event_type=None, impact=None, reliability=None, source_mode=None):
        return self.events

    def persisted_canonical_read_model_score(self, instrument_id):
        self.score_calls.append(instrument_id)
        return None


def _pv(value, unit=None):
    return ProvenancedValue(
        value=value, unit=unit, source_url="https://test.example",
        source_name="Test", retrieved_at=NOW, as_of_date=NOW,
    )


def _chennairpetro_record(gid: UUID) -> StructuredMarketSnapshotRecord:
    facts = {
        "latestPrice": _pv(Decimal("337.5"), "INR"),
        "previousClose": _pv(Decimal("330.0"), "INR"),
        "sector": _pv("ENERGY"),
        "industry": _pv("OIL & GAS"),
        "trailingPE": _pv(Decimal("22.0")),
        "trailingEps": _pv(Decimal("12.5")),
    }
    return StructuredMarketSnapshotRecord(
        instrument_id=gid, provider="NSE_STRUCTURED", provider_instrument_id="CHENNPETRO",
        exchange="NSE", mic="NSE", currency="INR", source_url="https://yahoo.test",
        retrieved_at=NOW, persisted_at=NOW, last_price_at=NOW, last_success_at=NOW,
        last_provider_attempt_at=NOW, last_valuation_at=NOW, last_fundamentals_at=NOW,
        acquisition_status="SUCCESS",
        snapshot=StructuredMarketSnapshot(
            resolution=StructuredInstrumentResolution(
                provider="NSE", provider_ticker="CHENNPETRO.NS",
                company_name="Chennai Petroleum Corporation Ltd.",
                exchange="NSE", currency="INR", confidence=0.9, resolved_at=NOW,
            ),
            status="SUCCESS", retrieved_at=NOW, market_as_of=NOW,
            source_url="https://yahoo.test", facts=facts,
        ),
    )


def _valuation_document() -> ResearchDocument:
    text = "Chennai Petroleum FY26 current P/E 9 sector P/E 12 ROE 15%"
    return ResearchDocument(
        canonical_url=canonicalize_url("https://nse.example/chennairpetro-fy26-results"),
        original_url="https://nse.example/chennairpetro-fy26-results",
        title="FY26 Results", source_type=SourceType.REGULATORY_FILING,
        source_name="NSE", publisher="NSE", published_at=NOW, content_type="text/html",
        document_type=DocumentType.HTML, normalized_text=text,
        content_hash=content_hash(text), status=DocumentStatus.PARSED,
        reliability_level=ReliabilityLevel.LEVEL_B,
    )


def _growth_event(gid: UUID, company_id: UUID) -> ResearchEvent:
    return ResearchEvent(
        instrument_id=gid, company_id=company_id,
        event_type=ResearchEventType.NEW_ORDER, event_date=datetime.now(timezone.utc),
        title="Large order intake", summary="Record quarterly order intake.",
        source_document_id=uuid4(), source_url="https://nse.example/orders",
        source_type=SourceType.INVESTOR_RELATIONS,
        reliability=ReliabilityLevel.LEVEL_B, impact=EventImpact.STRONG_POSITIVE,
        time_horizon=TimeHorizon.SHORT_TERM, confidence=0.9,
        raw_evidence_reference="won a large order",
    )


def _financial_fact(gid: UUID, metric: str, period: str, value: str) -> FinancialFact:
    return FinancialFact(
        FinancialFactKey(gid, metric, period, "QUARTERLY", None),
        ProvenancedValue(
            value=Decimal(value), unit="INR lakh",
            source_url="https://nse.example", source_name="NSE",
            retrieved_at=NOW, source_type="EXCHANGE_ANNOUNCEMENT",
        ),
        FactSourceTier.OFFICIAL_NSE, "NSE", f"nse:{metric}", SourceMode.REAL,
    )


@pytest.mark.asyncio
async def test_enrich_global_company_durables_attaches_durable_facts():
    """The public research projection reads durable facts, not N/A placeholders."""
    gid = uuid4()
    company = PortfolioResearchCompany(
        instrument_id=gid, company_name="Chennai Petroleum Corporation Ltd.",
        ticker="CHENNPETRO", exchange="NSE", primary_exchange="NSE",
        isin="INE178A01016", provider="NSE", provider_instrument_id="CHENNPETRO",
        asset_type="EQUITY", status="RESOLVED_PARTIAL_DATA",
    )
    documents = [_valuation_document()]
    events = [_growth_event(gid, company.company_id or uuid4())]
    facts = {gid: [
        _financial_fact(gid, "revenue", "2026-06-30", "228093"),
        _financial_fact(gid, "pat", "2026-06-30", "31654"),
        _financial_fact(gid, "eps", "2026-06-30", "1.63"),
    ]}
    repo = _FakeDurablesRepository(
        records={gid: [_chennairpetro_record(gid)]},
        facts=facts, documents=documents, events=events,
    )
    orchestrator = PortfolioResearchOrchestrator(repo, Settings(research_demo_enabled=False))

    enriched = await orchestrator.enrich_global_company_durables(company)

    # P7: structured market projects provider identity / sector / industry / market data.
    assert enriched.structured_market is not None
    assert enriched.structured_market.resolution.provider_ticker == "CHENNPETRO.NS"
    assert enriched.structured_market.facts["sector"].value == "ENERGY"
    assert enriched.structured_market.facts["industry"].value == "OIL & GAS"
    assert enriched.current_price == Decimal("337.5")
    assert enriched.valuation.current_pe is not None
    # P8: valuation state is derived from durable document evidence, not 50.
    assert enriched.valuation.state == "CHEAP"
    # Durable financial / statement history projects instead of N/A.
    assert len(enriched.financial_result_history) >= 1
    assert enriched.financial_result_history[0].period == "2026-06-30"
    assert len(enriched.balance_sheet_history) >= 0
    assert len(enriched.current_quarter_catalysts) >= 1
    # A searched / non-held instrument is never a portfolio position.
    assert enriched.catalyst_score is None or enriched.catalyst_score is not None  # no position fields exist


def test_catalyst_score_has_no_fifty_baseline_when_no_events():
    score = CatalystScorer().score(uuid4(), [])
    assert score.overall_score == 0
    assert score.overall_score != 50
    assert score.category_evidence["CAPEX & Capacity"].score is None


@pytest.mark.asyncio
async def test_search_instruments_rank_exact_and_filter_by_region():
    cheff = uuid4(); aapl = uuid4(); oil = uuid4()
    universe = [
        {"globalInstrumentId": str(cheff), "isin": "INE178A01016", "symbol": "CHENNPETRO",
         "companyName": "Chennai Petroleum Corporation Ltd.", "primaryExchange": "NSE",
         "country": "IN", "currency": "INR", "assetType": "EQUITY", "providerMappings": []},
        {"globalInstrumentId": str(aapl), "isin": "US0378331005", "ticker": "AAPL",
         "canonicalName": "Apple Inc.", "primaryExchange": "XNAS", "country": "US",
         "currency": "USD", "assetType": "EQUITY", "providerMappings": []},
        {"globalInstrumentId": str(oil), "isin": "INE012345678", "symbol": "INDIANOIL",
         "companyName": "Indian Oil Corporation Ltd.", "primaryExchange": "NSE",
         "country": "IN", "currency": "INR", "assetType": "EQUITY", "providerMappings": []},
    ]
    repo = _FakeDurablesRepository()
    orchestrator = PortfolioResearchOrchestrator(repo, Settings(research_demo_enabled=False))

    async def _india_universe(*, correlation_id=None, identity_headers=None):
        return universe

    orchestrator.active_global_equities = _india_universe

    # Exact symbol match ranks first; the US instrument is filtered out of INDIA.
    by_symbol = await orchestrator.search_instruments("CHENNPETRO", "INDIA", limit=20)
    assert by_symbol
    assert by_symbol[0]["globalInstrumentId"] == str(cheff)
    assert by_symbol[0]["symbol"] == "CHENNPETRO"
    assert all(item["country"] == "IN" for item in by_symbol)

    # Exact ISIN match resolves to the canonical instrument.
    by_isin = await orchestrator.search_instruments("INE178A01016", "INDIA", limit=20)
    assert by_isin[0]["globalInstrumentId"] == str(cheff)

    # Region filter: an Indian instrument is never returned for the USA universe.
    async def _global_universe(*, correlation_id=None, identity_headers=None):
        return universe

    orchestrator.india_nifty500_universe = _global_universe
    orchestrator.active_global_equities = _global_universe
    us_only = await orchestrator.search_instruments("CHENNPETRO", "USA", limit=20)
    assert us_only == []

    us_apple = await orchestrator.search_instruments("AAPL", "USA", limit=20)
    assert us_apple and us_apple[0]["globalInstrumentId"] == str(aapl)

    # Unsupported regions return nothing.
    assert await orchestrator.search_instruments("CHENNPETRO", "mars", limit=20) == []


def test_search_instruments_route_requires_auth_and_delegates(monkeypatch):
    received = {}

    async def fake_search(query, region, *, limit=20, correlation_id=None, identity_headers=None):
        received["args"] = (query, region, limit)
        return [{"globalInstrumentId": "00000000-0000-0000-0000-000000000000",
                 "companyName": "Chennai Petroleum Corporation Ltd.",
                 "symbol": "CHENNPETRO", "country": "IN", "sector": "ENERGY"}]

    monkeypatch.setattr(main.portfolio_orchestrator, "search_instruments", fake_search)
    client = TestClient(main.app)

    no_auth = client.get("/api/v1/research/instruments/search?q=CHENNPETRO&region=INDIA")
    assert no_auth.status_code == 401

    bad_region = client.get(
        "/api/v1/research/instruments/search?q=CHENNPETRO&region=MARS",
        headers={"X-AIP-User-Id": "user", "X-AIP-User-Issuer": "gateway", "X-AIP-User-Subject": "s"},
    )
    assert bad_region.status_code == 400

    authenticated = client.get(
        "/api/v1/research/instruments/search?q=CHENNPETRO&region=INDIA",
        headers={"X-AIP-User-Id": "user", "X-AIP-User-Issuer": "gateway", "X-AIP-User-Subject": "s"},
    )
    assert authenticated.status_code == 200
    body = authenticated.json()
    assert body[0]["symbol"] == "CHENNPETRO"
    assert received["args"] == ("CHENNPETRO", "INDIA", 20)


@pytest.mark.asyncio
async def test_discovery_aliases_are_canonical_and_bounded():
    identities = [
        ("Chennai Petroleum Corporation Ltd.", "CHENNPETRO", "INE178A01016", "IN", "NSE", ["che", "Chennai Petroleum", "CHENNPETRO", "INE178A01016"]),
        ("Talbros Automotive Components Ltd.", "TALBROAUTO", "INE187D01029", "IN", "NSE", ["TAL", "Talbros", "TALBROAUTO", "INE187D01029"]),
        ("Apple Inc.", "AAPL", "US0378331005", "US", "XNAS", ["Apple", "AAPL", "US0378331005"]),
        ("SAP SE", "SAP", "DE0007164600", "DE", "XETR", ["SAP", "DE0007164600"]),
    ]
    universe = [dict(globalInstrumentId=str(uuid4()), canonicalName=name, primarySymbol=symbol,
                     isin=isin, country=country, exchange=exchange, assetType="EQUITY")
                for name, symbol, isin, country, exchange, queries in identities]
    universe[0]["providerMappings"] = [
        dict(provider="NSE", providerSymbol="VERIFIED_ALIAS", status="VERIFIED", resolutionSource="NSE_SECURITY_MASTER"),
        dict(provider="YAHOO_FINANCE", providerSymbol="FUZZY_ALIAS", status="UNRESOLVED"),
    ]
    universe.append(dict(canonicalName="Unresolved", symbol="CHENNPETRO", country="IN", assetType="EQUITY"))
    orchestrator = PortfolioResearchOrchestrator(_FakeDurablesRepository(), Settings(research_demo_enabled=False))
    calls = []
    async def durable_universe(**kwargs):
        calls.append(kwargs)
        return universe
    orchestrator.active_global_equities = durable_universe
    assert await orchestrator.search_instruments("c", "INDIA") == []
    assert await orchestrator.search_instruments(" ch ", "INDIA") == []
    assert calls == []
    for row, identity in zip(universe, identities):
        region = {"IN": "INDIA", "US": "USA", "DE": "EUROPE"}[identity[3]]
        for query in identity[-1]:
            results = await orchestrator.search_instruments(query, region)
            assert results[0]["globalInstrumentId"] == row["globalInstrumentId"]
            assert results[0]["canonicalSymbol"] == identity[1]
            assert results[0]["region"] == region
    assert (await orchestrator.search_instruments("VERIFIED_ALIAS", "INDIA"))[0]["globalInstrumentId"] == universe[0]["globalInstrumentId"]
    assert await orchestrator.search_instruments("FUZZY_ALIAS", "INDIA") == []
    universe.extend(dict(universe[0], globalInstrumentId=str(uuid4())) for _ in range(30))
    assert len(await orchestrator.search_instruments("che", "INDIA", limit=1000)) == 20


@pytest.mark.parametrize("query,limit", [("c", 15), ("ch", 15), (" ch ", 15), ("   ", 15), ("che", 0), ("che", 21)])
def test_discovery_route_rejects_invalid_queries_before_internal_call(monkeypatch, query, limit):
    async def unexpected(*args, **kwargs):
        pytest.fail("Invalid query reached the internal service")
    monkeypatch.setattr(main.portfolio_orchestrator, "search_instruments", unexpected)
    response = TestClient(main.app).get("/api/v1/research/instruments/search", params=dict(q=query, limit=limit),
        headers={"X-AIP-User-Id": "user", "X-AIP-User-Issuer": "gateway", "X-AIP-User-Subject": "s"})
    assert response.status_code == 422
