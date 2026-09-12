from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import httpx
import pytest
import respx

from app.deduplication import DocumentDeduplicator
from app.entity_resolution import EntityResolver
from app.extraction import RuleBasedEventExtractor
from app.models import CompanyResearchProfile, DocumentStatus, DocumentSubtype, DocumentType, EntityResolution, EtfResearchProfile, EventImpact, PortfolioResearchCompany, PortfolioResearchSummary, ProvenancedValue, ReliabilityLevel, ResearchDocument, ResearchEvent, ResearchEventType, ShareholdingCategory, ShareholdingSnapshot, ShareholdingSnapshotValue, SourceClassification, SourceMode, SourceType, TimeHorizon
from app.fact_precedence import FactSourceTier, FinancialFact, FinancialFactKey
from app.normalization import canonicalize_url, content_hash, extract_published_at, extract_text, normalize_numbers
from app.repository import ResearchRepository, _InstrumentRefreshGate, _canonical_refresh_category
from app.persistence import SqliteResearchPersistence
from app.portfolio_orchestration import PortfolioResearchOrchestrator, _company_aliases, _hydrate_verified_exchange_mappings, _instrument_asset_type, _instrument_name
from app.research_fetching import DocumentSizeLimitExceeded, FetchError, FetchResult, HttpResearchFetcher, HttpStatusFetchError, NetworkFetchResult, PdfExtractionTimeoutError, RestrictedFetchError, TransportFetchError
from app.scoring import CatalystScorer, canonical_read_model_score
from app.settings import Settings
from app.source_discovery import (
    ApprovedSourceDiscovery,
    BraveCompatibleSearchDiscoveryProvider,
    CandidateSearchResult,
    DiscoveryResult,
    GoogleCompatibleSearchDiscoveryProvider,
    OfficialFilingDiscovery,
    SearchDateWindow,
    SearchDiscoveryService,
    SearchProviderConfigurationError,
    SearchProviderError,
    SearxngSearchDiscoveryProvider,
    classify_source,
    classify_nse_document_subtype,
    _candidate_rank,
    generate_search_queries,
)
from app.source_registry import AIXTRON_INSTRUMENT_ID, RegisteredResearchSource, registered_sources_for
import app.structured_research as structured_research
from app.structured_research import enrich_company_research, financial_result_history_from_facts, financial_statement_history_from_facts, latest_quarterly_result, latest_quarterly_result_from_facts, shareholding_changes, shareholding_changes_from_snapshots, source_diversity, valuation_assessment
from app.structured_market import StructuredProviderError
from app.url_security import UnsafeUrlError, validate_public_http_url


class _UnavailableStructuredProvider:
    provider_name = "test-structured"

    async def collect(self, instrument):
        raise StructuredProviderError("STRUCTURED_PROVIDER_UNAVAILABLE:FIXTURE")


@pytest.mark.asyncio
async def test_global_instrument_refresh_gate_reuses_fresh_complete_state_and_single_flights_stale_work(monkeypatch) -> None:
    repository = ResearchRepository(settings=Settings(research_live_enabled=True, research_demo_enabled=False))
    instrument_id = repository.profiles[0].instrument_id
    fresh = _InstrumentRefreshGate(set(), False, False, "FRESH_AND_COMPLETE")
    monkeypatch.setattr(repository, "_instrument_refresh_gate", lambda *_args, **_kwargs: fresh)
    provider_calls = 0

    async def should_not_discover(*_args, **_kwargs):
        nonlocal provider_calls
        provider_calls += 1

    monkeypatch.setattr(repository._official_filing_discovery, "discover", should_not_discover)
    monkeypatch.setattr(repository._discovery, "discover", should_not_discover)
    monkeypatch.setattr(repository, "_fetch_registered_source", should_not_discover)

    emitted: list[str] = []

    class _RepositoryLogCapture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            emitted.append(record.getMessage())

    repository_logger = logging.getLogger("app.repository")
    handler = _RepositoryLogCapture()
    previous_level = repository_logger.level
    repository_logger.setLevel(logging.INFO)
    repository_logger.addHandler(handler)
    try:
        await repository.refresh(instrument_id)
    finally:
        repository_logger.removeHandler(handler)
        repository_logger.setLevel(previous_level)
    assert provider_calls == 0
    assert any("research_refresh_gate" in message for message in emitted)
    assert any("outcome=REUSE_FRESH" in message for message in emitted)

    stale = _InstrumentRefreshGate({"FINANCIAL_RESULTS"}, False, False, "STALE")
    monkeypatch.setattr(repository, "_instrument_refresh_gate", lambda *_args, **_kwargs: stale)
    started = asyncio.Event()
    release = asyncio.Event()

    live_calls = 0

    async def refresh_once(*_args):
        nonlocal live_calls
        live_calls += 1
        started.set()
        await release.wait()

    monkeypatch.setattr(repository, "_refresh_live", refresh_once)
    first = asyncio.create_task(repository.refresh(instrument_id))
    await started.wait()
    second = asyncio.create_task(repository.refresh(instrument_id))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first, second)
    assert live_calls == 1


def test_url_canonicalization_and_hashing_are_deterministic() -> None:
    url = canonicalize_url("HTTPS://Example.COM/a//b/?utm_source=x&b=2&a=1#frag")
    assert url == "https://example.com/a/b?a=1&b=2"
    assert content_hash(" Hello   World ") == content_hash("hello world")


def test_ssrf_rejects_private_local_and_non_http_urls() -> None:
    for url in ["http://localhost/test", "http://127.0.0.1/test", "file:///etc/passwd", "http://169.254.169.254/latest"]:
        with pytest.raises(UnsafeUrlError):
            validate_public_http_url(url)


def test_numeric_normalization_supports_money_capacity_and_percentages() -> None:
    values = normalize_numbers("₹2,000 crore, €350 million, $1.2 billion, 73.15 MW, +42% backlog")
    assert any(v.currency == "INR" and v.value == Decimal("20000000000") for v in values)
    assert any(v.currency == "EUR" and v.value == Decimal("350000000") for v in values)
    assert any(v.currency == "USD" and v.value == Decimal("1200000000.0") for v in values)
    assert any(v.unit == "MW" and v.value == Decimal("73.15") for v in values)
    assert any(v.unit == "PERCENT" and v.value == Decimal("42") for v in values)


def test_published_date_extraction_supports_official_formats() -> None:
    assert extract_published_at("Herzogenrath, April 14, 2026") == datetime(2026, 4, 14, tzinfo=timezone.utc)
    assert extract_published_at("Veröffentlicht am 30.07.2026") == datetime(2026, 7, 30, tzinfo=timezone.utc)


def test_entity_resolution_uses_more_than_ticker() -> None:
    repo = ResearchRepository()
    resolver = EntityResolver(repo.profiles)
    resolution = resolver.resolve(
        "AIXTRON order",
        "AIXTRON SE AIXA XETR DE000A0WMPJ6 receives a new order.",
        "https://ir.aixtron.example/releases/order",
    )
    assert resolution.instrument_id == UUID("11111111-1111-1111-1111-111111111111")
    assert resolution.confidence > 0.8
    assert {"isin", "company_name", "known_domain"}.issubset(set(resolution.matched_on))


def test_entity_resolution_matches_besi_and_reliance_aliases() -> None:
    repo = ResearchRepository()
    resolver = EntityResolver(repo.profiles)

    besi = resolver.resolve(
        "BE Semiconductor Industries N.V. Announces Q2-26 Results",
        "Besi reported order growth for XAMS BESI holders.",
        "https://www.besi.com/investor-relations/press-releases/details/q2-results",
    )
    reliance = resolver.resolve(
        "RIL capex update",
        "Reliance Industries reported capex for RELIANCE XNSE INE002A01018.",
        "https://www.ril.com/ar2025-26/index.html",
    )

    assert besi.instrument_id == UUID("22222222-2222-2222-2222-222222222222")
    assert besi.confidence >= 0.30
    assert reliance.instrument_id == UUID("44444444-4444-4444-4444-444444444444")
    assert reliance.confidence >= 0.30


def test_document_deduplication_uses_url_and_hash() -> None:
    dedupe = DocumentDeduplicator()
    doc = _document("https://example.com/a", "same text")
    duplicate_url = _document("https://example.com/a", "different text")
    duplicate_hash = _document("https://example.com/b", "same text")
    assert dedupe.add(doc) is None
    assert dedupe.add(duplicate_url) == doc
    assert dedupe.add(duplicate_hash) == doc


def test_rule_based_extraction_sets_confidence_and_negative_events() -> None:
    doc = _document(
        "https://example.com/a",
        "AIXTRON SE AIXA XETR announced a new order worth €350 million. The factory ramp was delayed.",
    )
    doc.instrument_id = UUID("11111111-1111-1111-1111-111111111111")
    doc.company_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1")
    doc.entity_resolution_confidence = 0.9
    extractor = RuleBasedEventExtractor()

    events = extractor.extract(doc)

    assert any(event.event_type == "NEW_ORDER" and event.monetary_value == Decimal("350000000") for event in events)
    assert any(event.event_type == "PROJECT_DELAY" and event.impact == EventImpact.NEGATIVE for event in events)
    assert all(0 < event.confidence <= 1 for event in events)


def test_rule_based_extraction_classifies_capex_guidance_and_cancellations() -> None:
    doc = _document(
        "https://example.com/a",
        "AIXTRON SE AIXA XETR DE000A0WMPJ6 will invest EUR 120 million in CAPEX. "
        "The company raises guidance, later announced a guidance cut, and an order cancelled by a customer.",
    )
    doc.instrument_id = AIXTRON_INSTRUMENT_ID
    doc.company_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1")
    doc.entity_resolution_confidence = 1.0

    events = RuleBasedEventExtractor().extract(doc)

    assert any(event.event_type == "CAPEX" for event in events)
    assert any(event.event_type == "GUIDANCE_RAISED" and event.impact == EventImpact.POSITIVE for event in events)
    assert any(event.event_type == "GUIDANCE_CUT" and event.impact == EventImpact.NEGATIVE for event in events)
    assert any(event.event_type == "ORDER_CANCELLED" and event.impact == EventImpact.NEGATIVE for event in events)


def test_aixtron_extraction_uses_event_specific_fields_and_clean_text() -> None:
    title, text = extract_text(_aixtron_mojibake_fixture(), "text/html")
    assert title == "Strong momentum in optoelectronics continues"
    assert text is not None
    assert "Raised full-year 2026 guidance confirmed" in text
    assert "Navigation Suche" not in text
    assert not text.startswith("AIXTRON Press Information")

    doc = _document("https://www.aixtron.com/en/press/press-releases/strong", text)
    doc.title = title
    doc.instrument_id = AIXTRON_INSTRUMENT_ID
    doc.company_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1")
    doc.entity_resolution_confidence = 1.0

    events = RuleBasedEventExtractor().extract(doc)
    by_type = {str(event.event_type): event for event in events}

    assert "ANNUAL_REPORT" not in by_type
    assert "ORDER_BACKLOG_CHANGE" in by_type
    assert "GUIDANCE_MAINTAINED" in by_type
    assert "CAPACITY_EXPANSION" in by_type
    assert "EARNINGS_RELEASE" in by_type

    order = by_type["ORDER_BACKLOG_CHANGE"]
    assert order.monetary_original == "EUR 214.5 million"
    assert order.percentage_original in {"54%", "+81%"}
    assert order.customer is None
    assert order.counterparty is None
    assert "order intake" in order.raw_evidence_reference.lower()

    guidance = by_type["GUIDANCE_MAINTAINED"]
    assert guidance.monetary_original is None
    assert guidance.percentage_original is None
    assert guidance.customer is None
    assert guidance.counterparty is None
    assert "guidance" in guidance.raw_evidence_reference.lower()

    capacity = by_type["CAPACITY_EXPANSION"]
    assert capacity.monetary_original is None
    assert capacity.percentage_original is None
    assert capacity.customer is None
    assert capacity.counterparty is None
    assert "production capacity" in capacity.raw_evidence_reference.lower()

    earnings = by_type["EARNINGS_RELEASE"]
    assert "q2 results" in earnings.raw_evidence_reference.lower() or "half year results" in earnings.raw_evidence_reference.lower()


def test_catalyst_score_uses_negative_events_and_temporal_decay() -> None:
    repo = ResearchRepository()
    instrument_id = UUID("33333333-3333-3333-3333-333333333333")
    events = repo.events_for(instrument_id)
    score = CatalystScorer().score(instrument_id, events)
    assert 0 <= score.overall_score <= 100
    assert score.overall_score < 60


def test_category_no_evidence_is_not_score_50() -> None:
    repo = ResearchRepository()
    doc = _document(
        "https://www.aixtron.com/en/orders",
        "AIXTRON SE AIXA XETR DE000A0WMPJ6 recorded order intake of EUR 214.5 million (+54% yoy).",
    )
    doc.instrument_id = AIXTRON_INSTRUMENT_ID
    doc.company_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1")
    events = RuleBasedEventExtractor().extract(doc)

    score = CatalystScorer().score(AIXTRON_INSTRUMENT_ID, events)

    assert score.category_evidence["Orders & Backlog"].status == "POSITIVE_EVIDENCE"
    assert score.category_evidence["Orders & Backlog"].score is not None
    assert score.category_evidence["Growth"].status == "NO_EVIDENCE"
    assert score.category_evidence["Growth"].score is None
    assert score.buckets["Growth"] is None
    assert score.buckets["New Customers"] is None


def test_canonical_category_evidence_links_scored_events_outside_recent_slice() -> None:
    instrument_id = AIXTRON_INSTRUMENT_ID
    company_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1")
    base = _document(
        "https://www.aixtron.com/en/capex",
        "AIXTRON SE AIXA XETR DE000A0WMPJ6 will invest EUR 120 million in CAPEX.",
    ).model_copy(update={"instrument_id": instrument_id, "company_id": company_id})
    capex_event = next(event for event in RuleBasedEventExtractor().extract(base) if event.event_type == "CAPEX")
    newer_events = [
        capex_event.model_copy(update={
            "event_id": UUID(f"00000000-0000-0000-0000-{index:012d}"),
            "event_type": ResearchEventType.NEW_ORDER,
            "event_date": datetime(2026, 8, 30 - index, tzinfo=timezone.utc),
            "independence_key": f"newer-{index}",
        })
        for index in range(1, 12)
    ]
    capex_event = capex_event.model_copy(update={
        "event_date": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "independence_key": "capex-source",
        "impact": EventImpact.POSITIVE,
    })
    canonical = canonical_read_model_score(CatalystScorer().score(instrument_id, newer_events + [capex_event]))

    capex = canonical.category_evidence["CAPEX"]
    assert capex.status == "POSITIVE_EVIDENCE"
    assert [event.event_id for event in capex.supporting_events] == [capex_event.event_id]
    assert capex.source_count == capex.independent_source_count == 1
    assert "CAPEX & Capacity" not in canonical.category_evidence
    assert "NEW_FACILITIES" not in canonical.category_evidence
    assert canonical.category_evidence["ANALYST_TARGETS"].status == "NO_EVIDENCE"
    assert canonical.category_evidence["ANALYST_TARGETS"].supporting_events == []


def test_repository_ingestion_marks_duplicate_and_preserves_demo_summary() -> None:
    repo = ResearchRepository()
    first = repo.ingest_fixture(
        original_url="https://ir.aixtron.example/releases/new-order",
        source_type=SourceType.INVESTOR_RELATIONS,
        source_name="Fixture",
        publisher="DEMO",
        content_type="text/html",
        body="<html><title>AIXTRON order</title><body>AIXTRON SE AIXA XETR wins a new order worth €10 million.</body></html>",
        reliability=ReliabilityLevel.LEVEL_B,
        published_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )
    second = repo.ingest_fixture(
        original_url="https://ir.aixtron.example/releases/new-order?utm_source=feed",
        source_type=SourceType.RSS,
        source_name="Fixture RSS",
        publisher="DEMO",
        content_type="text/html",
        body="<html><title>AIXTRON order</title><body>AIXTRON SE AIXA XETR wins a new order worth €10 million.</body></html>",
        reliability=ReliabilityLevel.LEVEL_C,
    )
    assert first.status == DocumentStatus.PROCESSED
    assert second.status == DocumentStatus.DUPLICATE
    assert len([doc for doc in repo.documents_for(UUID("11111111-1111-1111-1111-111111111111")) if doc.canonical_url == first.canonical_url]) == 1
    assert repo.summary(UUID("11111111-1111-1111-1111-111111111111")).demo is True


@pytest.mark.asyncio
@respx.mock
async def test_targeted_gap_detection_fetches_only_missing_approved_sources() -> None:
    customer_source = _registered_source(
        "aixtron-customer-target",
        "https://www.aixtron.com/en/press/customer-win",
        priority=3,
        categories=("Customers",),
    )
    growth_source = _registered_source(
        "aixtron-growth-target",
        "https://www.aixtron.com/en/press/growth",
        priority=3,
        categories=("Growth",),
    )
    discovery = _StaticDiscovery([customer_source, growth_source])
    repo = ResearchRepository(settings=Settings(research_live_enabled=True, research_demo_enabled=True, research_max_retries=0), discovery=discovery)
    primary = registered_sources_for(AIXTRON_INSTRUMENT_ID)[0]
    respx.get(primary.url).mock(return_value=httpx.Response(200, headers={"content-type": "text/html"}, text=_aixtron_mojibake_fixture()))
    customer_route = respx.get(customer_source.url).mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<html><title>AIXTRON customer</title><main><p>Herzogenrath, May 2, 2026</p><p>AIXTRON SE AIXA XETR announced a customer win with Infineon Technologies AG for silicon-carbide production tools.</p></main></html>",
        )
    )
    growth_route = respx.get(growth_source.url).mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<html><title>AIXTRON growth</title><main><p>Herzogenrath, May 3, 2026</p><p>AIXTRON SE AIXA XETR announced geographic expansion into Japan for optoelectronics service coverage.</p></main></html>",
        )
    )

    summary = await repo.refresh(AIXTRON_INSTRUMENT_ID)

    assert customer_route.call_count == 1
    assert growth_route.call_count == 1
    assert summary.catalyst_score.category_evidence["Customers"].status == "POSITIVE_EVIDENCE"
    assert summary.catalyst_score.category_evidence["Growth"].status == "POSITIVE_EVIDENCE"
    urls = {event.source_url for event in summary.recent_events}
    assert customer_source.url in urls
    assert growth_source.url in urls


@pytest.mark.asyncio
@respx.mock
async def test_no_evidence_after_failed_discovery_remains_no_evidence() -> None:
    repo = ResearchRepository(
        settings=Settings(research_live_enabled=True, research_demo_enabled=True, research_max_retries=0),
        discovery=_StaticDiscovery([]),
    )
    primary = registered_sources_for(AIXTRON_INSTRUMENT_ID)[0]
    respx.get(primary.url).mock(return_value=httpx.Response(200, headers={"content-type": "text/html"}, text=_aixtron_mojibake_fixture()))

    summary = await repo.refresh(AIXTRON_INSTRUMENT_ID)

    assert summary.demo is False
    assert summary.catalyst_score.category_evidence["Growth"].status == "NO_EVIDENCE"
    assert summary.catalyst_score.category_evidence["Growth"].score is None
    assert summary.catalyst_score.category_evidence["Customers"].status == "NO_EVIDENCE"
    assert summary.catalyst_score.category_evidence["Customers"].score is None


@pytest.mark.asyncio
@respx.mock
async def test_cross_document_dedup_and_event_provenance_are_stable() -> None:
    source = _registered_source(
        "aixtron-duplicate-order",
        "https://www.aixtron.com/en/press/order-duplicate",
        priority=3,
        categories=("Orders & Backlog",),
    )
    repo = ResearchRepository(settings=Settings(research_live_enabled=True, research_demo_enabled=True, research_max_retries=0), discovery=_StaticDiscovery([source]))
    primary = registered_sources_for(AIXTRON_INSTRUMENT_ID)[0]
    body = "<html><title>AIXTRON order</title><main><p>Herzogenrath, May 4, 2026</p><p>AIXTRON SE AIXA XETR reported order intake of EUR 214.5 million (+54% yoy).</p></main></html>"
    respx.get(primary.url).mock(return_value=httpx.Response(200, headers={"content-type": "text/html"}, text=_aixtron_mojibake_fixture()))
    respx.get(source.url).mock(return_value=httpx.Response(200, headers={"content-type": "text/html"}, text=body))

    first = await repo.refresh(AIXTRON_INSTRUMENT_ID)
    second = await repo.refresh(AIXTRON_INSTRUMENT_ID)

    assert len(repo.documents_for(AIXTRON_INSTRUMENT_ID, source_mode=SourceMode.REAL)) == len(first.documents) == len(second.documents)
    assert len(repo.events_for(AIXTRON_INSTRUMENT_ID, source_mode=SourceMode.REAL)) == len(first.recent_events) == len(second.recent_events)
    assert {event.source_document_id for event in second.recent_events}.issubset({doc.document_id for doc in second.documents})


def test_conflicting_evidence_is_preserved_in_category_score() -> None:
    repo = ResearchRepository()
    positive = repo.ingest_fixture(
        original_url="https://www.aixtron.com/en/guidance-maintained",
        source_type=SourceType.INVESTOR_RELATIONS,
        source_name="Official",
        publisher="AIXTRON SE",
        content_type="text/html",
        body="<html><title>AIXTRON guidance</title><body>AIXTRON SE AIXA XETR DE000A0WMPJ6 confirms guidance for the full year 2026.</body></html>",
        reliability=ReliabilityLevel.LEVEL_B,
        published_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        source_mode=SourceMode.REAL,
    )
    negative = repo.ingest_fixture(
        original_url="https://www.aixtron.com/en/guidance-cut",
        source_type=SourceType.INVESTOR_RELATIONS,
        source_name="Official",
        publisher="AIXTRON SE",
        content_type="text/html",
        body="<html><title>AIXTRON guidance cut</title><body>AIXTRON SE AIXA XETR DE000A0WMPJ6 later announced a guidance cut for the full year 2026.</body></html>",
        reliability=ReliabilityLevel.LEVEL_B,
        published_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        source_mode=SourceMode.REAL,
    )
    assert positive.status == DocumentStatus.PROCESSED
    assert negative.status == DocumentStatus.PROCESSED

    summary = repo.summary(AIXTRON_INSTRUMENT_ID)

    assert any(event.event_type == "GUIDANCE_MAINTAINED" for event in summary.recent_events)
    assert any(event.event_type == "GUIDANCE_CUT" for event in summary.recent_events)
    assert summary.catalyst_score.category_evidence["Guidance"].status == "NEGATIVE_EVIDENCE"


def test_approved_source_discovery_filters_unapproved_and_seen_sources() -> None:
    profile = ResearchRepository().profile(AIXTRON_INSTRUMENT_ID)
    seen = canonicalize_url("https://www.aixtron.com/en/press/customer-win")
    allowed = _registered_source("allowed", "https://www.aixtron.com/en/press/customer-win", priority=3, categories=("Customers",))
    unapproved = _registered_source("blocked", "https://www.aixtron.com/en/press/growth", priority=3, categories=("Growth",), allowed=False)
    discovery = _StaticDiscovery([allowed, unapproved])

    results = discovery.discover(profile, {"Customers", "Growth"}, {seen})

    assert results == []


def test_registered_source_rejects_unallowed_source() -> None:
    repo = ResearchRepository()
    source = _registered_source("blocked", "https://www.aixtron.com/en/blocked", allowed=False)
    with pytest.raises(FetchError):
        repo._validate_registered_source(repo.profile(AIXTRON_INSTRUMENT_ID), source)


def test_search_query_generation_uses_company_ticker_alias_and_category_terms() -> None:
    profile = ResearchRepository().profile(AIXTRON_INSTRUMENT_ID)

    queries = generate_search_queries(profile, "Customers", SearchDateWindow(year=2026))

    assert "AIXTRON SE new customer 2026" in queries
    assert "AIXA customer order 2026" in queries
    assert "AIXTRON customer qualification 2026" in queries
    assert len(queries) == len(set(queries))


def test_search_queries_cover_broad_company_research_topics() -> None:
    profile = ResearchRepository().profile(AIXTRON_INSTRUMENT_ID)

    growth = generate_search_queries(profile, "Growth", SearchDateWindow(year=2026))
    orders = generate_search_queries(profile, "Orders & Backlog", SearchDateWindow(year=2026))
    capex = generate_search_queries(profile, "CAPEX & Capacity", SearchDateWindow(year=2026))
    guidance = generate_search_queries(profile, "Guidance", SearchDateWindow(year=2026))
    ownership = generate_search_queries(profile, "Ownership", SearchDateWindow(year=2026))
    analyst = generate_search_queries(profile, "Analyst", SearchDateWindow(year=2026))
    regulatory = generate_search_queries(profile, "Regulatory", SearchDateWindow(year=2026))
    analyst_targets = generate_search_queries(profile, "ANALYST_TARGETS", SearchDateWindow(year=2026))
    institutional = generate_search_queries(profile, "INSTITUTIONAL_ACTIVITY", SearchDateWindow(year=2026))

    assert any("earnings results" in query for query in growth)
    assert any("revenue growth" in query for query in growth)
    assert any("profit margins" in query for query in growth)
    assert any("order wins" in query for query in orders)
    assert any("new plant investment" in query for query in capex)
    assert any("management guidance" in query for query in guidance)
    assert any("institutional ownership" in query for query in ownership)
    assert any("analyst target" in query for query in analyst)
    assert any("regulatory announcement" in query for query in regulatory)
    assert any("analyst target price" in query for query in analyst_targets)
    assert any("institutional ownership" in query for query in institutional)


def test_source_classification_prefers_authoritative_publishers() -> None:
    profile = ResearchRepository().profile(AIXTRON_INSTRUMENT_ID)

    assert classify_source("www.aixtron.com", profile) == SourceClassification.OFFICIAL_COMPANY
    assert classify_source("www.sec.gov", profile) == SourceClassification.REGULATORY
    assert classify_source("www.nseindia.com", profile) == SourceClassification.EXCHANGE
    assert classify_source("www.bseindia.com", profile) == SourceClassification.EXCHANGE
    assert classify_source("www.sebi.gov.in", profile) == SourceClassification.REGULATORY
    assert classify_source("www.euronext.com", profile) == SourceClassification.EXCHANGE
    assert classify_source("www.afm.nl", profile) == SourceClassification.REGULATORY
    assert classify_source("www.deutsche-boerse.com", profile) == SourceClassification.EXCHANGE
    assert classify_source("www.infineon.com", profile) == SourceClassification.CUSTOMER
    assert classify_source("www.reuters.com", profile) == SourceClassification.REPUTABLE_NEWS
    assert classify_source("www.marketscreener.com", profile) == SourceClassification.INVESTMENT_RESEARCH
    assert classify_source("www.moneycontrol.com", profile) == SourceClassification.INVESTMENT_RESEARCH
    assert classify_source("www.trendlyne.com", profile) == SourceClassification.INVESTMENT_RESEARCH
    assert classify_source("www.screener.in", profile) == SourceClassification.INVESTMENT_RESEARCH
    assert classify_source("untrusted.example", profile) == SourceClassification.OTHER


def test_search_provider_disabled_mode_does_not_require_credentials() -> None:
    repo = ResearchRepository(settings=Settings(research_search_enabled=False))

    assert repo._search_discovery.provider.provider_name == "disabled"


def test_google_compatible_provider_complete_configuration_is_accepted() -> None:
    repo = ResearchRepository(
        settings=Settings(
            research_search_enabled=True,
            research_search_provider="google-compatible",
            research_search_endpoint="https://search.example/v1",
            research_search_api_key="secret-key",
            research_search_engine_id="engine-id",
        )
    )

    assert repo._search_discovery.provider.provider_name == "google-compatible"


def test_searxng_provider_complete_configuration_is_accepted() -> None:
    repo = ResearchRepository(
        settings=Settings(
            research_search_enabled=True,
            research_search_provider="searxng",
            research_search_endpoint="http://searxng/search",
        )
    )

    assert repo._search_discovery.provider.provider_name == "searxng"


def test_search_provider_enabled_missing_api_key_fails_explicitly() -> None:
    with pytest.raises(SearchProviderConfigurationError, match="SEARCH_PROVIDER_NOT_CONFIGURED"):
        ResearchRepository(
            settings=Settings(
                research_search_enabled=True,
                research_search_provider="google-compatible",
                research_search_endpoint="https://search.example/v1",
                research_search_engine_id="engine-id",
            )
        )


def test_search_provider_enabled_missing_engine_id_fails_explicitly() -> None:
    with pytest.raises(SearchProviderConfigurationError, match="engine_id"):
        ResearchRepository(
            settings=Settings(
                research_search_enabled=True,
                research_search_provider="google-compatible",
                research_search_endpoint="https://search.example/v1",
                research_search_api_key="secret-key",
            )
        )


def test_brave_compatible_provider_boundary_is_configuration_driven() -> None:
    repo = ResearchRepository(
        settings=Settings(
            research_search_enabled=True,
            research_search_provider="brave-compatible",
            research_search_endpoint="https://api.search.brave.com/res/v1/web/search",
            research_search_api_key="secret-key",
        )
    )

    assert repo._search_discovery.provider.provider_name == "brave-compatible"


@pytest.mark.asyncio
async def test_brave_compatible_provider_maps_query_token_header_and_limit() -> None:
    client = _RecordingSearchClient(
        httpx.Response(
            200,
            json={
                "web": {
                    "results": [
                        {"title": "One", "url": "https://www.aixtron.com/en/one", "description": "snippet one"},
                        {"title": "Two", "url": "https://www.aixtron.com/en/two", "description": "snippet two"},
                        {"title": "Three", "url": "https://www.aixtron.com/en/three", "description": "snippet three"},
                    ]
                }
            },
        )
    )
    provider = BraveCompatibleSearchDiscoveryProvider(
        "https://api.search.brave.com/res/v1/web/search",
        "secret-key",
        max_results_per_query=2,
        client=client,
    )
    profile = ResearchRepository().profile(AIXTRON_INSTRUMENT_ID)

    results = await provider.discover(
        profile,
        "Unmapped Category",
        SearchDateWindow(year=2026, query_limit=1),
    )

    assert client.calls
    assert client.calls[0]["url"] == "https://api.search.brave.com/res/v1/web/search"
    assert client.calls[0]["params"]["q"] == "AIXTRON SE Unmapped Category 2026"
    assert client.calls[0]["params"]["count"] == 2
    assert client.calls[0]["headers"]["X-Subscription-Token"] == "secret-key"
    assert len(client.calls) == 1
    assert len(results) == 2
    assert {result.url for result in results} == {"https://www.aixtron.com/en/one", "https://www.aixtron.com/en/two"}
    assert all(result.provider == "brave-compatible" for result in results)


@pytest.mark.asyncio
async def test_searxng_provider_maps_json_query_and_limit() -> None:
    client = _RecordingSearchClient(
        httpx.Response(
            200,
            json={
                "results": [
                    {"title": "One", "url": "https://www.aixtron.com/en/one", "content": "snippet one"},
                    {"title": "Two", "url": "https://www.reuters.com/markets/two", "content": "snippet two"},
                    {"title": "Three", "url": "https://example.com/three", "content": "snippet three"},
                ]
            },
        )
    )
    provider = SearxngSearchDiscoveryProvider(
        "http://searxng/search",
        max_results_per_query=2,
        client=client,
    )
    profile = ResearchRepository().profile(AIXTRON_INSTRUMENT_ID)

    results = await provider.discover(profile, "Unmapped Category", SearchDateWindow(year=2026, query_limit=1))

    assert client.calls
    assert client.calls[0]["url"] == "http://searxng/search"
    assert client.calls[0]["params"]["q"] == "AIXTRON SE Unmapped Category 2026"
    assert client.calls[0]["params"]["format"] == "json"
    assert client.calls[0]["params"]["categories"] == "general"
    assert len(results) == 2
    assert {result.provider for result in results} == {"searxng"}
    assert {result.url for result in results} == {"https://www.aixtron.com/en/one", "https://www.reuters.com/markets/two"}


@pytest.mark.asyncio
async def test_searxng_provider_malformed_response_is_unavailable() -> None:
    provider = SearxngSearchDiscoveryProvider(
        "http://searxng/search",
        client=_RecordingSearchClient(httpx.Response(200, json={"results": "bad"})),
    )
    profile = ResearchRepository().profile(AIXTRON_INSTRUMENT_ID)

    with pytest.raises(SearchProviderError) as exc_info:
        await provider.discover(profile, "Customers", SearchDateWindow(year=2026))

    assert str(exc_info.value) == "SEARCH_PROVIDER_UNAVAILABLE:invalid_response"


@pytest.mark.asyncio
async def test_searxng_empty_healthy_response_remains_a_legitimate_zero_result() -> None:
    provider = SearxngSearchDiscoveryProvider("http://searxng/search", client=_RecordingSearchClient(httpx.Response(200, json={"results": [], "unresponsive_engines": []})))
    assert await provider.discover(ResearchRepository().profile(AIXTRON_INSTRUMENT_ID), "Customers", SearchDateWindow(year=2026, query_limit=1)) == []


@pytest.mark.asyncio
async def test_searxng_empty_unresponsive_response_is_retryable_provider_failure() -> None:
    provider = SearxngSearchDiscoveryProvider("http://searxng/search", client=_RecordingSearchClient(httpx.Response(200, json={"results": [], "unresponsive_engines": ["google", "bing"]})))
    with pytest.raises(SearchProviderError, match="SEARCH_PROVIDER_DEGRADED"):
        await provider.discover(ResearchRepository().profile(AIXTRON_INSTRUMENT_ID), "Customers", SearchDateWindow(year=2026, query_limit=1))


@pytest.mark.asyncio
async def test_brave_compatible_provider_empty_results_are_allowed() -> None:
    provider = BraveCompatibleSearchDiscoveryProvider(
        "https://api.search.brave.com/res/v1/web/search",
        "secret-key",
        client=_RecordingSearchClient(httpx.Response(200, json={"web": {"results": []}})),
    )
    profile = ResearchRepository().profile(AIXTRON_INSTRUMENT_ID)

    results = await provider.discover(profile, "Customers", SearchDateWindow(year=2026))

    assert results == []


@pytest.mark.asyncio
async def test_brave_compatible_provider_malformed_response_is_unavailable() -> None:
    provider = BraveCompatibleSearchDiscoveryProvider(
        "https://api.search.brave.com/res/v1/web/search",
        "SECRET-KEY-123",
        client=_RecordingSearchClient(httpx.Response(200, json={"web": {"results": "bad"}})),
    )
    profile = ResearchRepository().profile(AIXTRON_INSTRUMENT_ID)

    with pytest.raises(SearchProviderError) as exc_info:
        await provider.discover(profile, "Customers", SearchDateWindow(year=2026))

    assert str(exc_info.value) == "SEARCH_PROVIDER_UNAVAILABLE:invalid_response"
    assert "SECRET-KEY-123" not in str(exc_info.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (401, "SEARCH_PROVIDER_FORBIDDEN"),
        (403, "SEARCH_PROVIDER_FORBIDDEN"),
        (429, "SEARCH_PROVIDER_RATE_LIMITED"),
    ],
)
async def test_brave_compatible_provider_auth_and_rate_limit_errors_are_sanitized(
    status_code: int, expected: str
) -> None:
    provider = BraveCompatibleSearchDiscoveryProvider(
        "https://api.search.brave.com/res/v1/web/search",
        "SECRET-KEY-123",
        client=_RecordingSearchClient(httpx.Response(status_code, json={"error": "bad SECRET-KEY-123"})),
    )
    profile = ResearchRepository().profile(AIXTRON_INSTRUMENT_ID)

    with pytest.raises(SearchProviderError) as exc_info:
        await provider.discover(profile, "Customers", SearchDateWindow(year=2026))

    assert str(exc_info.value) == expected
    assert "SECRET-KEY-123" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_brave_compatible_provider_timeout_is_sanitized() -> None:
    provider = BraveCompatibleSearchDiscoveryProvider(
        "https://api.search.brave.com/res/v1/web/search",
        "SECRET-KEY-123",
        client=_RecordingSearchClient(httpx.TimeoutException("timeout with SECRET-KEY-123")),
    )
    profile = ResearchRepository().profile(AIXTRON_INSTRUMENT_ID)

    with pytest.raises(SearchProviderError) as exc_info:
        await provider.discover(profile, "Customers", SearchDateWindow(year=2026))

    assert str(exc_info.value) == "SEARCH_PROVIDER_TIMEOUT"
    assert "SECRET-KEY-123" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_google_compatible_provider_maps_query_key_cx_and_limit() -> None:
    client = _RecordingSearchClient(
        httpx.Response(
            200,
            json={
                "items": [
                    {"title": "One", "link": "https://www.aixtron.com/en/one", "snippet": "snippet one"},
                    {"title": "Two", "link": "https://www.aixtron.com/en/two", "snippet": "snippet two"},
                    {"title": "Three", "link": "https://www.aixtron.com/en/three", "snippet": "snippet three"},
                ]
            },
        )
    )
    provider = GoogleCompatibleSearchDiscoveryProvider(
        "https://search.example/v1",
        "secret-key",
        "engine-id",
        max_results_per_query=2,
        client=client,
    )
    profile = ResearchRepository().profile(AIXTRON_INSTRUMENT_ID)

    results = await provider.discover(profile, "Unmapped Category", SearchDateWindow(year=2026))

    assert client.calls
    assert client.calls[0]["url"] == "https://search.example/v1"
    assert client.calls[0]["params"]["q"] == "AIXTRON SE Unmapped Category 2026"
    assert client.calls[0]["params"]["key"] == "secret-key"
    assert client.calls[0]["params"]["cx"] == "engine-id"
    assert client.calls[0]["params"]["num"] == 2
    assert len(results) == 6
    assert {result.url for result in results} == {"https://www.aixtron.com/en/one", "https://www.aixtron.com/en/two"}


@pytest.mark.asyncio
async def test_google_compatible_provider_rate_limit_is_sanitized() -> None:
    provider = GoogleCompatibleSearchDiscoveryProvider(
        "https://search.example/v1",
        "SECRET-KEY-123",
        "engine-id",
        client=_RecordingSearchClient(httpx.Response(429, json={"error": "rate"})),
    )
    profile = ResearchRepository().profile(AIXTRON_INSTRUMENT_ID)

    with pytest.raises(SearchProviderError) as exc_info:
        await provider.discover(profile, "Customers", SearchDateWindow(year=2026))

    assert str(exc_info.value) == "SEARCH_PROVIDER_RATE_LIMITED"
    assert "SECRET-KEY-123" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_google_compatible_provider_timeout_is_sanitized() -> None:
    provider = GoogleCompatibleSearchDiscoveryProvider(
        "https://search.example/v1",
        "SECRET-KEY-123",
        "engine-id",
        client=_RecordingSearchClient(httpx.TimeoutException("timeout with SECRET-KEY-123")),
    )
    profile = ResearchRepository().profile(AIXTRON_INSTRUMENT_ID)

    with pytest.raises(SearchProviderError) as exc_info:
        await provider.discover(profile, "Customers", SearchDateWindow(year=2026))

    assert str(exc_info.value) == "SEARCH_PROVIDER_TIMEOUT"
    assert "SECRET-KEY-123" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_google_compatible_provider_403_is_forbidden_and_does_not_leak_api_key() -> None:
    provider = GoogleCompatibleSearchDiscoveryProvider(
        "https://search.example/v1",
        "SECRET-KEY-123",
        "engine-id",
        client=_RecordingSearchClient(httpx.Response(403, json={"error": "bad SECRET-KEY-123"})),
    )
    profile = ResearchRepository().profile(AIXTRON_INSTRUMENT_ID)

    with pytest.raises(SearchProviderError) as exc_info:
        await provider.discover(profile, "Customers", SearchDateWindow(year=2026))

    assert str(exc_info.value) == "GOOGLE_PROVIDER_UNAVAILABLE"
    assert "SECRET-KEY-123" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_google_compatible_provider_500_is_unavailable() -> None:
    provider = GoogleCompatibleSearchDiscoveryProvider(
        "https://search.example/v1",
        "SECRET-KEY-123",
        "engine-id",
        client=_RecordingSearchClient(httpx.Response(500, json={"error": "server"})),
    )
    profile = ResearchRepository().profile(AIXTRON_INSTRUMENT_ID)

    with pytest.raises(SearchProviderError) as exc_info:
        await provider.discover(profile, "Customers", SearchDateWindow(year=2026))

    assert str(exc_info.value) == "SEARCH_PROVIDER_UNAVAILABLE"


@pytest.mark.asyncio
@respx.mock
async def test_research_refresh_continues_when_search_provider_forbidden() -> None:
    search = SearchDiscoveryService(_FailingSearchProvider("SEARCH_PROVIDER_FORBIDDEN"))
    repo = ResearchRepository(
        settings=Settings(research_live_enabled=True, research_demo_enabled=True, research_search_enabled=True, research_max_retries=0),
        discovery=_StaticDiscovery([]),
        search_discovery=search,
    )
    primary = registered_sources_for(AIXTRON_INSTRUMENT_ID)[0]
    respx.get(primary.url).mock(return_value=httpx.Response(200, headers={"content-type": "text/html"}, text=_aixtron_mojibake_fixture()))

    summary = await repo.refresh(AIXTRON_INSTRUMENT_ID)

    assert summary.demo is False
    assert summary.data_freshness == "REAL"
    assert summary.documents
    assert any(event.source_mode == SourceMode.REAL for event in summary.recent_events)
    assert summary.catalyst_score.category_evidence["Orders & Backlog"].status == "POSITIVE_EVIDENCE"
    assert summary.catalyst_score.category_evidence["Customers"].status == "NO_EVIDENCE"
    assert summary.catalyst_score.category_evidence["Customers"].score is None
    assert repo.last_live_error[AIXTRON_INSTRUMENT_ID].startswith("SEARCH_PROVIDER_UNAVAILABLE:")
    assert "SEARCH_PROVIDER_FORBIDDEN" in repo.last_live_error[AIXTRON_INSTRUMENT_ID]
    assert search.last_stats.rejected_reasons["SEARCH_PROVIDER_FORBIDDEN"] == 1


@pytest.mark.asyncio
@respx.mock
async def test_research_refresh_continues_when_search_provider_rate_limited() -> None:
    search = SearchDiscoveryService(_FailingSearchProvider("SEARCH_PROVIDER_RATE_LIMITED"))
    repo = ResearchRepository(
        settings=Settings(research_live_enabled=True, research_demo_enabled=True, research_search_enabled=True, research_max_retries=0),
        discovery=_StaticDiscovery([]),
        search_discovery=search,
    )
    primary = registered_sources_for(AIXTRON_INSTRUMENT_ID)[0]
    respx.get(primary.url).mock(return_value=httpx.Response(200, headers={"content-type": "text/html"}, text=_aixtron_mojibake_fixture()))

    summary = await repo.refresh(AIXTRON_INSTRUMENT_ID)

    assert summary.demo is False
    assert summary.catalyst_score.category_evidence["Customers"].status == "NO_EVIDENCE"
    assert repo.last_live_error[AIXTRON_INSTRUMENT_ID].startswith("SEARCH_PROVIDER_UNAVAILABLE:")
    assert "SEARCH_PROVIDER_RATE_LIMITED" in repo.last_live_error[AIXTRON_INSTRUMENT_ID]


@pytest.mark.asyncio
@respx.mock
async def test_research_refresh_continues_when_search_provider_timeout() -> None:
    search = SearchDiscoveryService(_FailingSearchProvider("SEARCH_PROVIDER_TIMEOUT"))
    repo = ResearchRepository(
        settings=Settings(research_live_enabled=True, research_demo_enabled=True, research_search_enabled=True, research_max_retries=0),
        discovery=_StaticDiscovery([]),
        search_discovery=search,
    )
    primary = registered_sources_for(AIXTRON_INSTRUMENT_ID)[0]
    respx.get(primary.url).mock(return_value=httpx.Response(200, headers={"content-type": "text/html"}, text=_aixtron_mojibake_fixture()))

    summary = await repo.refresh(AIXTRON_INSTRUMENT_ID)

    assert summary.demo is False
    assert summary.catalyst_score.category_evidence["Customers"].status == "NO_EVIDENCE"
    assert repo.last_live_error[AIXTRON_INSTRUMENT_ID].startswith("SEARCH_PROVIDER_UNAVAILABLE:")
    assert "SEARCH_PROVIDER_TIMEOUT" in repo.last_live_error[AIXTRON_INSTRUMENT_ID]


@pytest.mark.asyncio
@respx.mock
async def test_search_result_is_not_evidence_and_original_publisher_page_is_fetched() -> None:
    publisher_url = "https://www.infineon.com/cms/en/about-infineon/press/customer-aixtron"
    provider = _StaticSearchProvider(
        [
            CandidateSearchResult(
                title="Search result title",
                url=publisher_url,
                snippet="AIXTRON customer win snippet cx engine-id that must never become evidence",
                discovered_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
                provider="test-search",
                query_id="Customers:1",
                query="AIXTRON customer win 2026",
                category="Customers",
            )
        ]
    )
    search = SearchDiscoveryService(provider, max_documents_per_refresh=2)
    repo = ResearchRepository(
        settings=Settings(research_live_enabled=True, research_demo_enabled=True, research_search_enabled=True, research_max_retries=0),
        discovery=_StaticDiscovery([]),
        search_discovery=search,
    )
    primary = registered_sources_for(AIXTRON_INSTRUMENT_ID)[0]
    respx.get(primary.url).mock(return_value=httpx.Response(200, headers={"content-type": "text/html"}, text=_aixtron_mojibake_fixture()))
    route = respx.get(publisher_url).mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<html><title>Infineon validates AIXTRON tool</title><main><p>May 2, 2026</p><p>AIXTRON SE AIXA XETR was selected by Infineon Technologies AG for silicon-carbide production equipment.</p></main></html>",
        )
    )

    summary = await repo.refresh(AIXTRON_INSTRUMENT_ID)

    assert route.call_count == 1
    assert search.last_stats.accepted_count == 1
    assert summary.catalyst_score.category_evidence["Customers"].status == "POSITIVE_EVIDENCE"
    customer_events = [event for event in summary.recent_events if event.event_type == "NEW_CUSTOMER"]
    assert customer_events
    assert all("snippet" not in event.raw_evidence_reference.lower() for event in customer_events)
    assert all("engine-id" not in event.raw_evidence_reference.lower() for event in customer_events)
    assert customer_events[0].source_url == publisher_url
    assert customer_events[0].source_classification == SourceClassification.CUSTOMER
    assert customer_events[0].supporting_sources[0].url == publisher_url


@pytest.mark.asyncio
@respx.mock
async def test_search_refresh_works_for_company_without_registered_sources() -> None:
    besi_id = UUID("22222222-2222-2222-2222-222222222222")
    publisher_url = "https://www.marketscreener.com/quote/stock/BESI-6319/news/besi-capacity-expansion"
    provider = _StaticSearchProvider(
        [
            CandidateSearchResult(
                title="BESI capacity expansion",
                url=publisher_url,
                snippet="Search result snippet is not evidence",
                discovered_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
                provider="test-search",
                query_id="CAPEX & Capacity:1",
                query="BESI capacity expansion 2026",
                category="CAPEX & Capacity",
            )
        ]
    )
    search = SearchDiscoveryService(provider, max_documents_per_refresh=2)
    repo = ResearchRepository(
        settings=Settings(research_live_enabled=True, research_demo_enabled=True, research_search_enabled=True, research_max_retries=0),
        discovery=_StaticDiscovery([]),
        search_discovery=search,
    )
    respx.get(publisher_url).mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<html><title>BESI capacity expansion</title><main><p>May 2, 2026</p><p>BE Semiconductor Industries BESI XAMS will invest EUR 120 million in CAPEX for a new facility and capacity expansion.</p></main></html>",
        )
    )

    summary = await repo.refresh(besi_id)

    assert summary.demo is False
    assert summary.data_freshness == "REAL"
    assert summary.documents
    assert summary.documents[0].canonical_url == publisher_url
    assert summary.documents[0].source_classification == SourceClassification.INVESTMENT_RESEARCH
    assert summary.recent_events
    assert repo.last_live_error.get(besi_id) is None


@pytest.mark.asyncio
@respx.mock
async def test_search_result_found_but_fetch_rejected_is_categorized() -> None:
    besi_id = UUID("22222222-2222-2222-2222-222222222222")
    restricted_url = "https://seekingalpha.com/article/besi-q2"
    provider = _StaticSearchProvider([_candidate(restricted_url, "CAPEX & Capacity")])
    search = SearchDiscoveryService(provider, max_documents_per_refresh=2)
    repo = ResearchRepository(
        settings=Settings(research_live_enabled=True, research_demo_enabled=True, research_search_enabled=True, research_max_retries=0),
        discovery=_StaticDiscovery([]),
        search_discovery=search,
    )
    respx.get(restricted_url).mock(return_value=httpx.Response(403, headers={"content-type": "text/html"}))

    summary = await repo.refresh(besi_id)

    assert summary.data_freshness == "DEMO_FALLBACK"
    assert search.last_stats.candidate_count == 1
    assert search.last_stats.rejected_reasons["ROBOTS_OR_ACCESS_BLOCKED"] == 1
    assert repo.documents_for(besi_id, source_mode=SourceMode.REAL) == []


@pytest.mark.asyncio
@respx.mock
async def test_search_refresh_persists_relevant_document_without_publication_date_or_events() -> None:
    besi_id = UUID("22222222-2222-2222-2222-222222222222")
    publisher_url = "https://www.marketscreener.com/quote/stock/BESI-6319/news/besi-company-profile"
    provider = _StaticSearchProvider([_candidate(publisher_url, "Growth")])
    search = SearchDiscoveryService(provider, max_documents_per_refresh=2)
    repo = ResearchRepository(
        settings=Settings(research_live_enabled=True, research_demo_enabled=True, research_search_enabled=True, research_max_retries=0),
        discovery=_StaticDiscovery([]),
        search_discovery=search,
    )
    respx.get(publisher_url).mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<html><title>BESI company profile</title><main><p>BE Semiconductor Industries N.V. BESI XAMS supplies assembly equipment for semiconductor manufacturers.</p></main></html>",
        )
    )

    summary = await repo.refresh(besi_id)

    assert summary.demo is False
    assert summary.data_freshness == "REAL"
    assert len(summary.documents) == 1
    assert summary.documents[0].published_at is None
    assert summary.documents[0].retrieved_at is not None
    assert summary.documents[0].source_classification == SourceClassification.INVESTMENT_RESEARCH
    assert summary.documents[0].discovery_provider == "SEARCH_DISCOVERY"
    assert summary.recent_events == []
    assert search.last_stats.documents_fetched == 1
    assert search.last_stats.events_extracted == 0
    assert search.last_stats.rejected_reasons["SEARCH_RESULT_ACCEPTED"] == 1


@pytest.mark.asyncio
@respx.mock
async def test_tier_two_and_three_sources_are_accepted_with_lower_confidence() -> None:
    besi_id = UUID("22222222-2222-2222-2222-222222222222")
    urls = [
        "https://finance.yahoo.com/technology/articles/semiconductor-industries-n-v-announces-071600996.html",
        "https://www.marketscreener.com/quote/stock/BESI-6319/news/besi-capacity-expansion",
    ]
    provider = _StaticSearchProvider([_candidate(urls[0], "Guidance"), _candidate(urls[1], "CAPEX & Capacity")])
    search = SearchDiscoveryService(provider, max_documents_per_refresh=3)
    repo = ResearchRepository(
        settings=Settings(research_live_enabled=True, research_demo_enabled=True, research_search_enabled=True, research_max_retries=0),
        discovery=_StaticDiscovery([]),
        search_discovery=search,
    )
    respx.get(urls[0]).mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<html><title>BE Semiconductor Industries N.V. Announces Q2-26 Results</title><main><p>July 23, 2026</p><p>BE Semiconductor Industries N.V. BESI XAMS guidance for the full year remains supported by order intake.</p></main></html>",
        )
    )
    respx.get(urls[1]).mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<html><title>BESI capacity expansion</title><main><p>BE Semiconductor Industries BESI XAMS will invest EUR 120 million in CAPEX and expand capacity.</p></main></html>",
        )
    )

    summary = await repo.refresh(besi_id)

    classifications = {document.source_classification for document in summary.documents}
    assert SourceClassification.REPUTABLE_NEWS in classifications
    assert SourceClassification.INVESTMENT_RESEARCH in classifications
    assert any(event.reliability == ReliabilityLevel.LEVEL_C for event in summary.recent_events)
    assert any(event.reliability == ReliabilityLevel.LEVEL_D for event in summary.recent_events)
    assert all(event.confidence < 0.90 for event in summary.recent_events)


@pytest.mark.asyncio
@respx.mock
async def test_search_relevance_rejects_wrong_company_result_without_persisting() -> None:
    besi_id = UUID("22222222-2222-2222-2222-222222222222")
    url = "https://finance.yahoo.com/news/nvidia-capex"
    provider = _StaticSearchProvider([_candidate(url, "CAPEX & Capacity")])
    search = SearchDiscoveryService(provider, max_documents_per_refresh=2)
    repo = ResearchRepository(
        settings=Settings(research_live_enabled=True, research_demo_enabled=True, research_search_enabled=True, research_max_retries=0),
        discovery=_StaticDiscovery([]),
        search_discovery=search,
    )
    respx.get(url).mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<html><title>NVIDIA capex</title><main><p>NVIDIA Corporation NVDA XNAS reported large AI capex plans.</p></main></html>",
        )
    )

    await repo.refresh(besi_id)

    assert repo.documents_for(besi_id, source_mode=SourceMode.REAL) == []
    assert search.last_stats.rejected_reasons["COMPANY_RELEVANCE_FAILED"] == 1


@pytest.mark.asyncio
async def test_search_discovery_rejects_unsafe_and_seen_candidates_but_accepts_other_public_web() -> None:
    profile = ResearchRepository().profile(AIXTRON_INSTRUMENT_ID)
    provider = _StaticSearchProvider(
        [
            _candidate("http://127.0.0.1/private", "Customers"),
            _candidate("https://unknown.example/aixtron", "Customers"),
            _candidate("https://www.aixtron.com/en/press/customer-win", "Customers"),
        ]
    )
    search = SearchDiscoveryService(provider)

    results = await search.discover(profile, {"Customers"}, {canonicalize_url("https://www.aixtron.com/en/press/customer-win")})

    assert len(results) == 1
    assert results[0].source.url == "https://unknown.example/aixtron"
    assert results[0].source.source_classification == SourceClassification.OTHER
    assert search.last_stats.rejected_count == 2
    assert "DOMAIN_VALIDATION_FAILED" in search.last_stats.rejected_reasons
    assert "DUPLICATE" in search.last_stats.rejected_reasons


@pytest.mark.asyncio
async def test_search_discovery_classifies_validated_company_domain_as_official() -> None:
    profile = ResearchRepository().profile(AIXTRON_INSTRUMENT_ID)
    profile.known_domains = []
    provider = _StaticSearchProvider(
        [
            CandidateSearchResult(
                title="AIXTRON SE investor relations",
                url="https://www.aixtron.com/en/investors/results",
                snippet="AIXTRON SE annual report and quarterly results",
                discovered_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
                provider="test-search",
                query_id="FINANCIAL_RESULTS:1",
                query="AIXTRON SE annual report",
                category="FINANCIAL_RESULTS",
            )
        ]
    )
    search = SearchDiscoveryService(provider, max_documents_per_refresh=2)

    results = await search.discover(profile, {"FINANCIAL_RESULTS"}, set())

    assert results[0].source.source_classification == SourceClassification.OFFICIAL_COMPANY
    assert results[0].source.source_type == SourceType.INVESTOR_RELATIONS


@pytest.mark.asyncio
async def test_search_discovery_is_bounded() -> None:
    profile = ResearchRepository().profile(AIXTRON_INSTRUMENT_ID)
    provider = _StaticSearchProvider([_candidate(f"https://www.aixtron.com/en/press/customer-{index}", "Customers") for index in range(10)])
    search = SearchDiscoveryService(provider, max_documents_per_refresh=3)

    results = await search.discover(profile, {"Customers"}, set())

    assert len(results) == 3
    assert search.last_stats.accepted_count == 3


def test_source_independence_uses_content_hash_for_syndicated_duplicates() -> None:
    repo = ResearchRepository()
    body = "<html><title>AIXTRON customer</title><body>AIXTRON SE AIXA XETR announced a customer win with Infineon Technologies AG.</body></html>"
    first = repo.ingest_fixture(
        original_url="https://www.aixtron.com/en/customer-win",
        source_type=SourceType.INVESTOR_RELATIONS,
        source_classification=SourceClassification.OFFICIAL_COMPANY,
        source_name="AIXTRON",
        publisher="AIXTRON SE",
        content_type="text/html",
        body=body,
        reliability=ReliabilityLevel.LEVEL_B,
        source_mode=SourceMode.REAL,
    )
    duplicate = repo.ingest_fixture(
        original_url="https://www.reuters.com/markets/aixtron-customer-win",
        source_type=SourceType.NEWS,
        source_classification=SourceClassification.REPUTABLE_NEWS,
        source_name="Reuters",
        publisher="Reuters",
        content_type="text/html",
        body=body,
        reliability=ReliabilityLevel.LEVEL_C,
        source_mode=SourceMode.REAL,
    )

    assert first.status == DocumentStatus.PROCESSED
    assert duplicate.status == DocumentStatus.DUPLICATE
    assert duplicate.duplicate_of_document_id == first.document_id
    evidence = repo.summary(AIXTRON_INSTRUMENT_ID).catalyst_score.category_evidence["Customers"]
    assert evidence.independent_source_count == 1


def test_durable_persistence_survives_repository_restart(tmp_path) -> None:
    database = tmp_path / "research.sqlite"
    persistence = SqliteResearchPersistence(database)
    repo = ResearchRepository(settings=Settings(research_demo_enabled=False), persistence=persistence)
    doc = repo.ingest_fixture(
        original_url="https://www.aixtron.com/en/customer-win",
        source_type=SourceType.INVESTOR_RELATIONS,
        source_classification=SourceClassification.OFFICIAL_COMPANY,
        source_name="AIXTRON",
        publisher="AIXTRON SE",
        content_type="text/html",
        body="<html><title>AIXTRON customer</title><body>AIXTRON SE AIXA XETR DE000A0WMPJ6 announced a customer win with Infineon Technologies AG.</body></html>",
        reliability=ReliabilityLevel.LEVEL_B,
        source_mode=SourceMode.REAL,
    )

    restarted = ResearchRepository(
        settings=Settings(research_demo_enabled=False),
        persistence=SqliteResearchPersistence(database),
    )
    summary = restarted.summary(AIXTRON_INSTRUMENT_ID)

    assert doc.status == DocumentStatus.PROCESSED
    assert summary.demo is False
    assert summary.documents[0].canonical_url == "https://www.aixtron.com/en/customer-win"
    assert summary.recent_events
    assert summary.catalyst_score.category_evidence["Customers"].status == "POSITIVE_EVIDENCE"


def test_document_subtype_round_trips_and_deduplication_enriches_metadata(tmp_path) -> None:
    database = tmp_path / "research.sqlite"
    repo = ResearchRepository(settings=Settings(research_demo_enabled=False), persistence=SqliteResearchPersistence(database))
    kwargs = dict(
        original_url="https://www.aixtron.com/en/investor-presentation",
        source_type=SourceType.INVESTOR_RELATIONS,
        source_classification=SourceClassification.OFFICIAL_COMPANY,
        source_name="AIXTRON",
        publisher="AIXTRON SE",
        content_type="text/html",
        body="<html><title>AIXTRON presentation</title><body>AIXTRON SE AIXA XETR DE000A0WMPJ6 published a sufficiently detailed investor presentation.</body></html>",
        reliability=ReliabilityLevel.LEVEL_B,
        source_mode=SourceMode.REAL,
    )
    generic = repo.ingest_fixture(**kwargs)
    enriched = generic.model_copy(update={"document_subtype": DocumentSubtype.INVESTOR_PRESENTATION})
    assert repo._persistence.upsert_document(enriched) is False

    restarted = ResearchRepository(settings=Settings(research_demo_enabled=False), persistence=SqliteResearchPersistence(database))
    documents = restarted.documents_for(AIXTRON_INSTRUMENT_ID, source_mode=SourceMode.REAL)
    assert len(documents) == 1
    assert documents[0].document_id == generic.document_id
    assert documents[0].document_subtype == DocumentSubtype.INVESTOR_PRESENTATION


def test_durable_document_and_event_refresh_are_idempotent(tmp_path) -> None:
    database = tmp_path / "research.sqlite"
    repo = ResearchRepository(settings=Settings(research_demo_enabled=False), persistence=SqliteResearchPersistence(database))
    kwargs = dict(
        original_url="https://www.aixtron.com/en/order",
        source_type=SourceType.INVESTOR_RELATIONS,
        source_classification=SourceClassification.OFFICIAL_COMPANY,
        source_name="AIXTRON",
        publisher="AIXTRON SE",
        content_type="text/html",
        body="<html><title>AIXTRON order</title><body>AIXTRON SE AIXA XETR DE000A0WMPJ6 announced a new order worth EUR 10 million.</body></html>",
        reliability=ReliabilityLevel.LEVEL_B,
        source_mode=SourceMode.REAL,
    )

    first = repo.ingest_fixture(**kwargs)
    second = repo.ingest_fixture(**kwargs)
    restarted = ResearchRepository(settings=Settings(research_demo_enabled=False), persistence=SqliteResearchPersistence(database))

    assert first.status == DocumentStatus.PROCESSED
    assert second.status == DocumentStatus.DUPLICATE
    assert len(restarted.documents_for(AIXTRON_INSTRUMENT_ID, source_mode=SourceMode.REAL)) == 1
    assert len(restarted.events_for(AIXTRON_INSTRUMENT_ID, source_mode=SourceMode.REAL)) == 1


def test_durable_event_can_link_multiple_sources(tmp_path) -> None:
    persistence = SqliteResearchPersistence(tmp_path / "research.sqlite")
    repo = ResearchRepository(settings=Settings(research_demo_enabled=False), persistence=persistence)
    first = repo.ingest_fixture(
        original_url="https://www.aixtron.com/en/customer-win",
        source_type=SourceType.INVESTOR_RELATIONS,
        source_classification=SourceClassification.OFFICIAL_COMPANY,
        source_name="AIXTRON",
        publisher="AIXTRON SE",
        content_type="text/html",
        body="<html><title>AIXTRON customer</title><body>AIXTRON SE AIXA XETR DE000A0WMPJ6 announced a customer win with Infineon Technologies AG.</body></html>",
        reliability=ReliabilityLevel.LEVEL_B,
        source_mode=SourceMode.REAL,
    )
    second = repo.ingest_fixture(
        original_url="https://www.infineon.com/cms/en/aixtron",
        source_type=SourceType.SEARCH_DISCOVERY,
        source_classification=SourceClassification.CUSTOMER,
        source_name="Infineon",
        publisher="Infineon",
        content_type="text/html",
        body="<html><title>Infineon customer</title><body><p>Infineon source confirmation.</p><p>AIXTRON SE AIXA XETR DE000A0WMPJ6 announced a customer win with Infineon Technologies AG.</p></body></html>",
        reliability=ReliabilityLevel.LEVEL_B,
        source_mode=SourceMode.REAL,
    )
    event = next(
        event
        for event in repo.events_for(AIXTRON_INSTRUMENT_ID, source_mode=SourceMode.REAL)
        if event.source_document_id == first.document_id
    )
    event.supporting_sources.append(
        event.supporting_sources[0].model_copy(
            update={
                "document_id": second.document_id,
                "url": second.canonical_url,
                "canonical_url": second.canonical_url,
                "source_name": "Infineon",
                "publisher": "Infineon",
                "source_type": SourceClassification.CUSTOMER,
            }
        )
    )
    persistence.upsert_event(event)

    restarted = ResearchRepository(settings=Settings(research_demo_enabled=False), persistence=SqliteResearchPersistence(tmp_path / "research.sqlite"))
    loaded_event = next(
        loaded
        for loaded in restarted.events_for(AIXTRON_INSTRUMENT_ID, source_mode=SourceMode.REAL)
        if loaded.event_id == event.event_id
    )

    assert first.status == DocumentStatus.PROCESSED
    assert len(loaded_event.supporting_sources) == 2


def test_refresh_audit_records_success_and_failure_states(tmp_path) -> None:
    persistence = SqliteResearchPersistence(tmp_path / "research.sqlite")
    run = persistence.start_refresh_run(
        instrument_id=AIXTRON_INSTRUMENT_ID,
        company_id=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1"),
        correlation_id="phase4-test",
        mode="LIVE",
    )
    persistence.complete_refresh_run(
        run,
        status="FAILED",
        documents_discovered=1,
        documents_accepted=0,
        events_extracted=0,
        events_created=0,
        events_updated=0,
        deduplicated_count=1,
        safe_error_code="FetchError",
        safe_error_message="safe",
    )
    row = persistence._connection.execute("SELECT * FROM research_refresh_runs WHERE refresh_run_id = ?", (str(run.refresh_run_id),)).fetchone()

    assert row["status"] == "FAILED"
    assert row["correlation_id"] == "phase4-test"
    assert row["safe_error_code"] == "FetchError"


def test_durable_unique_constraints_and_rollback_on_failure(tmp_path) -> None:
    persistence = SqliteResearchPersistence(tmp_path / "research.sqlite")
    try:
        with persistence._connection:
            persistence._connection.execute(
                "INSERT INTO research_events (event_id, instrument_id, company_id, event_fingerprint, event_type, detected_at, title, summary, impact, time_horizon, confidence, status, source_document_id, source_url, source_type, source_classification, reliability, source_mode, raw_evidence_reference, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(UUID("99999999-9999-9999-9999-999999999999")),
                    str(AIXTRON_INSTRUMENT_ID),
                    "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1",
                    "bad",
                    "NEW_ORDER",
                    "2026-01-01T00:00:00+00:00",
                    "bad",
                    "bad",
                    "POSITIVE",
                    "SHORT_TERM",
                    0.9,
                    "VALIDATED",
                    str(UUID("88888888-8888-8888-8888-888888888888")),
                    "https://www.aixtron.com/en/missing",
                    "INVESTOR_RELATIONS",
                    "OFFICIAL_COMPANY",
                    "LEVEL_B",
                    "REAL",
                    "bad",
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                ),
            )
    except Exception:
        pass

    count = persistence._connection.execute("SELECT count(*) AS count FROM research_events").fetchone()["count"]
    assert count == 0


@pytest.mark.asyncio
async def test_portfolio_research_handles_supported_source_not_configured_and_unresolved(tmp_path) -> None:
    positions = [
        _portfolio_position("DE000A0WMPJ6", "AIXA", "XETR", "AIXTRON SE"),
        _portfolio_position("DE000A0WMPJ6", "AIXA", "XETR", "AIXTRON SE"),
        _portfolio_position("NL0012866412", "BESI", "XAMS", "BE Semiconductor Industries"),
        _portfolio_position("UNKNOWN", "UNKNOWN", "XNAS", "Unknown Corp"),
    ]
    client = _RecordingPortfolioClient(positions)
    repo = ResearchRepository(settings=Settings(research_live_enabled=False, research_demo_enabled=False))
    orchestrator = PortfolioResearchOrchestrator(
        repo,
        Settings(
            portfolio_service_base_url="http://portfolio-service",
            research_live_enabled=True,
            research_search_enabled=True,
        ),
        client=client,
        structured_provider=_UnavailableStructuredProvider(),
    )

    result = await orchestrator.refresh_portfolio(UUID("aaaaaaaa-1111-1111-1111-aaaaaaaaaaaa"), correlation_id="phase4-correlation")

    statuses = {company.company_name: company.status for company in result.companies}
    assert result.companies_requested == 3
    assert statuses["AIXTRON SE"] == "RESOLVED_NO_SOURCES"
    assert statuses["BE Semiconductor Industries"] == "RESOLVED_NO_SOURCES"
    assert statuses["Unknown Corp"] == "COMPANY_NOT_RESOLVED"
    assert client.calls[0]["headers"]["X-Correlation-Id"] == "phase4-correlation"


@pytest.mark.asyncio
async def test_portfolio_research_resolves_ibkr_conid_exchange_alias_and_skips_etf() -> None:
    repo = ResearchRepository(settings=Settings(research_live_enabled=False, research_demo_enabled=True))
    repo.profiles[0].provider_instrument_ids["IBKR"] = "AIXTRON-CONID"
    client = _RecordingPortfolioClient([
        _portfolio_position(None, "AIXA", "IBIS2", "AIXA", provider="IBKR", provider_instrument_id="AIXTRON-CONID"),
        _portfolio_position("IE00B4L5Y983", "IUSA", "AEB", "iShares Core S&P 500 UCITS ETF", asset_type="ETF"),
    ])
    orchestrator = PortfolioResearchOrchestrator(
        repo,
        Settings(
            portfolio_service_base_url="http://portfolio-service",
            research_live_enabled=True,
            research_search_enabled=True,
        ),
        client=client,
    )

    result = await orchestrator.refresh_portfolio(UUID("aaaaaaaa-1111-1111-1111-aaaaaaaaaaaa"))

    by_ticker = {company.ticker: company for company in result.companies}
    assert by_ticker["AIXA"].instrument_id == AIXTRON_INSTRUMENT_ID
    assert by_ticker["AIXA"].company_id == UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1")
    assert by_ticker["AIXA"].status == "RESOLVED_PARTIAL_DATA"
    assert by_ticker["AIXA"].exchange == "XETR"
    assert by_ticker["IUSA"].status == "ETF_UNSUPPORTED"
    assert by_ticker["IUSA"].asset_type == "ETF"
    assert len([profile for profile in repo.list_profiles() if profile.company_name == "AIXTRON SE"]) == 1


@pytest.mark.asyncio
async def test_portfolio_research_read_shows_existing_besi_real_research_without_refresh() -> None:
    repo = ResearchRepository(settings=Settings(research_live_enabled=True, research_demo_enabled=True))
    repo.ingest_fixture(
        original_url="https://www.besi.com/investor-relations/press-releases/details/real-order",
        source_type=SourceType.INVESTOR_RELATIONS,
        source_classification=SourceClassification.OFFICIAL_COMPANY,
        source_name="BESI official press release",
        publisher="BE Semiconductor Industries",
        content_type="text/html",
        body=(
            "<html><title>BESI capacity expansion</title><body>"
            "BE Semiconductor Industries BESI XAMS announced capacity expansion for hybrid bonding equipment "
            "and a new customer order supporting 2026 backlog."
            "</body></html>"
        ),
        reliability=ReliabilityLevel.LEVEL_B,
        source_mode=SourceMode.REAL,
    )
    client = _RecordingPortfolioClient([
        _portfolio_position(
            "NL0012866412",
            "BESI",
            "AEB",
            "BE Semiconductor Industries N.V.",
            provider="IBKR",
            provider_instrument_id="BESI-CONID",
            data_freshness="REAL_BROKER",
        )
    ])
    orchestrator = PortfolioResearchOrchestrator(repo, Settings(portfolio_service_base_url="http://portfolio-service"), client=client,
        structured_provider=_UnavailableStructuredProvider())

    result = await orchestrator.read_portfolio_summary(UUID("aaaaaaaa-1111-1111-1111-aaaaaaaaaaaa"))

    company = result.companies[0]
    assert company.company_name == "BE Semiconductor Industries"
    assert company.status == "RESOLVED_RESEARCH_AVAILABLE"
    assert company.freshness == "REAL"
    assert company.mode == "REAL"
    assert company.document_count == 1
    assert company.event_count > 0
    assert company.source_count == 1


@pytest.mark.asyncio
async def test_global_instrument_alias_is_resolvable_before_research_exists_and_reuses_global_research() -> None:
    repo = ResearchRepository(settings=Settings(research_live_enabled=True, research_demo_enabled=False))
    global_instrument_id = UUID("77777777-7777-7777-7777-777777777777")
    broker_position = _portfolio_position(
        "INE000K01001", "BROKER_ALIAS", "NSE", "Example Components Limited",
        provider="ICICI_DIRECT", provider_instrument_id="broker-alias", data_freshness="REAL_BROKER",
    )
    broker_position["instrument"].update({
        "globalInstrumentId": str(global_instrument_id), "country": "IN",
        "providerMappings": [
            {"provider": "NSE", "providerSymbol": "VERIFIED_NSE", "status": "VERIFIED"},
            {"provider": "YAHOO_FINANCE", "providerSymbol": "VERIFIED.NS", "status": "VERIFIED"},
        ],
    })
    orchestrator = PortfolioResearchOrchestrator(
        repo, Settings(portfolio_service_base_url="http://portfolio-service"),
        client=_RecordingPortfolioClient([broker_position]), structured_provider=_UnavailableStructuredProvider(),
    )

    before_refresh = await orchestrator.read_portfolio_summary(UUID("aaaaaaaa-1111-1111-1111-111111111111"))
    assert before_refresh.companies[0].instrument_id == global_instrument_id
    assert before_refresh.companies[0].status == "RESOLVED_NO_SOURCES"
    assert before_refresh.companies[0].provider == "ICICI_DIRECT"
    assert before_refresh.companies[0].provider_instrument_id == "broker-alias"
    assert before_refresh.companies[0].listing_provider == "NSE"
    assert before_refresh.companies[0].listing_symbol == "VERIFIED_NSE"
    assert before_refresh.companies[0].primary_exchange == "NSE"
    assert before_refresh.companies[0].verified_provider_mappings == {"ICICI_DIRECT": "BROKER-ALIAS", "NSE": "VERIFIED_NSE", "YAHOO_FINANCE": "VERIFIED.NS"}
    profile = repo.profile(global_instrument_id)
    assert profile.ticker == "BROKER_ALIAS"
    assert profile.provider_instrument_ids["NSE"] == "VERIFIED_NSE"

    repo.ingest_fixture(
        original_url="https://example.test/results", source_type=SourceType.EXCHANGE_ANNOUNCEMENT,
        source_classification=SourceClassification.EXCHANGE, source_name="NSE", publisher="NSE", content_type="text/html",
        body="<main>Example Components Limited VERIFIED_NSE INE000K01001 quarterly financial results revenue 1000 crore PAT 100 crore</main>",
        reliability=ReliabilityLevel.LEVEL_A, source_mode=SourceMode.REAL, expected_profile=profile,
    )
    manual_alias = _portfolio_position("INE000K01001", "SECOND_BROKER_ALIAS", "NSE", "Example Components Limited")
    manual_alias["instrument"].update({"globalInstrumentId": str(global_instrument_id), "country": "IN", "providerMappings": broker_position["instrument"]["providerMappings"]})
    assert len(orchestrator._dedupe_instruments([broker_position, manual_alias])) == 1
    orchestrator._client = _RecordingPortfolioClient([manual_alias])
    after_refresh = await orchestrator.read_portfolio_summary(UUID("bbbbbbbb-1111-1111-1111-111111111111"))
    assert after_refresh.companies[0].instrument_id == global_instrument_id
    assert after_refresh.companies[0].status == "RESOLVED_PARTIAL_DATA"
    assert after_refresh.companies[0].document_count == 1


@pytest.mark.asyncio
async def test_portfolio_research_read_resolves_aixa_to_existing_aixtron_without_refresh_or_duplicate() -> None:
    repo = ResearchRepository(settings=Settings(research_live_enabled=True, research_demo_enabled=True))
    repo.ingest_fixture(
        original_url="https://www.aixtron.com/en/press/press-releases/read-existing",
        source_type=SourceType.INVESTOR_RELATIONS,
        source_classification=SourceClassification.OFFICIAL_COMPANY,
        source_name="AIXTRON official press release",
        publisher="AIXTRON SE",
        content_type="text/html",
        body=_aixtron_live_fixture(),
        reliability=ReliabilityLevel.LEVEL_B,
        source_mode=SourceMode.REAL,
    )
    client = _RecordingPortfolioClient([
        _portfolio_position(
            None,
            "AIXA",
            "IBIS2",
            "AIXA",
            provider="IBKR",
            provider_instrument_id="AIXA-CONID",
            data_freshness="REAL_BROKER",
        )
    ])
    orchestrator = PortfolioResearchOrchestrator(repo, Settings(portfolio_service_base_url="http://portfolio-service"), client=client,
                                                 structured_provider=_UnavailableStructuredProvider())

    result = await orchestrator.read_portfolio_summary(UUID("aaaaaaaa-1111-1111-1111-aaaaaaaaaaaa"))

    company = result.companies[0]
    assert company.instrument_id == AIXTRON_INSTRUMENT_ID
    assert company.company_id == UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1")
    assert company.company_name == "AIXTRON SE"
    assert company.status == "RESOLVED_RESEARCH_AVAILABLE"
    assert company.freshness == "REAL"
    assert len([profile for profile in repo.list_profiles() if profile.company_name == "AIXTRON SE"]) == 1


@pytest.mark.asyncio
async def test_portfolio_research_read_derives_terminal_statuses_without_refresh() -> None:
    repo = ResearchRepository(settings=Settings(research_live_enabled=False, research_demo_enabled=True))
    client = _RecordingPortfolioClient([
        _portfolio_position("IE00B4L5Y983", "IUSA", "AEB", "iShares Core S&P 500 UCITS ETF", asset_type="ETF"),
        _portfolio_position("DE000A0WMPJ6", "AIXA", "XETR", "AIXTRON SE", data_freshness="REAL_BROKER"),
        _portfolio_position("UNKNOWN", "UNKNOWN", "XNAS", "Unknown Corp", data_freshness="REAL_BROKER"),
    ])
    orchestrator = PortfolioResearchOrchestrator(repo, Settings(portfolio_service_base_url="http://portfolio-service"), client=client,
                                                 structured_provider=_UnavailableStructuredProvider())

    result = await orchestrator.read_portfolio_summary(UUID("aaaaaaaa-1111-1111-1111-aaaaaaaaaaaa"))

    by_ticker = {company.ticker: company for company in result.companies}
    assert by_ticker["IUSA"].status == "ETF_UNSUPPORTED"
    assert by_ticker["AIXA"].status == "RESOLVED_NO_SOURCES"
    assert by_ticker["UNKNOWN"].status == "COMPANY_NOT_RESOLVED"
    assert "INSTRUMENT_RESOLVED" not in {company.status for company in result.companies}


@pytest.mark.asyncio
async def test_etf_projects_only_trusted_nse_listing_identity_without_changing_ticker() -> None:
    repo = ResearchRepository(settings=Settings(research_live_enabled=False, research_demo_enabled=True))
    trusted = _portfolio_position("INF204KB17I5", "GOLDEX", "NSE", "NIPPON INDIA ETF GOLD BEES", asset_type="ETF",
        canonical_symbol="GOLDEX", canonical_name="NIPPON INDIA ETF GOLD BEES", canonical_exchange="NSE", security_type="ETF",
        provider_mappings=[
            {"provider": "NSE", "providerSymbol": "GOLDBEES", "exchange": "NSE", "status": "VERIFIED", "resolutionSource": "NSE_VALIDATED_RESOLUTION"},
            {"provider": "YAHOO_FINANCE", "providerSymbol": "GOLDBEES.NS", "exchange": "NSE", "status": "VERIFIED"},
        ])
    unverified = _portfolio_position("INF179KC1HS2", "HDFN50", "NSE", "HDFC NIFTY NEXT 50 ETF", asset_type="ETF",
        canonical_symbol="HDFN50", canonical_name="HDFC NIFTY NEXT 50 ETF", canonical_exchange="NSE", security_type="ETF",
        provider_mappings=[{"provider": "NSE", "providerSymbol": "HDFN50", "exchange": "NSE", "status": "INVALID"}])
    orchestrator = PortfolioResearchOrchestrator(repo, Settings(portfolio_service_base_url="http://portfolio-service"),
        client=_RecordingPortfolioClient([trusted, unverified]), structured_provider=_UnavailableStructuredProvider())

    result = await orchestrator.read_portfolio_summary(UUID("aaaaaaaa-1111-1111-1111-aaaaaaaaaaaa"))
    by_ticker = {company.ticker: company for company in result.companies}
    gold = by_ticker["GOLDEX"]
    assert gold.listing_provider == "NSE"
    assert gold.listing_symbol == "GOLDBEES"
    assert gold.primary_exchange == "NSE"
    assert gold.verified_provider_mappings == {"NSE": "GOLDBEES", "YAHOO_FINANCE": "GOLDBEES.NS"}
    assert gold.ticker == "GOLDEX"
    hdfc = by_ticker["HDFN50"]
    assert hdfc.listing_provider is None
    assert hdfc.listing_symbol == "HDFN50"
    assert "NSE" not in hdfc.verified_provider_mappings


@pytest.mark.asyncio
async def test_portfolio_research_read_does_not_resolve_from_ticker_only_unknown_exchange() -> None:
    repo = ResearchRepository(settings=Settings(research_live_enabled=False, research_demo_enabled=True))
    position = _portfolio_position(None, "AIXA", "UNKNOWN", "AIXA", data_freshness="REAL_BROKER")
    position["instrument"]["instrumentId"] = "99999999-9999-9999-9999-999999999999"
    client = _RecordingPortfolioClient([position])
    orchestrator = PortfolioResearchOrchestrator(repo, Settings(portfolio_service_base_url="http://portfolio-service"), client=client,
                                                 structured_provider=_UnavailableStructuredProvider())

    result = await orchestrator.read_portfolio_summary(UUID("aaaaaaaa-1111-1111-1111-aaaaaaaaaaaa"))

    assert result.companies[0].status == "COMPANY_NOT_RESOLVED"


@pytest.mark.asyncio
async def test_portfolio_research_read_uses_enriched_ibkr_metadata_for_etf_and_equity() -> None:
    repo = ResearchRepository(settings=Settings(research_live_enabled=False, research_demo_enabled=True))
    client = _RecordingPortfolioClient([
        _portfolio_position(
            "IE0031442068",
            "IUSA",
            "UNKNOWN",
            "IUSA",
            provider="IBKR",
            provider_instrument_id="789012",
            asset_type="ETF",
            data_freshness="REAL_BROKER",
            broker_symbol="IUSA",
            broker_description="IUSA",
            broker_exchange="AEB",
            canonical_symbol="IUSA",
            canonical_name="iShares Core S&P 500 UCITS ETF",
            canonical_exchange="XAMS",
            canonical_mic="XAMS",
            security_type="ETF",
        ),
        _portfolio_position(
            "DE000RENK730",
            "R3NK",
            "UNKNOWN",
            "R3NK",
            provider="IBKR",
            provider_instrument_id="345678",
            asset_type="EQUITY",
            data_freshness="REAL_BROKER",
            broker_symbol="R3NK",
            broker_description="R3NK",
            broker_exchange="IBIS2",
            canonical_symbol="R3NK",
            canonical_name="RENK Group AG",
            canonical_exchange="XETR",
            canonical_mic="XETR",
            security_type="STK",
        ),
    ])
    orchestrator = PortfolioResearchOrchestrator(repo, Settings(portfolio_service_base_url="http://portfolio-service"), client=client)

    result = await orchestrator.read_portfolio_summary(UUID("aaaaaaaa-1111-1111-1111-aaaaaaaaaaaa"))

    by_ticker = {company.ticker: company for company in result.companies}
    assert by_ticker["IUSA"].company_name == "iShares Core S&P 500 UCITS ETF"
    assert by_ticker["IUSA"].asset_type == "ETF"
    assert by_ticker["IUSA"].status == "ETF_UNSUPPORTED"
    assert by_ticker["R3NK"].company_name == "RENK Group AG"
    assert by_ticker["R3NK"].asset_type == "EQUITY"
    assert by_ticker["R3NK"].exchange == "XETR"
    assert by_ticker["R3NK"].status == "RESOLVED_NO_SOURCES"


@pytest.mark.asyncio
@respx.mock
async def test_iusa_and_nqse_route_to_etf_pipeline_and_use_canonical_name_search() -> None:
    class RecordingEtfSearchProvider:
        provider_name = "test-search"

        def __init__(self) -> None:
            self.names: list[str] = []
            self.queries: list[str] = []

        async def discover(self, profile, category: str, date_window: SearchDateWindow) -> list[CandidateSearchResult]:
            self.names.append(profile.fund_name)
            query = generate_search_queries(profile, category, date_window)[0]
            self.queries.append(query)
            if category != "ETF_PROFILE":
                return []
            slug = "iusa" if profile.ticker == "IUSA" else "nqse"
            return [
                CandidateSearchResult(
                    title=f"{profile.fund_name} factsheet",
                    url=f"https://www.ishares.com/{slug}/factsheet",
                    snippet=f"{profile.fund_name} holdings expense ratio",
                    discovered_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
                    provider=self.provider_name,
                    query_id=f"{category}:1",
                    query=query,
                    category=category,
                )
            ]

    provider = RecordingEtfSearchProvider()
    search = SearchDiscoveryService(provider, max_documents_per_refresh=4)
    repo = ResearchRepository(
        settings=Settings(research_live_enabled=True, research_search_enabled=True, research_demo_enabled=True, research_max_retries=0),
        search_discovery=search,
    )
    iusa = _portfolio_position(
        "IE0031442068",
        "IUSA",
        "AEB",
        "ISHARES CORE S&P 500",
        provider="IBKR",
        provider_instrument_id="IUSA-CONID",
        asset_type="ETF",
        data_freshness="REAL_BROKER",
        canonical_symbol="IUSA",
        canonical_name="ISHARES CORE S&P 500",
        canonical_exchange="XAMS",
        canonical_mic="XAMS",
        security_type="ETF",
    )
    nqse = _portfolio_position(
        "IE00B53SZB19",
        "NQSE",
        "IBIS2",
        "ISHARES NASDAQ 100 EUR-H ACC",
        provider="IBKR",
        provider_instrument_id="NQSE-CONID",
        asset_type="ETF",
        data_freshness="REAL_BROKER",
        canonical_symbol="NQSE",
        canonical_name="ISHARES NASDAQ 100 EUR-H ACC",
        canonical_exchange="XETR",
        canonical_mic="XETR",
        security_type="ETF",
    )
    respx.get("https://www.ishares.com/iusa/factsheet").mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<html><title>ISHARES CORE S&P 500 factsheet</title><main><p>iShares ETF factsheet tracks the S&P 500. Expense ratio 0.07%. Holdings 503. AUM USD 85bn. Top holdings Apple Microsoft NVIDIA.</p></main></html>",
        )
    )
    respx.get("https://www.ishares.com/nqse/factsheet").mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<html><title>ISHARES NASDAQ 100 EUR-H ACC factsheet</title><main><p>iShares ETF factsheet tracks the NASDAQ 100. Total expense ratio 0.33%. Holdings 101. Accumulating. Top holdings Apple Microsoft NVIDIA.</p></main></html>",
        )
    )
    client = _RecordingPortfolioClient([iusa, nqse])
    orchestrator = PortfolioResearchOrchestrator(
        repo,
        Settings(portfolio_service_base_url="http://portfolio-service", research_live_enabled=True, research_search_enabled=True),
        client=client,
    )

    result = await orchestrator.refresh_portfolio(UUID("aaaaaaaa-1111-1111-1111-aaaaaaaaaaaa"))

    by_ticker = {company.ticker: company for company in result.companies}
    assert by_ticker["IUSA"].status == "ETF_UNSUPPORTED"
    assert by_ticker["IUSA"].etf_profile is not None
    assert by_ticker["IUSA"].etf_profile.underlying_index == "S&P 500"
    assert by_ticker["IUSA"].etf_profile.facts["expenseRatio"].value == "0.07%"
    assert by_ticker["NQSE"].status == "ETF_UNSUPPORTED"
    assert by_ticker["NQSE"].etf_profile is not None
    assert by_ticker["NQSE"].etf_profile.underlying_index == "NASDAQ 100"
    assert by_ticker["NQSE"].etf_profile.facts["distributionPolicy"].source_url == "https://www.ishares.com/nqse/factsheet"
    assert "ISHARES CORE S&P 500" in provider.names
    assert "ISHARES NASDAQ 100 EUR-H ACC" in provider.names
    assert all(not query.startswith(("IUSA ", "NQSE ")) for query in provider.queries)


@pytest.mark.asyncio
async def test_etf_read_does_not_call_search_or_mutate_research_db(tmp_path) -> None:
    class FailingSearchDiscovery:
        async def discover(self, company, missing, seen_urls):
            raise AssertionError("search provider must not be called during ETF read")

    class CountingPersistence(SqliteResearchPersistence):
        def __init__(self, database_path):
            super().__init__(database_path)
            self.write_count = 0

        def upsert_document(self, document):
            self.write_count += 1
            return super().upsert_document(document)

        def start_refresh_run(self, **kwargs):
            self.write_count += 1
            return super().start_refresh_run(**kwargs)

        def complete_refresh_run(self, run, **kwargs):
            self.write_count += 1
            return super().complete_refresh_run(run, **kwargs)

    persistence = CountingPersistence(tmp_path / "research.sqlite")
    repo = ResearchRepository(
        settings=Settings(research_live_enabled=True, research_search_enabled=True, research_demo_enabled=True),
        search_discovery=FailingSearchDiscovery(),
        persistence=persistence,
    )
    profile = repo.register_etf_profile(
        repo_etf_profile(
            "IE0031442068",
            "IUSA",
            "XAMS",
            "ISHARES CORE S&P 500",
            "IBKR",
            "IUSA-CONID",
        )
    )
    repo.ingest_etf_fixture(
        profile,
        original_url="https://www.ishares.com/iusa/factsheet",
        source_type=SourceType.INVESTOR_RELATIONS,
        source_classification=SourceClassification.OFFICIAL_COMPANY,
        source_name="iShares factsheet",
        publisher="iShares",
        content_type="text/html",
        body="<html><title>ISHARES CORE S&P 500 factsheet</title><main><p>iShares ETF factsheet tracks the S&P 500. Expense ratio 0.07%. Holdings 503.</p></main></html>",
        reliability=ReliabilityLevel.LEVEL_B,
    )
    persistence.write_count = 0
    client = _RecordingPortfolioClient([
        _portfolio_position(
            "IE0031442068",
            "IUSA",
            "AEB",
            "ISHARES CORE S&P 500",
            provider="IBKR",
            provider_instrument_id="IUSA-CONID",
            asset_type="ETF",
            data_freshness="REAL_BROKER",
            canonical_symbol="IUSA",
            canonical_name="ISHARES CORE S&P 500",
            canonical_exchange="XAMS",
            canonical_mic="XAMS",
            security_type="ETF",
        )
    ])
    orchestrator = PortfolioResearchOrchestrator(repo, Settings(portfolio_service_base_url="http://portfolio-service"), client=client)

    result = await orchestrator.read_portfolio_summary(UUID("aaaaaaaa-1111-1111-1111-aaaaaaaaaaaa"))

    assert result.companies[0].status == "ETF_UNSUPPORTED"
    assert persistence.write_count == 0


@pytest.mark.asyncio
async def test_portfolio_research_refresh_uses_canonical_company_name_for_search() -> None:
    class SearchStats:
        documents_fetched = 0
        events_extracted = 0

        def reject(self, reason: str) -> None:
            pass

    class RecordingSearch:
        def __init__(self) -> None:
            self.last_stats = SearchStats()
            self.company_names: list[str] = []

        async def discover(self, profile, missing, seen_urls):
            self.company_names.append(profile.company_name)
            return []

    search = RecordingSearch()
    repo = ResearchRepository(
        settings=Settings(research_live_enabled=True, research_search_enabled=True, research_demo_enabled=False),
        search_discovery=search,
    )
    client = _RecordingPortfolioClient([
        _portfolio_position(
            "DE000RENK730",
            "R3NK",
            "UNKNOWN",
            "R3NK",
            provider="IBKR",
            provider_instrument_id="345678",
            asset_type="EQUITY",
            data_freshness="REAL_BROKER",
            canonical_symbol="R3NK",
            canonical_name="RENK Group AG",
            canonical_exchange="XETR",
            canonical_mic="XETR",
            security_type="STK",
        )
    ])
    orchestrator = PortfolioResearchOrchestrator(
        repo,
        Settings(portfolio_service_base_url="http://portfolio-service", research_live_enabled=True, research_search_enabled=True),
        client=client,
    )

    result = await orchestrator.refresh_portfolio(UUID("aaaaaaaa-1111-1111-1111-aaaaaaaaaaaa"))

    assert result.companies[0].ticker == "R3NK"
    assert "RENK Group AG" in search.company_names
    assert "R3NK" not in search.company_names


@pytest.mark.parametrize(
    ("display_name", "expected"),
    [
        ("TALAUT / NSE / TALBROS AUTOMOTIVE COMPONENTS", "TALBROS AUTOMOTIVE COMPONENTS"),
        ("TALAUT / NSE / TALBROS AUTOMOTIVE COMPONENTS / EXTRA", "TALBROS AUTOMOTIVE COMPONENTS"),
    ],
)
def test_legacy_holding_display_name_uses_only_company_segment(display_name: str, expected: str) -> None:
    instrument = {"ticker": "TALAUT", "companyName": display_name}

    assert _instrument_name(instrument) == expected
    assert instrument["ticker"] == "TALAUT"


def test_research_company_name_priority_preserves_identity_fields() -> None:
    instrument = {
        "instrumentId": "11111111-1111-1111-1111-111111111111",
        "provider": "HDFC_SECURITIES",
        "providerInstrumentId": "SYMBOL:TALAUT",
        "isin": None,
        "ticker": "TALAUT",
        "exchange": "NSE",
        "companyName": "Structured Company Limited",
        "canonicalName": "Trusted Resolved Company Limited",
        "_researchDisplayName": "TALAUT / NSE / Legacy Company Limited / EXTRA",
        "_researchCustomDisplayName": "  User Company Limited  ",
    }
    identity_before = {key: instrument[key] for key in (
        "instrumentId", "provider", "providerInstrumentId", "isin", "ticker", "exchange"
    )}

    assert _instrument_name(instrument) == "User Company Limited"
    instrument["_researchCustomDisplayName"] = None
    assert _instrument_name(instrument) == "Structured Company Limited"
    instrument["companyName"] = "TALAUT / NSE / Legacy Company Limited / EXTRA"
    assert _instrument_name(instrument) == "Trusted Resolved Company Limited"
    instrument["canonicalName"] = None
    assert _instrument_name(instrument) == "Legacy Company Limited"
    assert {key: instrument[key] for key in identity_before} == identity_before


@pytest.mark.asyncio
async def test_portfolio_research_search_receives_clean_legacy_company_name() -> None:
    class SearchStats:
        documents_fetched = 0
        events_extracted = 0

        def reject(self, reason: str) -> None:
            pass

    class RecordingSearch:
        def __init__(self) -> None:
            self.last_stats = SearchStats()
            self.company_names: list[str] = []

        async def discover(self, profile, missing, seen_urls):
            self.company_names.append(profile.company_name)
            return []

    search = RecordingSearch()
    repo = ResearchRepository(
        settings=Settings(research_live_enabled=True, research_search_enabled=True, research_demo_enabled=False),
        search_discovery=search,
    )
    position = _portfolio_position(
        None,
        "TALAUT",
        "NSE",
        "TALAUT / NSE / TALBROS AUTOMOTIVE COMPONENTS / EXTRA",
        provider="HDFC_SECURITIES",
        provider_instrument_id="SYMBOL:TALAUT",
        data_freshness="REAL_BROKER",
    )
    identity_before = dict(position["instrument"])
    orchestrator = PortfolioResearchOrchestrator(
        repo,
        Settings(portfolio_service_base_url="http://portfolio-service", research_live_enabled=True, research_search_enabled=True),
        client=_RecordingPortfolioClient([position]),
    )

    await orchestrator.refresh_portfolio(UUID("aaaaaaaa-1111-1111-1111-aaaaaaaaaaaa"))

    assert search.company_names == ["TALBROS AUTOMOTIVE COMPONENTS"]
    assert position["instrument"] == identity_before


@pytest.mark.asyncio
async def test_portfolio_research_read_does_not_call_search_provider_or_mutate_research_db(tmp_path) -> None:
    class FailingSearchDiscovery:
        async def discover(self, company, missing, seen_urls):
            raise AssertionError("search provider must not be called during read")

    class CountingPersistence(SqliteResearchPersistence):
        def __init__(self, database_path):
            super().__init__(database_path)
            self.write_count = 0

        def upsert_document(self, document):
            self.write_count += 1
            return super().upsert_document(document)

        def upsert_event(self, event):
            self.write_count += 1
            return super().upsert_event(event)

        def start_refresh_run(self, **kwargs):
            self.write_count += 1
            return super().start_refresh_run(**kwargs)

        def complete_refresh_run(self, run, **kwargs):
            self.write_count += 1
            return super().complete_refresh_run(run, **kwargs)

    persistence = CountingPersistence(tmp_path / "research.sqlite")
    repo = ResearchRepository(
        settings=Settings(research_live_enabled=True, research_demo_enabled=True),
        search_discovery=FailingSearchDiscovery(),
        persistence=persistence,
    )
    repo.ingest_fixture(
        original_url="https://www.besi.com/investor-relations/press-releases/details/read-only",
        source_type=SourceType.INVESTOR_RELATIONS,
        source_classification=SourceClassification.OFFICIAL_COMPANY,
        source_name="BESI official press release",
        publisher="BE Semiconductor Industries",
        content_type="text/html",
        body=(
            "<html><title>BESI backlog</title><body>"
            "BE Semiconductor Industries BESI XAMS announced a new customer order and capacity expansion."
            "</body></html>"
        ),
        reliability=ReliabilityLevel.LEVEL_B,
        source_mode=SourceMode.REAL,
    )
    persistence.write_count = 0
    client = _RecordingPortfolioClient([
        _portfolio_position("NL0012866412", "BESI", "XAMS", "BE Semiconductor Industries", data_freshness="REAL_BROKER")
    ])
    orchestrator = PortfolioResearchOrchestrator(repo, Settings(portfolio_service_base_url="http://portfolio-service"), client=client)

    result = await orchestrator.read_portfolio_summary(UUID("aaaaaaaa-1111-1111-1111-aaaaaaaaaaaa"))

    assert result.companies[0].status == "RESOLVED_RESEARCH_AVAILABLE"
    assert persistence.write_count == 0


@pytest.mark.asyncio
async def test_portfolio_research_read_preserves_user_identity_headers_for_isolation() -> None:
    repo = ResearchRepository(settings=Settings(research_live_enabled=False, research_demo_enabled=False))
    client = _RecordingPortfolioClient([])
    orchestrator = PortfolioResearchOrchestrator(repo, Settings(portfolio_service_base_url="http://portfolio-service"), client=client)

    await orchestrator.read_portfolio_summary(
        UUID("bbbbbbbb-1111-1111-1111-bbbbbbbbbbbb"),
        identity_headers={"X-AIP-User-Id": "user-b", "X-AIP-User-Email": "b@example.test"},
    )

    assert client.calls[0]["headers"]["X-AIP-User-Id"] == "user-b"
    assert client.calls[0]["headers"]["X-AIP-User-Email"] == "b@example.test"


@pytest.mark.asyncio
async def test_real_broker_portfolio_research_does_not_use_demo_fallback() -> None:
    repo = ResearchRepository(settings=Settings(research_live_enabled=False, research_demo_enabled=True))
    repo.profiles[0].provider_instrument_ids["IBKR"] = "AIXTRON-CONID"
    client = _RecordingPortfolioClient([
        _portfolio_position(
            None,
            "AIXA",
            "IBIS2",
            "AIXA",
            provider="IBKR",
            provider_instrument_id="AIXTRON-CONID",
            data_freshness="REAL_BROKER",
        ),
    ])
    orchestrator = PortfolioResearchOrchestrator(repo, Settings(portfolio_service_base_url="http://portfolio-service"), client=client,
                                                 structured_provider=_UnavailableStructuredProvider())

    result = await orchestrator.refresh_portfolio(UUID("aaaaaaaa-1111-1111-1111-aaaaaaaaaaaa"))

    assert result.companies[0].status == "RESOLVED_NO_SOURCES"
    assert result.companies[0].freshness == "UNAVAILABLE"
    assert result.companies[0].mode == "UNAVAILABLE"
    assert result.companies[0].document_count == 0
    assert result.companies[0].event_count == 0


@pytest.mark.asyncio
async def test_portfolio_research_registers_canonical_company_from_resolved_real_equity_metadata() -> None:
    repo = ResearchRepository(settings=Settings(research_live_enabled=False, research_demo_enabled=True))
    client = _RecordingPortfolioClient([
        _portfolio_position(
            "NL0006237562",
            "ARCAD",
            "XAMS",
            "Arcadis NV",
            provider="IBKR",
            provider_instrument_id="ARCAD-CONID",
            data_freshness="REAL_BROKER",
        ),
        _portfolio_position(
            "NL0000852564",
            "AALB",
            "AEB",
            "Aalberts N.V.",
            provider="IBKR",
            provider_instrument_id="AALB-CONID",
            data_freshness="REAL_BROKER",
        ),
    ])
    orchestrator = PortfolioResearchOrchestrator(
        repo, Settings(portfolio_service_base_url="http://portfolio-service"), client=client,
        structured_provider=_UnavailableStructuredProvider(),
    )

    result = await orchestrator.refresh_portfolio(UUID("aaaaaaaa-1111-1111-1111-aaaaaaaaaaaa"))

    by_ticker = {company.ticker: company for company in result.companies}
    assert by_ticker["ARCAD"].status == "RESOLVED_NO_SOURCES"
    assert by_ticker["ARCAD"].company_name == "Arcadis NV"
    assert by_ticker["ARCAD"].provider_instrument_id == "ARCAD-CONID"
    assert by_ticker["AALB"].status == "RESOLVED_NO_SOURCES"
    assert by_ticker["AALB"].exchange == "XAMS"
    assert by_ticker["AALB"].company_name == "Aalberts N.V."
    assert {profile.ticker for profile in repo.list_profiles()} >= {"ARCAD", "AALB"}


@pytest.mark.asyncio
@respx.mock
async def test_portfolio_research_dynamically_discovers_sources_for_unregistered_equities() -> None:
    class DynamicOfficialSearchProvider:
        provider_name = "test-search"

        def __init__(self) -> None:
            self.company_names: list[str] = []

        async def discover(self, company, category: str, date_window: SearchDateWindow) -> list[CandidateSearchResult]:
            self.company_names.append(company.company_name)
            if category != "FINANCIAL_RESULTS":
                return []
            domain = _company_fixture_domain(company.company_name)
            return [
                CandidateSearchResult(
                    title=f"{company.company_name} investor relations quarterly results",
                    url=f"https://www.{domain}.com/investor-relations/results",
                    snippet=f"{company.company_name} annual report, quarterly results and earnings",
                    discovered_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
                    provider=self.provider_name,
                    query_id=f"{category}:1",
                    query=f"{company.company_name} annual report",
                    category=category,
                )
            ]

    provider = DynamicOfficialSearchProvider()
    search = SearchDiscoveryService(provider, max_documents_per_refresh=8)
    repo = ResearchRepository(
        settings=Settings(research_live_enabled=True, research_search_enabled=True, research_demo_enabled=True, research_max_retries=0),
        discovery=_StaticDiscovery([]),
        search_discovery=search,
    )
    positions = [
        _portfolio_position(
            "DE000RENK730",
            "R3NK",
            "UNKNOWN",
            "R3NK",
            provider="IBKR",
            provider_instrument_id="RENK-CONID",
            asset_type="EQUITY",
            data_freshness="REAL_BROKER",
            canonical_symbol="R3NK",
            canonical_name="RENK Group AG",
            canonical_exchange="XETR",
            canonical_mic="XETR",
            security_type="STK",
        ),
        _portfolio_position(
            "NL0006237562",
            "ARCAD",
            "XAMS",
            "Arcadis NV",
            provider="IBKR",
            provider_instrument_id="ARCAD-CONID",
            asset_type="EQUITY",
            data_freshness="REAL_BROKER",
        ),
        _portfolio_position(
            "NL0000852564",
            "AALB",
            "AEB",
            "Aalberts N.V.",
            provider="IBKR",
            provider_instrument_id="AALB-CONID",
            asset_type="EQUITY",
            data_freshness="REAL_BROKER",
        ),
        _portfolio_position(
            None,
            "SYNTH",
            "XAMS",
            "SynthAlpha Technologies PLC",
            provider="IBKR",
            provider_instrument_id="SYNTH-CONID",
            asset_type="EQUITY",
            data_freshness="REAL_BROKER",
        ),
    ]
    for position in positions:
        instrument = position["instrument"]
        company_name = _instrument_fixture_name(instrument)
        ticker = instrument.get("canonicalSymbol") or instrument.get("ticker")
        exchange = instrument.get("canonicalExchange") or instrument.get("exchange")
        domain = _company_fixture_domain(company_name)
        respx.get(f"https://www.{domain}.com/investor-relations/results").mock(
            return_value=httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text=(
                    f"<html><title>{company_name} quarterly results</title><main>"
                    f"<p>May 2, 2026</p><p>{company_name} {ticker} {exchange} reported quarterly results "
                    "with earnings, order intake growth and guidance for the full year.</p></main></html>"
                ),
            )
        )
    client = _RecordingPortfolioClient(positions)
    orchestrator = PortfolioResearchOrchestrator(
        repo,
        Settings(portfolio_service_base_url="http://portfolio-service", research_live_enabled=True, research_search_enabled=True),
        client=client,
    )

    result = await orchestrator.refresh_portfolio(UUID("aaaaaaaa-1111-1111-1111-aaaaaaaaaaaa"))

    by_ticker = {company.ticker: company for company in result.companies}
    for ticker in ["R3NK", "ARCAD", "AALB", "SYNTH"]:
        company = by_ticker[ticker]
        assert company.status == "RESOLVED_RESEARCH_AVAILABLE"
        assert company.document_count > 0
        assert company.source_count > 0
        assert company.freshness == "REAL"
        assert company.mode == "REAL"
        assert company.safe_error_code is None
    assert search.last_stats.documents_fetched > 0
    assert {profile.ticker for profile in repo.list_profiles()} >= {"R3NK", "ARCAD", "AALB", "SYNTH"}
    assert all(document.source_classification == SourceClassification.OFFICIAL_COMPANY for document in repo.documents.values() if document.source_mode == SourceMode.REAL)
    assert "RENK Group AG" in provider.company_names
    assert "SynthAlpha Technologies PLC" in provider.company_names


@pytest.mark.asyncio
async def test_google_provider_unavailable_maps_to_portfolio_research_provider_unavailable() -> None:
    class GoogleUnavailableRepository(ResearchRepository):
        async def refresh(self, instrument_id: UUID, correlation_id: str | None = None, **kwargs):
            self.last_live_error[instrument_id] = "GOOGLE_PROVIDER_UNAVAILABLE"
            return self.summary(instrument_id, allow_demo=False)

    repo = GoogleUnavailableRepository(settings=Settings(research_live_enabled=True, research_demo_enabled=True))
    repo.profiles[0].provider_instrument_ids["IBKR"] = "AIXTRON-CONID"
    client = _RecordingPortfolioClient([
        _portfolio_position(
            None,
            "AIXA",
            "IBIS2",
            "AIXA",
            provider="IBKR",
            provider_instrument_id="AIXTRON-CONID",
            data_freshness="REAL_BROKER",
        ),
    ])
    orchestrator = PortfolioResearchOrchestrator(repo, Settings(portfolio_service_base_url="http://portfolio-service"), client=client,
                                                 structured_provider=_UnavailableStructuredProvider())

    result = await orchestrator.refresh_portfolio(UUID("aaaaaaaa-1111-1111-1111-aaaaaaaaaaaa"))

    assert result.companies[0].status == "SEARCH_PROVIDER_UNAVAILABLE"
    assert result.companies[0].safe_error_code == "GOOGLE_PROVIDER_UNAVAILABLE"
    assert result.companies[0].mode == "SOURCE_UNAVAILABLE"


@pytest.mark.asyncio
async def test_real_broker_portfolio_research_never_uses_demo_fallback_when_real_search_fails() -> None:
    class SearchRejectedRepository(ResearchRepository):
        async def refresh(self, instrument_id: UUID, correlation_id: str | None = None, **kwargs):
            self.last_live_error[instrument_id] = "SEARCH_SOURCE_UNAVAILABLE:source:COMPANY_RELEVANCE_FAILED"
            return self.summary(instrument_id, allow_demo=False)

    repo = SearchRejectedRepository(settings=Settings(research_live_enabled=True, research_demo_enabled=True))
    repo.profiles[1].provider_instrument_ids["IBKR"] = "BESI-CONID"
    client = _RecordingPortfolioClient([
        _portfolio_position(
            None,
            "BESI",
            "AEB",
            "BE Semiconductor Industries N.V.",
            provider="IBKR",
            provider_instrument_id="BESI-CONID",
            data_freshness="REAL_BROKER",
        ),
    ])
    orchestrator = PortfolioResearchOrchestrator(
        repo,
        Settings(
            portfolio_service_base_url="http://portfolio-service",
            research_live_enabled=True,
            research_search_enabled=True,
        ),
        client=client,
        structured_provider=_UnavailableStructuredProvider(),
    )

    result = await orchestrator.refresh_portfolio(UUID("aaaaaaaa-1111-1111-1111-aaaaaaaaaaaa"))

    company = result.companies[0]
    assert company.status == "DOCUMENT_FETCH_FAILED"
    assert company.mode == "SOURCE_UNAVAILABLE"
    assert company.freshness == "SOURCE_UNAVAILABLE"
    assert company.safe_error_code == "SEARCH_SOURCE_UNAVAILABLE:source:COMPANY_RELEVANCE_FAILED"
    assert company.document_count == 0
    assert company.event_count == 0


@pytest.mark.asyncio
async def test_portfolio_research_partial_failure_does_not_fail_entire_response() -> None:
    class FailingRefreshRepository(ResearchRepository):
        async def refresh(self, instrument_id: UUID, correlation_id: str | None = None, **kwargs):
            raise FetchError("controlled failure")

    repo = FailingRefreshRepository(settings=Settings(research_live_enabled=False, research_demo_enabled=False))
    client = _RecordingPortfolioClient([_portfolio_position("DE000A0WMPJ6", "AIXA", "XETR", "AIXTRON SE")])
    orchestrator = PortfolioResearchOrchestrator(repo, Settings(portfolio_service_base_url="http://portfolio-service"), client=client,
                                                 structured_provider=_UnavailableStructuredProvider())

    result = await orchestrator.refresh_portfolio(UUID("aaaaaaaa-1111-1111-1111-aaaaaaaaaaaa"), correlation_id="phase4")

    assert result.companies_degraded == 1
    assert result.companies[0].status == "SEARCH_PROVIDER_UNAVAILABLE"
    assert result.companies[0].safe_error_code == "FetchError"


def test_positive_and_negative_sources_show_mixed_evidence() -> None:
    repo = ResearchRepository()
    repo.ingest_fixture(
        original_url="https://www.aixtron.com/en/customer-win",
        source_type=SourceType.INVESTOR_RELATIONS,
        source_classification=SourceClassification.OFFICIAL_COMPANY,
        source_name="AIXTRON",
        publisher="AIXTRON SE",
        content_type="text/html",
        body="<html><title>AIXTRON customer</title><body>AIXTRON SE AIXA XETR announced a customer win with Infineon Technologies AG.</body></html>",
        reliability=ReliabilityLevel.LEVEL_B,
        source_mode=SourceMode.REAL,
    )
    repo.ingest_fixture(
        original_url="https://www.reuters.com/markets/aixtron-customer-loss",
        source_type=SourceType.NEWS,
        source_classification=SourceClassification.REPUTABLE_NEWS,
        source_name="Reuters",
        publisher="Reuters",
        content_type="text/html",
        body="<html><title>AIXTRON customer loss</title><body>AIXTRON SE AIXA XETR reported a customer loss in silicon-carbide tools.</body></html>",
        reliability=ReliabilityLevel.LEVEL_C,
        source_mode=SourceMode.REAL,
    )

    evidence = repo.summary(AIXTRON_INSTRUMENT_ID).catalyst_score.category_evidence["Customers"]

    assert evidence.status == "MIXED_EVIDENCE"
    assert evidence.has_conflict is True


@pytest.mark.asyncio
@respx.mock
async def test_live_aixtron_refresh_fetches_registered_official_source_and_preserves_provenance() -> None:
    settings = Settings(research_live_enabled=True, research_demo_enabled=True, research_max_retries=0)
    repo = ResearchRepository(settings=settings)
    source = registered_sources_for(AIXTRON_INSTRUMENT_ID)[0]
    respx.get(source.url).mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text=_aixtron_live_fixture(),
        )
    )

    summary = await repo.refresh(AIXTRON_INSTRUMENT_ID)

    assert summary.demo is False
    assert summary.data_freshness == "REAL"
    assert summary.documents[0].source_mode == SourceMode.REAL
    assert summary.documents[0].source_name == "AIXTRON official press release"
    assert summary.documents[0].published_at == datetime(2026, 4, 14, tzinfo=timezone.utc)
    assert summary.documents[0].content_hash
    assert any(event.source_mode == SourceMode.REAL and event.reliability == ReliabilityLevel.LEVEL_B for event in summary.recent_events)
    assert any(event.event_type == "NEW_ORDER" and event.confidence > 0.8 for event in summary.recent_events)


@pytest.mark.asyncio
@respx.mock
async def test_live_refresh_is_idempotent_for_documents_events_and_scores() -> None:
    settings = Settings(research_live_enabled=True, research_demo_enabled=True, research_max_retries=0)
    repo = ResearchRepository(settings=settings)
    source = registered_sources_for(AIXTRON_INSTRUMENT_ID)[0]
    respx.get(source.url).mock(
        return_value=httpx.Response(200, headers={"content-type": "text/html"}, text=_aixtron_live_fixture())
    )

    first = await repo.refresh(AIXTRON_INSTRUMENT_ID)
    second = await repo.refresh(AIXTRON_INSTRUMENT_ID)

    assert len(repo.documents_for(AIXTRON_INSTRUMENT_ID, source_mode=SourceMode.REAL)) == len(first.documents) == len(second.documents)
    assert len(repo.events_for(AIXTRON_INSTRUMENT_ID, source_mode=SourceMode.REAL)) == len(first.recent_events) == len(second.recent_events)
    assert first.catalyst_score.overall_score == second.catalyst_score.overall_score


def test_registered_source_rejects_unapproved_host() -> None:
    repo = ResearchRepository()
    profile = repo.profile(AIXTRON_INSTRUMENT_ID)
    source = RegisteredResearchSource(
        source_id="bad",
        instrument_id=AIXTRON_INSTRUMENT_ID,
        url="https://example.com/aixtron",
        source_type=SourceType.INVESTOR_RELATIONS,
        source_name="Bad",
        publisher="Bad",
        reliability_level=ReliabilityLevel.LEVEL_C,
    )
    with pytest.raises(FetchError):
        repo._validate_registered_source(profile, source)


@pytest.mark.asyncio
@respx.mock
async def test_http_fetch_validates_content_type_retries_and_size() -> None:
    settings = Settings(research_max_content_bytes=50, research_max_retries=1)
    fetcher = HttpResearchFetcher(settings)
    route = respx.get("https://example.com/release").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(200, headers={"content-type": "text/html"}, text="<html>ok</html>"),
        ]
    )
    result = await fetcher.fetch("https://example.com/release")
    assert route.call_count == 2
    assert result.content_type == "text/html"


@pytest.mark.asyncio
@respx.mock
async def test_http_fetch_timeout_raises_structured_error() -> None:
    settings = Settings(research_max_retries=0)
    fetcher = HttpResearchFetcher(settings)
    respx.get("https://example.com/timeout").mock(side_effect=httpx.TimeoutException("timeout"))
    with pytest.raises(FetchError, match="timed out"):
        await fetcher.fetch("https://example.com/timeout")


@pytest.mark.asyncio
@respx.mock
async def test_http_fetch_404_raises_structured_error() -> None:
    fetcher = HttpResearchFetcher(Settings(research_max_retries=0))
    respx.get("https://example.com/missing").mock(return_value=httpx.Response(404, headers={"content-type": "text/html"}))
    with pytest.raises(FetchError, match="404"):
        await fetcher.fetch("https://example.com/missing")


@pytest.mark.asyncio
@respx.mock
async def test_http_fetch_rejects_unsupported_content_type() -> None:
    fetcher = HttpResearchFetcher(Settings())
    respx.get("https://example.com/data").mock(return_value=httpx.Response(200, headers={"content-type": "application/json"}, json={"ok": True}))
    with pytest.raises(FetchError, match="Unsupported content type"):
        await fetcher.fetch("https://example.com/data")


@pytest.mark.asyncio
@respx.mock
async def test_http_fetch_rejects_oversize_response() -> None:
    fetcher = HttpResearchFetcher(Settings(research_max_content_bytes=8))
    respx.get("https://example.com/large").mock(return_value=httpx.Response(200, headers={"content-type": "text/html"}, text="0123456789"))
    with pytest.raises(FetchError, match="Maximum content size exceeded"):
        await fetcher.fetch("https://example.com/large")


@pytest.mark.asyncio
@respx.mock
async def test_http_fetch_does_not_retry_restricted_sources() -> None:
    settings = Settings(research_max_retries=2)
    fetcher = HttpResearchFetcher(settings)
    route = respx.get("https://example.com/private").mock(return_value=httpx.Response(403))
    with pytest.raises(RestrictedFetchError):
        await fetcher.fetch("https://example.com/private")
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_http_fetch_follows_safe_public_redirect() -> None:
    fetcher = HttpResearchFetcher(Settings(research_max_retries=0))
    start = "https://nsearchives.nseindia.com/corporate/start.pdf"
    final = "https://nsearchives.nseindia.com/corporate/final.html"
    respx.get(start).mock(return_value=httpx.Response(302, headers={"location": final}))
    respx.get(final).mock(return_value=httpx.Response(200, headers={"content-type": "text/html"}, text="<html>financial results</html>"))

    result = await fetcher.fetch(start)

    assert result.final_url == final


@pytest.mark.asyncio
@respx.mock
async def test_http_fetch_rejects_redirect_to_private_target() -> None:
    fetcher = HttpResearchFetcher(Settings(research_max_retries=0))
    start = "https://nsearchives.nseindia.com/corporate/start.pdf"
    respx.get(start).mock(return_value=httpx.Response(302, headers={"location": "http://127.0.0.1/private.pdf"}))

    with pytest.raises(UnsafeUrlError):
        await fetcher.fetch(start)


def test_india_equity_queries_are_exchange_first_and_remain_publicly_broad() -> None:
    profiles = [
        next(profile for profile in ResearchRepository().profiles if profile.ticker == "RELIANCE"),
        CompanyResearchProfile(
            instrument_id=UUID("77777777-7777-7777-7777-777777777777"),
            company_id=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa7"),
            company_name="Bharat Electronics Limited",
            aliases=["BEL"], isin="INE263A01024", ticker="BEL", exchange="XNSE", mic="XNSE",
            country="IN", currency="INR", known_domains=["bel-india.in"],
        ),
    ]
    for profile in profiles:
        queries = generate_search_queries(profile, "FINANCIAL_RESULTS", SearchDateWindow())
        assert "site:nseindia.com" in queries[0]
        assert "site:bseindia.com" in queries[1]
        assert any(profile.isin in query for query in queries)
        assert any(profile.company_name in query and "site:" not in query for query in queries[:6])


def test_latest_quarter_result_extracts_only_verified_metrics_with_provenance() -> None:
    document = _document(
        "https://www.bseindia.com/xml-data/corpfiling/q1.pdf",
        "Q1 FY27 revenue 1,250 revenue YoY +18% EBITDA 240 EBITDA margin 19.2% PAT 125 PAT YoY +24% EPS 3.2",
    ).model_copy(update={"source_classification": SourceClassification.EXCHANGE, "source_name": "BSE"})
    result = latest_quarterly_result([document])
    assert result is not None
    assert result.period == "Q1 FY27"
    assert result.revenue.value == Decimal("1250")
    assert result.pat_yoy_percent.value == Decimal("24")
    assert result.revenue.source_url == document.canonical_url
    assert result.revenue_qoq_percent is None
    assert result.document_title == document.title
    assert result.yoy_summary == "Revenue 18% YoY; PAT 24% YoY"


def test_latest_quarter_result_selects_latest_fiscal_period_not_latest_retrieval() -> None:
    older_period = _document("https://www.nseindia.com/q4.pdf", "Q4 FY26 revenue 900 PAT 90 EPS 2")
    latest_period = _document("https://www.nseindia.com/q1.pdf", "Q1 FY27 revenue 1,000 PAT 110 EPS 2.5")
    older_period = older_period.model_copy(update={"retrieved_at": datetime(2026, 8, 1, tzinfo=timezone.utc)})
    latest_period = latest_period.model_copy(update={"retrieved_at": datetime(2026, 7, 1, tzinfo=timezone.utc)})
    result = latest_quarterly_result([older_period, latest_period])
    assert result is not None
    assert result.period == "Q1 FY27"
    assert result.revenue.value == Decimal("1000")


def test_latest_quarter_prefers_consolidated_for_same_fiscal_period_and_preserves_unit() -> None:
    standalone = _document("https://www.nseindia.com/standalone.pdf", "Q1 FY27 standalone revenue 120 crore PAT 12 EPS 1")
    consolidated = _document("https://www.nseindia.com/consolidated.pdf", "Q1 FY27 consolidated revenue 150 crore PAT 15 EPS 1.2")
    result = latest_quarterly_result([standalone, consolidated])
    assert result is not None
    assert result.reporting_basis == "CONSOLIDATED"
    assert result.revenue.value == Decimal("150")
    assert result.revenue.unit == "crore"


def test_official_pdf_filing_candidate_ranks_above_ir_landing_page() -> None:
    profile = next(profile for profile in ResearchRepository().profiles if profile.ticker == "RELIANCE")
    landing = CandidateSearchResult("Investor relations", "https://www.ril.com/investors", "Investor relations home", datetime.now(timezone.utc), "searxng", "1", "q", "FINANCIAL_RESULTS")
    filing = CandidateSearchResult("Quarterly Financial Results Q1 FY27", "https://www.nseindia.com/results/q1.pdf", "Unaudited financial results", datetime.now(timezone.utc), "searxng", "2", "q", "FINANCIAL_RESULTS")
    assert _candidate_rank(profile, filing) > _candidate_rank(profile, landing)


@pytest.mark.parametrize(("metadata", "expected"), [
    (("Investor Presentation Q1", "", "q1-investor-presentation.pdf"), DocumentSubtype.INVESTOR_PRESENTATION),
    (("AGM Presentation", "", "agm-presentation.pdf"), None),
    (("Presentation", "", "presentation.pdf"), None),
    (("Press Release", "Business update", "release.pdf"), DocumentSubtype.INVESTOR_RELEASE),
    (("Compliance notice", "", "notice.pdf"), None),
    (("", "Conference Call Transcript", "transcript.pdf"), DocumentSubtype.CONFERENCE_CALL_MATERIAL),
    (("", "Earnings Call Presentation", "concall.pdf"), DocumentSubtype.CONFERENCE_CALL_MATERIAL),
    (("Conference Call Invitation", "", "invite.pdf"), None),
    (("Receipt of Order", "", "order.pdf"), DocumentSubtype.ORDER_CONTRACT_DISCLOSURE),
    (("Letter of Award", "", "loa.pdf"), DocumentSubtype.ORDER_CONTRACT_DISCLOSURE),
    (("Order update", "", "order.pdf"), None),
    (("Award received", "", "award.pdf"), None),
    (("Capacity expansion", "", "expansion.pdf"), DocumentSubtype.CAPEX_CAPACITY_DISCLOSURE),
    (("New manufacturing facility", "", "facility.pdf"), DocumentSubtype.CAPEX_CAPACITY_DISCLOSURE),
    (("Installed capacity", "", "results.pdf"), None),
    (("", "", "ordinary-attachment.pdf"), None),
])
def test_nse_document_subtype_classifier_is_conservative(metadata, expected) -> None:
    desc, attachment_text, attachment_file = metadata
    assert classify_nse_document_subtype(
        desc=desc, attachment_text=attachment_text, attachment_file=attachment_file,
    ) == expected


def test_nse_document_subtype_classifier_has_single_overlap_precedence() -> None:
    assert classify_nse_document_subtype(
        desc="Investor Presentation and conference call transcript",
        attachment_text="",
        attachment_file="q1.pdf",
    ) == DocumentSubtype.CONFERENCE_CALL_MATERIAL


@pytest.mark.asyncio
async def test_nse_official_discovery_keeps_required_filings_and_confident_high_value_metadata() -> None:
    profile = CompanyResearchProfile(
        instrument_id=UUID("99999999-9999-9999-9999-999999999999"), company_id=UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
        company_name="Example Components Limited", isin="INE000A01010", ticker="EXAMPLE", exchange="NSE", mic="XNSE",
        country="IN", currency="INR", provider_instrument_ids={"NSE": "EXAMPLE"},
    )
    rows = [
        {"an_dt": "10-Aug-2026 13:52:28", "desc": "Financial results", "attchmntText": "Financial results", "attchmntFile": "https://nsearchives.nseindia.com/corporate/results.pdf"},
        {"an_dt": "09-Aug-2026 13:52:28", "desc": "Shareholding pattern", "attchmntText": "", "attchmntFile": "https://nsearchives.nseindia.com/corporate/holding.pdf"},
        {"an_dt": "08-Aug-2026 13:52:28", "desc": "Investor Presentation", "attchmntText": "", "attchmntFile": "https://nsearchives.nseindia.com/corporate/presentation.pdf"},
        {"an_dt": "07-Aug-2026 13:52:28", "desc": "Receipt of Order", "attchmntText": "", "attchmntFile": "https://nsearchives.nseindia.com/corporate/order.pdf"},
        {"an_dt": "06-Aug-2026 13:52:28", "desc": "Board meeting notice", "attchmntText": "", "attchmntFile": "https://nsearchives.nseindia.com/corporate/notice.pdf"},
    ]
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=rows, request=request)))
    result = await OfficialFilingDiscovery(client).discover(profile, {"FINANCIAL_RESULTS", "SHAREHOLDING_PATTERN"}, set())
    assert {item.category for item in result} == {
        "FINANCIAL_RESULTS", "SHAREHOLDING_PATTERN", "INVESTOR_PRESENTATION", "ORDER_CONTRACT_DISCLOSURE",
    }
    assert next(item for item in result if item.category == "INVESTOR_PRESENTATION").source.document_subtype == DocumentSubtype.INVESTOR_PRESENTATION
    assert all("notice.pdf" not in item.source.url for item in result)


@pytest.mark.asyncio
async def test_nse_official_discovery_uses_verified_nse_symbol_and_returns_latest_financial_pdf() -> None:
    profile = CompanyResearchProfile(
        instrument_id=UUID("99999999-9999-9999-9999-999999999999"), company_id=UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
        company_name="Example Components Limited", isin="INE000A01010", ticker="BROKER_ALIAS", exchange="NSE", mic="XNSE",
        country="IN", currency="INR", provider_instrument_ids={"NSE": "VERIFIED_NSE_SYMBOL"},
    )
    rows = [
        {"an_dt": "10-Aug-2026 13:52:28", "desc": "Outcome of Board Meeting", "attchmntText": "Financial results for the period ended Jun 30, 2026", "attchmntFile": "https://nsearchives.nseindia.com/corporate/latest.pdf"},
        {"an_dt": "01-Aug-2026 12:00:00", "desc": "AGM notice", "attchmntText": "Annual general meeting", "attchmntFile": "https://nsearchives.nseindia.com/corporate/agm.pdf"},
    ]
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=rows, request=request)))
    result = await OfficialFilingDiscovery(client).discover(profile, {"FINANCIAL_RESULTS"}, set())
    assert len(result) == 1
    assert result[0].source.url.endswith("latest.pdf")
    assert result[0].source.source_classification == SourceClassification.EXCHANGE
    assert result[0].source.discovery_method == "NSE_OFFICIAL_API"
    assert result[0].source.official_nse_profile_symbol == "VERIFIED_NSE_SYMBOL"


@pytest.mark.asyncio
async def test_trusted_nse_api_discovery_persists_expected_profile_when_generic_resolution_fails(monkeypatch) -> None:
    profile = CompanyResearchProfile(
        instrument_id=UUID("99999999-9999-9999-9999-999999999999"), company_id=UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
        company_name="Example Components Limited", isin="INE000A01010", ticker="BROKER_ALIAS", exchange="NSE", mic="XNSE",
        country="IN", currency="INR", provider_instrument_ids={"NSE": "VERIFIED_NSE_SYMBOL"},
    )
    rows = [{"an_dt": "10-Aug-2026 13:52:28", "desc": "Financial results", "attchmntText": "Financial results", "attchmntFile": "https://nsearchives.nseindia.com/corporate/latest.pdf"}]
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=rows, request=request)))
    discovered = await OfficialFilingDiscovery(client).discover(profile, {"FINANCIAL_RESULTS"}, set())
    repository = ResearchRepository(settings=Settings(research_live_enabled=True, research_demo_enabled=False))
    repository._fetcher = _OfficialFetchFixture(FetchResult(
        final_url=discovered[0].source.url, status_code=200, content_type="text/html", bytes_read=100,
        text="<html><title>Board meeting outcome</title><main>Unaudited quarterly financial results were approved by the board today.</main></html>",
    ))
    monkeypatch.setattr(repository._resolver, "resolve", lambda *_args: EntityResolution(instrument_id=None, company_id=None, confidence=0.0))

    await repository._fetch_official_filings(profile, discovered, set())

    document = next(document for document in repository.documents.values() if document.canonical_url == discovered[0].source.url)
    assert document.instrument_id == profile.instrument_id
    assert document.company_id == profile.company_id
    assert document.entity_resolution_confidence == 0.99
    assert document.discovery_provider == "NSE_OFFICIAL_API"


def test_expected_profile_alone_cannot_bypass_generic_relevance(monkeypatch) -> None:
    repository = ResearchRepository()
    profile = repository.profile(AIXTRON_INSTRUMENT_ID)
    documents_before = dict(repository.documents)
    events_before = dict(repository.events)
    event_keys_before = set(repository._event_keys)
    monkeypatch.setattr(repository._resolver, "resolve", lambda *_args: EntityResolution(instrument_id=None, company_id=None, confidence=0.0))

    with pytest.raises(FetchError, match="COMPANY_RELEVANCE_FAILED"):
        repository.ingest_fixture(
            original_url="https://example.com/untrusted", source_type=SourceType.NEWS, source_name="untrusted",
            publisher="untrusted", content_type="text/html", body="<main>unrelated document with sufficient extracted text for validation.</main>",
            reliability=ReliabilityLevel.LEVEL_C, expected_profile=profile,
        )


def test_prepare_ingested_document_does_not_apply_repository_state() -> None:
    repository = ResearchRepository()
    profile = repository.profile(AIXTRON_INSTRUMENT_ID)
    documents_before = dict(repository.documents)
    events_before = dict(repository.events)
    event_keys_before = set(repository._event_keys)
    document = repository._prepare_ingested_document(
        original_url="https://example.com/prepared", source_type=SourceType.NEWS, source_name="fixture",
        publisher="fixture", content_type="text/html",
        body="<main>AIXTRON SE published sufficient fixture content for preparation.</main>",
        reliability=ReliabilityLevel.LEVEL_C, published_at=None, source_mode=SourceMode.DEMO,
        source_classification=SourceClassification.OTHER, discovered_at=None, discovery_provider=None,
        expected_profile=profile, document_status=DocumentStatus.PARSED, allow_empty_content=False,
        trusted_profile_identity=None,
    )
    assert document.canonical_url == "https://example.com/prepared"
    assert repository.documents == documents_before
    assert repository.events == events_before
    assert repository._event_keys == event_keys_before


def test_nse_looking_url_without_official_discovery_provenance_cannot_bypass_relevance(monkeypatch) -> None:
    repository = ResearchRepository()
    profile = repository.profile(AIXTRON_INSTRUMENT_ID)
    source = RegisteredResearchSource(
        source_id="untrusted-nse-lookalike", instrument_id=profile.instrument_id,
        url="https://nsearchives.nseindia.com/corporate/untrusted.pdf", source_type=SourceType.EXCHANGE_ANNOUNCEMENT,
        source_classification=SourceClassification.EXCHANGE, source_name="NSE", publisher="NSE",
        reliability_level=ReliabilityLevel.LEVEL_A, domain="nsearchives.nseindia.com", company_id=profile.company_id,
        discovery_method="NSE_OFFICIAL_API",
    )
    monkeypatch.setattr(repository._resolver, "resolve", lambda *_args: EntityResolution(instrument_id=None, company_id=None, confidence=0.0))

    with pytest.raises(FetchError, match="COMPANY_RELEVANCE_FAILED"):
        repository._ingest_registered_fetch_result(profile, source, FetchResult(
            final_url=source.url, status_code=200, content_type="text/html", bytes_read=100,
            text="<main>Unrelated document with sufficient extracted text for validation.</main>",
        ), expected_profile=profile)


@pytest.mark.asyncio
async def test_nse_official_discovery_preserves_valid_archive_url_and_logs_terminal_failure(caplog) -> None:
    profile = CompanyResearchProfile(
        instrument_id=UUID("99999999-9999-9999-9999-999999999999"), company_id=UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
        company_name="Example Components Limited", isin="INE000A01010", ticker="EXAMPLE", exchange="NSE", mic="XNSE",
        country="IN", currency="INR", provider_instrument_ids={"NSE": "EXAMPLE"},
    )
    rows = [{"an_dt": "10-Aug-2026 13:52:28", "desc": "Financial results", "attchmntText": "Financial results", "attchmntFile": "HTTPS://nsearchives.nseindia.com/corporate//latest.pdf?b=2&a=1"}]
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=rows, request=request)))
    result = await OfficialFilingDiscovery(client).discover(profile, {"FINANCIAL_RESULTS"}, set())
    assert result[0].source.url == "https://nsearchives.nseindia.com/corporate/latest.pdf?a=1&b=2"

    invalid_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}, request=request)))
    with caplog.at_level(logging.WARNING, logger="app.source_discovery"), pytest.raises(SearchProviderError):
        await OfficialFilingDiscovery(invalid_client).discover(profile, {"FINANCIAL_RESULTS"}, set())
    assert any("official_discovery_result" in record.message and "status=FAILED" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_unsafe_or_malformed_nse_attachment_is_rejected_without_aborting_other_candidates(caplog) -> None:
    profile = CompanyResearchProfile(
        instrument_id=UUID("99999999-9999-9999-9999-999999999999"), company_id=UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
        company_name="Example Components Limited", isin="INE000A01010", ticker="EXAMPLE", exchange="NSE", mic="XNSE",
        country="IN", currency="INR", provider_instrument_ids={"NSE": "EXAMPLE"},
    )
    rows = [
        {"an_dt": "11-Aug-2026 13:52:28", "desc": "Financial results", "attchmntText": "Financial results", "attchmntFile": "http://127.0.0.1/private.pdf"},
        {"an_dt": "10-Aug-2026 13:52:28", "desc": "Financial results", "attchmntText": "Financial results", "attchmntFile": "/relative.pdf"},
        {"an_dt": "09-Aug-2026 13:52:28", "desc": "Financial results", "attchmntText": "Financial results", "attchmntFile": "https://nsearchives.nseindia.com/corporate/valid.pdf"},
    ]
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=rows, request=request)))
    with caplog.at_level(logging.WARNING, logger="app.source_discovery"):
        result = await OfficialFilingDiscovery(client).discover(profile, {"FINANCIAL_RESULTS"}, set())

    assert [item.source.url for item in result] == ["https://nsearchives.nseindia.com/corporate/valid.pdf"]
    assert sum("official_candidate_rejected" in record.message for record in caplog.records) == 2


def test_public_nse_archive_cdn_url_remains_permitted_by_ssrf_validation() -> None:
    assert validate_public_http_url("https://nsearchives.nseindia.com/corporate/results.pdf")


def _official_financial_result_source(profile: CompanyResearchProfile, suffix: str) -> RegisteredResearchSource:
    return RegisteredResearchSource(
        source_id=f"nse-result-{suffix}", instrument_id=profile.instrument_id,
        url=f"https://nsearchives.nseindia.com/corporate/{suffix}.pdf",
        source_type=SourceType.EXCHANGE_ANNOUNCEMENT, source_classification=SourceClassification.EXCHANGE,
        source_name="NSE corporate announcements", publisher="NSE", reliability_level=ReliabilityLevel.LEVEL_A,
        domain="nsearchives.nseindia.com", company_id=profile.company_id, discovery_method="NSE_OFFICIAL_API",
        priority=1, categories=("FINANCIAL_RESULTS",),
    )


class _StaticOfficialDiscovery:
    def __init__(self, results: list[DiscoveryResult]) -> None:
        self.results = results
        self.calls = 0

    async def discover(self, _profile, _categories, _seen_urls) -> list[DiscoveryResult]:
        self.calls += 1
        return self.results


class _OfficialFetchFixture:
    def __init__(self, response: FetchResult | Exception) -> None:
        self.response = response
        self.urls: list[str] = []

    async def fetch(self, url: str) -> FetchResult:
        self.urls.append(url)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


@pytest.mark.asyncio
async def test_successful_official_financial_result_persists_and_is_removed_from_fallback_missing_categories() -> None:
    settings = Settings(research_live_enabled=True, research_search_enabled=True, research_official_document_max_attempts_per_refresh=3)
    repository = ResearchRepository(settings=settings, search_discovery=SearchDiscoveryService(_StaticSearchProvider([])))
    profile = next(profile for profile in repository.profiles if profile.ticker == "RELIANCE")
    source = _official_financial_result_source(profile, "latest")
    repository._official_filing_discovery = _StaticOfficialDiscovery([DiscoveryResult("FINANCIAL_RESULTS", source)])
    repository._fetcher = _OfficialFetchFixture(FetchResult(
        final_url=source.url, status_code=200, content_type="text/html", bytes_read=180,
        text="<html><title>Reliance Industries Limited Quarterly Financial Results</title><main>Reliance Industries Limited RELIANCE INE002A01018 quarterly financial results revenue from operations 1000 crore PAT 100 crore.</main></html>",
    ))

    await repository._refresh_targeted(profile, set())

    documents = repository.documents_for(profile.instrument_id, source_mode=SourceMode.REAL)
    assert len(documents) == 1
    assert documents[0].status == DocumentStatus.PROCESSED
    assert "FINANCIAL_RESULTS" not in repository._missing_categories(profile, set(), datetime.now(timezone.utc), {"FINANCIAL_RESULTS"})


@pytest.mark.asyncio
async def test_official_timeout_uses_host_failure_budget_and_future_refresh_can_retry_without_freshness() -> None:
    settings = Settings(
        research_live_enabled=True,
        research_official_document_max_attempts_per_refresh=3,
        research_official_document_max_transport_failures_per_host=1,
        research_official_document_timeout_seconds=1.0,
    )
    repository = ResearchRepository(settings=settings)
    profile = next(profile for profile in repository.profiles if profile.ticker == "RELIANCE")
    sources = [_official_financial_result_source(profile, f"newest-{index}") for index in range(5)]
    fetcher = _OfficialFetchFixture(TransportFetchError("Fetch timed out"))
    repository._fetcher = fetcher

    await repository._fetch_official_filings(profile, [DiscoveryResult("FINANCIAL_RESULTS", source) for source in sources], set())
    assert fetcher.urls == [sources[0].url]
    assert not repository._category_is_fresh(profile.instrument_id, "FINANCIAL_RESULTS", datetime.now(timezone.utc))

    await repository._fetch_official_filings(profile, [DiscoveryResult("FINANCIAL_RESULTS", source) for source in sources], set())
    assert fetcher.urls == [sources[0].url, sources[0].url]


@pytest.mark.asyncio
async def test_failed_official_fetch_keeps_existing_qualifying_evidence() -> None:
    repository = ResearchRepository(settings=Settings(research_live_enabled=True))
    profile = next(profile for profile in repository.profiles if profile.ticker == "RELIANCE")
    existing = repository.ingest_fixture(
        original_url="https://nsearchives.nseindia.com/corporate/existing-result.pdf",
        source_type=SourceType.EXCHANGE_ANNOUNCEMENT, source_classification=SourceClassification.EXCHANGE,
        source_name="NSE corporate announcements", publisher="NSE", content_type="text/html",
        body="<html><title>Reliance Industries Limited Financial Results</title><main>Reliance Industries Limited RELIANCE INE002A01018 quarterly financial results revenue 1000 crore PAT 100 crore.</main></html>",
        reliability=ReliabilityLevel.LEVEL_A, source_mode=SourceMode.REAL, expected_profile=profile,
    )
    source = _official_financial_result_source(profile, "timeout")
    repository._fetcher = _OfficialFetchFixture(FetchError("Fetch timed out"))

    await repository._fetch_official_filings(profile, [DiscoveryResult("FINANCIAL_RESULTS", source)], set())

    assert repository.documents[existing.document_id] is existing


@pytest.mark.asyncio
async def test_official_http_document_failures_do_not_trip_host_transport_budget() -> None:
    settings = Settings(research_live_enabled=True, research_official_document_max_attempts_per_refresh=3)
    repository = ResearchRepository(settings=settings)
    profile = next(profile for profile in repository.profiles if profile.ticker == "RELIANCE")
    sources = [_official_financial_result_source(profile, f"http-{index}") for index in range(4)]
    fetcher = _OfficialFetchFixture(HttpStatusFetchError(404))
    repository._fetcher = fetcher

    await repository._fetch_official_filings(profile, [DiscoveryResult("FINANCIAL_RESULTS", source) for source in sources], set())

    assert fetcher.urls == [source.url for source in sources[:3]]


@pytest.mark.asyncio
async def test_official_restricted_document_failure_does_not_trip_host_transport_budget() -> None:
    settings = Settings(research_live_enabled=True, research_official_document_max_attempts_per_refresh=3)
    repository = ResearchRepository(settings=settings)
    profile = next(profile for profile in repository.profiles if profile.ticker == "RELIANCE")
    sources = [_official_financial_result_source(profile, f"restricted-{index}") for index in range(4)]
    fetcher = _OfficialFetchFixture(RestrictedFetchError(403))
    repository._fetcher = fetcher

    await repository._fetch_official_filings(profile, [DiscoveryResult("FINANCIAL_RESULTS", source) for source in sources], set())

    assert fetcher.urls == [source.url for source in sources[:3]]


@pytest.mark.asyncio
async def test_pdf_extraction_timeout_does_not_trip_official_host_transport_budget() -> None:
    class SlowPdfFetcher(HttpResearchFetcher):
        def process_network_response(self, response, *, max_bytes=None):
            time.sleep(0.05)
            return FetchResult(response.final_url, 200, "text/html", "<html>unused</html>", len(response.content))

    settings = Settings(
        research_live_enabled=True,
        research_official_document_max_attempts_per_refresh=2,
        research_official_document_timeout_seconds=1.0,
        research_official_document_extraction_timeout_seconds=0.01,
    )
    requests: list[str] = []
    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF-fast", request=request)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    repository = ResearchRepository(settings=settings, fetcher=SlowPdfFetcher(settings, client))
    profile = next(profile for profile in repository.profiles if profile.ticker == "RELIANCE")
    sources = [_official_financial_result_source(profile, f"slow-{index}") for index in range(3)]

    await repository._fetch_official_filings(profile, [DiscoveryResult("FINANCIAL_RESULTS", source) for source in sources], set())

    # Extraction failures are document failures, so the normal attempt budget
    # (two) applies rather than the one-failure host transport circuit.
    assert requests == [source.url for source in sources[:2]]


@pytest.mark.asyncio
async def test_official_filing_single_flight_shares_network_extraction_and_persistence() -> None:
    class CountingFetcher(HttpResearchFetcher):
        def __init__(self, settings):
            super().__init__(settings)
            self.network_calls = 0
            self.extraction_calls = 0
            self.network_started = asyncio.Event()
            self.release_network = asyncio.Event()

        async def fetch_network(self, url, **_kwargs):
            self.network_calls += 1
            self.network_started.set()
            await self.release_network.wait()
            return type("Network", (), {"final_url": url, "status_code": 200, "content": b"body"})()

        def process_network_response(self, response, *, max_bytes=None):
            self.extraction_calls += 1
            return FetchResult(
                response.final_url, 200, "text/html",
                "<title>Reliance quarterly financial results</title><main>Reliance Industries Limited RELIANCE INE002A01018 quarterly financial results revenue 1000 crore PAT 100 crore</main>",
                len(response.content),
            )

    settings = Settings(research_live_enabled=True)
    fetcher = CountingFetcher(settings)
    repository = ResearchRepository(settings=settings, fetcher=fetcher)
    profile = next(profile for profile in repository.profiles if profile.ticker == "RELIANCE")
    source = _official_financial_result_source(profile, "single-flight")
    persistence_calls = 0
    original_ingest = repository._ingest_registered_fetch_result_async

    async def count_ingest(*args, **kwargs):
        nonlocal persistence_calls
        persistence_calls += 1
        return await original_ingest(*args, **kwargs)

    repository._ingest_registered_fetch_result_async = count_ingest
    diagnostic_records: list[logging.LogRecord] = []

    class DiagnosticHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            diagnostic_records.append(record)

    diagnostic_handler = DiagnosticHandler(level=logging.INFO)
    repository_logger = logging.getLogger("app.repository")
    original_level = repository_logger.level
    repository_logger.setLevel(logging.INFO)
    repository_logger.addHandler(diagnostic_handler)
    try:
        leader = asyncio.create_task(
            repository._fetch_official_filings(profile, [DiscoveryResult("FINANCIAL_RESULTS", source)], set())
        )
        await fetcher.network_started.wait()
        follower = asyncio.create_task(
            repository._fetch_official_filings(profile, [DiscoveryResult("FINANCIAL_RESULTS", source)], set())
        )
        # The leader is blocked above.  Yielding one event-loop turn lets the
        # follower observe and await the installed flight without timing luck.
        await asyncio.sleep(0)
        assert not follower.done()
        fetcher.release_network.set()
        await asyncio.gather(leader, follower)
    finally:
        repository_logger.removeHandler(diagnostic_handler)
        repository_logger.setLevel(original_level)

    assert fetcher.network_calls == fetcher.extraction_calls == persistence_calls == 1
    assert len(repository.documents_for(profile.instrument_id, source_mode=SourceMode.REAL)) == 1
    assert any(
        "outcome=REUSED" in record.getMessage() and "reason=IN_FLIGHT_SINGLE_FLIGHT" in record.getMessage()
        for record in diagnostic_records
    )
    assert repository._official_filing_flights == {}


@pytest.mark.asyncio
async def test_async_live_ingestion_preparation_does_not_block_event_loop() -> None:
    repository = ResearchRepository(settings=Settings(research_live_enabled=True))
    profile = next(item for item in repository.profiles if item.ticker == "RELIANCE")
    source = _official_financial_result_source(profile, "prepare-worker")
    entered, release = threading.Event(), threading.Event()
    worker_thread: list[int] = []
    original = repository._prepare_ingested_document

    def blocked_prepare(*args, **kwargs):
        worker_thread.append(threading.get_ident())
        entered.set()
        assert release.wait(2)
        return original(*args, **kwargs)

    repository._prepare_ingested_document = blocked_prepare
    result = FetchResult(source.url, 200, "text/html", "<main>Reliance Industries Limited RELIANCE INE002A01018 sufficient content</main>", 100)
    loop_thread = threading.get_ident()
    documents_before = dict(repository.documents)
    task = asyncio.create_task(repository._ingest_registered_fetch_result_async(profile, source, result, expected_profile=profile))
    assert await asyncio.to_thread(entered.wait, 1)
    progressed = False
    async def sibling():
        nonlocal progressed
        await asyncio.sleep(0)
        progressed = True
    await sibling()
    assert progressed and worker_thread[0] != loop_thread
    assert repository.documents == documents_before
    release.set()
    document = await task
    assert document.status == DocumentStatus.PROCESSED
    assert len([item for item in repository.documents.values() if item.document_id == document.document_id]) == 1


@pytest.mark.asyncio
async def test_async_live_ingestion_document_persistence_does_not_block_event_loop() -> None:
    repository = ResearchRepository(settings=Settings(research_live_enabled=True))
    profile = next(item for item in repository.profiles if item.ticker == "RELIANCE")
    source = _official_financial_result_source(profile, "persist-worker")
    started, release = threading.Event(), threading.Event()
    worker: list[int] = []; continued = 0; calls = 0
    original_persist = repository._persist_ingested_document
    original_continue = repository._continue_ingested_document_after_events_async
    def blocked(document, **kwargs):
        nonlocal calls
        calls += 1; worker.append(threading.get_ident()); started.set(); assert release.wait(2)
        return original_persist(document, **kwargs)
    async def count_continue(*args, **kwargs):
        nonlocal continued
        continued += 1
        return await original_continue(*args, **kwargs)
    repository._persist_ingested_document = blocked
    repository._continue_ingested_document_after_events_async = count_continue
    result = FetchResult(source.url, 200, "text/html", "<main>Reliance Industries Limited RELIANCE INE002A01018 sufficient content</main>", 100)
    loop_thread = threading.get_ident()
    task = asyncio.create_task(repository._ingest_registered_fetch_result_async(profile, source, result, expected_profile=profile))
    assert await asyncio.to_thread(started.wait, 1)
    await asyncio.sleep(0)
    assert worker[0] != loop_thread and continued == 0
    assert any(item.canonical_url == source.url for item in repository.documents.values())
    release.set(); document = await task
    assert calls == continued == 1 and document.status == DocumentStatus.PROCESSED


@pytest.mark.asyncio
async def test_async_live_ingestion_document_persistence_failure_rolls_back() -> None:
    repository = ResearchRepository(settings=Settings(research_live_enabled=True))
    profile = next(item for item in repository.profiles if item.ticker == "RELIANCE")
    source = _official_financial_result_source(profile, "persist-failure")
    attempted: list[UUID] = []; continued = 0
    def fail(document, **_kwargs):
        attempted.append(document.document_id)
        assert document.document_id in repository.documents
        raise RuntimeError("db unavailable")
    repository._persist_ingested_document = fail
    repository._continue_ingested_document_after_persistence = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("downstream must not run"))
    result = FetchResult(source.url, 200, "text/html", "<main>Reliance Industries Limited RELIANCE INE002A01018 sufficient content</main>", 100)
    with pytest.raises(FetchError, match="DOCUMENT_PERSIST_FAILED"):
        await repository._ingest_registered_fetch_result_async(profile, source, result, expected_profile=profile)
    assert len(attempted) == 1 and attempted[0] not in repository.documents


@pytest.mark.asyncio
async def test_async_live_ingestion_financial_processing_does_not_block_event_loop() -> None:
    repository = ResearchRepository(settings=Settings(research_live_enabled=True))
    profile = next(item for item in repository.profiles if item.ticker == "RELIANCE")
    source = _official_financial_result_source(profile, "financial-worker")
    started, release = threading.Event(), threading.Event()
    worker: list[int] = []; financial_calls = 0; continued = 0
    original_financial = repository._persist_official_financial_facts
    original_continue = repository._continue_ingested_document_after_events_async
    def blocked(document):
        nonlocal financial_calls
        financial_calls += 1; worker.append(threading.get_ident()); started.set(); assert release.wait(2)
        return original_financial(document)
    async def count_continue(*args, **kwargs):
        nonlocal continued
        continued += 1
        return await original_continue(*args, **kwargs)
    repository._persist_official_financial_facts = blocked
    repository._continue_ingested_document_after_events_async = count_continue
    result = FetchResult(source.url, 200, "text/html", "<main>Reliance Industries Limited RELIANCE INE002A01018 quarterly financial results revenue 1000 crore PAT 100 crore</main>", 100)
    loop_thread = threading.get_ident()
    task = asyncio.create_task(repository._ingest_registered_fetch_result_async(profile, source, result, expected_profile=profile))
    assert await asyncio.to_thread(started.wait, 1)
    await asyncio.sleep(0)
    assert worker[0] != loop_thread and continued == 0
    assert any(item.canonical_url == source.url for item in repository.documents.values())
    release.set(); document = await task
    assert document.status == DocumentStatus.PROCESSED
    assert financial_calls == continued == 1


@pytest.mark.asyncio
async def test_async_live_ingestion_event_extraction_does_not_block_event_loop() -> None:
    repository = ResearchRepository(settings=Settings(research_live_enabled=True))
    profile = next(item for item in repository.profiles if item.ticker == "RELIANCE")
    source = _official_financial_result_source(profile, "event-worker")
    started, release = threading.Event(), threading.Event()
    worker: list[int] = []; calls = 0
    original = repository._extract_ingested_event_candidates
    before_keys, before_events = set(repository._event_keys), dict(repository.events)
    def blocked(*args, **kwargs):
        nonlocal calls
        calls += 1; worker.append(threading.get_ident()); started.set(); assert release.wait(2)
        return original(*args, **kwargs)
    repository._extract_ingested_event_candidates = blocked
    result = FetchResult(source.url, 200, "text/html", "<main>Reliance Industries Limited RELIANCE INE002A01018 quarterly financial results revenue 1000 crore PAT 100 crore</main>", 100)
    loop_thread = threading.get_ident()
    task = asyncio.create_task(repository._ingest_registered_fetch_result_async(profile, source, result, expected_profile=profile))
    assert await asyncio.to_thread(started.wait, 1)
    progressed = False
    async def sibling():
        nonlocal progressed
        await asyncio.sleep(0); progressed = True
    await sibling()
    assert progressed and worker[0] != loop_thread and calls == 1
    assert repository._event_keys == before_keys and repository.events == before_events
    assert any(item.canonical_url == source.url for item in repository.documents.values())
    release.set(); await task
    assert calls == 1


@pytest.mark.asyncio
async def test_official_filing_single_flight_failure_is_shared_and_cleanup_allows_retry() -> None:
    class FailingFetcher(HttpResearchFetcher):
        def __init__(self, settings):
            super().__init__(settings)
            self.calls = 0

        async def fetch_network(self, _url, **_kwargs):
            self.calls += 1
            await asyncio.sleep(0.01)
            raise TransportFetchError("Fetch timed out")

    settings = Settings(research_live_enabled=True)
    fetcher = FailingFetcher(settings)
    repository = ResearchRepository(settings=settings, fetcher=fetcher)
    profile = next(profile for profile in repository.profiles if profile.ticker == "RELIANCE")
    source = _official_financial_result_source(profile, "single-flight-failure")

    await asyncio.gather(
        repository._fetch_official_filings(profile, [DiscoveryResult("FINANCIAL_RESULTS", source)], set()),
        repository._fetch_official_filings(profile, [DiscoveryResult("FINANCIAL_RESULTS", source)], set()),
    )
    assert fetcher.calls == 1
    assert repository._official_filing_flights == {}

    await repository._fetch_official_filings(profile, [DiscoveryResult("FINANCIAL_RESULTS", source)], set())
    assert fetcher.calls == 2


@pytest.mark.asyncio
async def test_late_extraction_completion_after_timeout_cannot_persist_or_retain_flight() -> None:
    class SlowFetcher(HttpResearchFetcher):
        def __init__(self, settings):
            super().__init__(settings)
            self.extractions = 0

        async def fetch_network(self, url, **_kwargs):
            return type("Network", (), {"final_url": url, "status_code": 200, "content": b"body"})()

        def process_network_response(self, response, *, max_bytes=None):
            self.extractions += 1
            time.sleep(0.05)
            return FetchResult(response.final_url, 200, "text/html", "<main>late</main>", len(response.content))

    settings = Settings(research_live_enabled=True, research_official_document_extraction_timeout_seconds=0.01)
    fetcher = SlowFetcher(settings)
    repository = ResearchRepository(settings=settings, fetcher=fetcher)
    profile = next(profile for profile in repository.profiles if profile.ticker == "RELIANCE")
    source = _official_financial_result_source(profile, "late-thread")
    await repository._fetch_official_filings(profile, [DiscoveryResult("FINANCIAL_RESULTS", source)], set())
    await asyncio.sleep(0.07)

    assert fetcher.extractions == 1
    assert repository.documents_for(profile.instrument_id, source_mode=SourceMode.REAL) == []
    assert repository._official_filing_flights == {}


@pytest.mark.asyncio
async def test_official_filing_single_flight_cancellation_cleans_up_and_urls_remain_independent() -> None:
    class BlockingFetcher(HttpResearchFetcher):
        def __init__(self, settings):
            super().__init__(settings)
            self.calls: list[str] = []
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def fetch_network(self, url, **_kwargs):
            self.calls.append(url)
            self.started.set()
            await self.release.wait()
            raise TransportFetchError("cancelled test")

    settings = Settings(research_live_enabled=True)
    fetcher = BlockingFetcher(settings)
    repository = ResearchRepository(settings=settings, fetcher=fetcher)
    profile = next(profile for profile in repository.profiles if profile.ticker == "RELIANCE")
    first = _official_financial_result_source(profile, "cancel-one")
    second = _official_financial_result_source(profile, "cancel-two")
    first_task = asyncio.create_task(repository._fetch_official_filings(profile, [DiscoveryResult("FINANCIAL_RESULTS", first)], set()))
    await fetcher.started.wait()
    second_task = asyncio.create_task(repository._fetch_official_filings(profile, [DiscoveryResult("FINANCIAL_RESULTS", second)], set()))
    for _ in range(10):
        if len(fetcher.calls) == 2:
            break
        await asyncio.sleep(0)
    assert set(fetcher.calls) == {first.url, second.url}

    first_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_task
    await asyncio.sleep(0)
    assert (profile.instrument_id, canonicalize_url(first.url)) not in repository._official_filing_flights

    fetcher.release.set()
    await second_task
    assert repository._official_filing_flights == {}


@pytest.mark.asyncio
async def test_fast_large_official_pdf_persists_and_marks_financial_results_fresh(monkeypatch) -> None:
    class Page:
        def extract_text(self):
            return "Reliance Industries Limited RELIANCE INE002A01018 quarterly financial results revenue 1000 crore PAT 100 crore"

    class Reader:
        def __init__(self, _stream):
            self.pages = [Page()]

    monkeypatch.setattr("pypdf.PdfReader", Reader)
    settings = Settings(
        research_live_enabled=True,
        research_max_retries=0,
        research_max_content_bytes=6_000_000,
        research_official_document_timeout_seconds=1.0,
        research_official_document_extraction_timeout_seconds=1.0,
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF-" + b"x" * 5_000_000, request=request)
    ))
    repository = ResearchRepository(settings=settings, fetcher=HttpResearchFetcher(settings, client))
    profile = next(profile for profile in repository.profiles if profile.ticker == "RELIANCE")
    source = _official_financial_result_source(profile, "large")

    await repository._fetch_official_filings(profile, [DiscoveryResult("FINANCIAL_RESULTS", source)], set())
    repository._mark_qualifying_categories_fresh(profile.instrument_id, {"FINANCIAL_RESULTS"}, datetime.now(timezone.utc))

    assert repository.documents_for(profile.instrument_id, source_mode=SourceMode.REAL)[0].status == DocumentStatus.PROCESSED
    assert repository._category_is_fresh(profile.instrument_id, "FINANCIAL_RESULTS", datetime.now(timezone.utc))


@pytest.mark.asyncio
async def test_official_document_fetch_uses_configured_provider_neutral_user_agent() -> None:
    settings = Settings(research_max_retries=0, research_official_document_user_agent="Mozilla/5.0 test-agent")
    observed: dict[str, str] = {}
    def handler(request):
        observed["user_agent"] = request.headers["user-agent"]
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<html>official document</html>", request=request)
    fetcher = HttpResearchFetcher(settings, httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    result = await fetcher.fetch_network(
        "https://public-official.example/documents/report.html",
        headers={"User-Agent": settings.research_official_document_user_agent},
    )

    assert result.status_code == 200
    assert observed["user_agent"] == "Mozilla/5.0 test-agent"


@pytest.mark.asyncio
async def test_official_network_budget_accepts_five_and_eleven_megabyte_pdfs() -> None:
    settings = Settings(research_max_retries=0)
    for size in (5_500_000, 11_000_000):
        client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request, size=size: httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF-" + b"x" * size, request=request)
        ))
        result = await HttpResearchFetcher(settings, client).fetch_network(
            "https://nsearchives.nseindia.com/corporate/result.pdf", max_bytes=settings.research_official_document_max_bytes,
        )
        assert len(result.content) == size + 5


@pytest.mark.asyncio
async def test_official_content_length_limit_rejects_before_body_read() -> None:
    settings = Settings(research_max_retries=0, research_official_document_max_bytes=10)
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, headers={"content-type": "application/pdf", "content-length": "11"}, content=b"%PDF-body", request=request)
    ))
    with pytest.raises(DocumentSizeLimitExceeded) as exc_info:
        await HttpResearchFetcher(settings, client).fetch_network("https://nsearchives.nseindia.com/corporate/result.pdf", max_bytes=10)
    assert exc_info.value.content_length == 11
    assert exc_info.value.max_bytes == 10


@pytest.mark.asyncio
async def test_official_streaming_limit_handles_missing_or_dishonest_content_length() -> None:
    settings = Settings(research_max_retries=0, research_official_document_max_bytes=10)
    for headers in ({"content-type": "application/pdf"}, {"content-type": "application/pdf", "content-length": "1"}):
        client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request, headers=headers: httpx.Response(200, headers=headers, content=b"%PDF-" + b"x" * 10, request=request)
        ))
        with pytest.raises(DocumentSizeLimitExceeded):
            await HttpResearchFetcher(settings, client).fetch_network("https://nsearchives.nseindia.com/corporate/result.pdf", max_bytes=10)


@pytest.mark.asyncio
async def test_oversized_official_document_is_retryable_and_does_not_open_transport_circuit_or_freshen() -> None:
    settings = Settings(
        research_live_enabled=True, research_max_retries=0,
        research_official_document_max_bytes=10,
        research_official_document_max_attempts_per_refresh=2,
    )
    requests: list[str] = []
    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(200, headers={"content-type": "application/pdf", "content-length": "11"}, content=b"%PDF-body", request=request)
    repository = ResearchRepository(settings=settings, fetcher=HttpResearchFetcher(settings, httpx.AsyncClient(transport=httpx.MockTransport(handler))))
    profile = next(profile for profile in repository.profiles if profile.ticker == "RELIANCE")
    sources = [_official_financial_result_source(profile, f"oversized-{index}") for index in range(3)]

    await repository._fetch_official_filings(profile, [DiscoveryResult("FINANCIAL_RESULTS", source) for source in sources], set())

    assert requests == [source.url for source in sources[:2]]
    assert repository.documents_for(profile.instrument_id, source_mode=SourceMode.REAL) == []
    assert not repository._category_is_fresh(profile.instrument_id, "FINANCIAL_RESULTS", datetime.now(timezone.utc))


@pytest.mark.asyncio
@pytest.mark.parametrize("body_size", [5_500_000, 11_000_000, 16_000_000, 24 * 1024 * 1024])
async def test_official_pdf_under_official_budget_reaches_processing_and_persistence(monkeypatch, body_size: int) -> None:
    class Page:
        def extract_text(self):
            return "Reliance Industries Limited RELIANCE INE002A01018 quarterly financial results revenue 1000 crore PAT 100 crore"

    class Reader:
        def __init__(self, _stream):
            self.pages = [Page()]

    monkeypatch.setattr("pypdf.PdfReader", Reader)
    settings = Settings(research_live_enabled=True, research_max_retries=0)
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF-" + b"x" * body_size, request=request)
    ))
    repository = ResearchRepository(settings=settings, fetcher=HttpResearchFetcher(settings, client))
    profile = next(profile for profile in repository.profiles if profile.ticker == "RELIANCE")
    source = _official_financial_result_source(profile, f"size-{body_size}")

    await repository._fetch_official_filings(profile, [DiscoveryResult("FINANCIAL_RESULTS", source)], set())

    assert repository.documents_for(profile.instrument_id, source_mode=SourceMode.REAL)[0].status == DocumentStatus.PROCESSED


@pytest.mark.asyncio
async def test_official_pdf_above_default_budget_fails_before_processing() -> None:
    settings = Settings(research_live_enabled=True, research_max_retries=0)
    requests: list[str] = []
    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(
            200,
            headers={"content-type": "application/pdf", "content-length": str(settings.research_official_document_max_bytes + 1)},
            content=b"",
            request=request,
        )
    repository = ResearchRepository(settings=settings, fetcher=HttpResearchFetcher(settings, httpx.AsyncClient(transport=httpx.MockTransport(handler))))
    profile = next(profile for profile in repository.profiles if profile.ticker == "RELIANCE")
    source = _official_financial_result_source(profile, "over-default")

    await repository._fetch_official_filings(profile, [DiscoveryResult("FINANCIAL_RESULTS", source)], set())

    assert requests == [source.url]
    assert repository.documents_for(profile.instrument_id, source_mode=SourceMode.REAL) == []


@pytest.mark.asyncio
async def test_global_official_document_is_reused_without_second_download_or_extraction() -> None:
    repository = ResearchRepository(settings=Settings(research_live_enabled=True))
    profile = next(profile for profile in repository.profiles if profile.ticker == "RELIANCE")
    source = _official_financial_result_source(profile, "reused")
    fetcher = _OfficialFetchFixture(FetchResult(
        final_url=source.url, status_code=200, content_type="text/html", bytes_read=180,
        text="<html><title>Reliance Industries Limited Quarterly Financial Results</title><main>Reliance Industries Limited RELIANCE INE002A01018 quarterly financial results revenue 1000 crore PAT 100 crore.</main></html>",
    ))
    repository._fetcher = fetcher

    await repository._fetch_official_filings(profile, [DiscoveryResult("FINANCIAL_RESULTS", source)], set())
    await repository._fetch_official_filings(profile, [DiscoveryResult("FINANCIAL_RESULTS", source)], set())
    repository._mark_qualifying_categories_fresh(profile.instrument_id, {"FINANCIAL_RESULTS"}, datetime.now(timezone.utc))

    assert fetcher.urls == [source.url]
    assert len(repository.documents_for(profile.instrument_id, source_mode=SourceMode.REAL)) == 1
    assert repository._category_is_fresh(profile.instrument_id, "FINANCIAL_RESULTS", datetime.now(timezone.utc))


@pytest.mark.asyncio
async def test_failed_or_scanned_official_document_is_not_reused_and_remains_retryable() -> None:
    repository = ResearchRepository(settings=Settings(research_live_enabled=True))
    profile = next(profile for profile in repository.profiles if profile.ticker == "RELIANCE")
    source = _official_financial_result_source(profile, "retryable")
    failed = repository.ingest_fixture(
        original_url=source.url, source_type=source.source_type, source_classification=source.source_classification,
        source_name=source.source_name, publisher=source.publisher, content_type="application/pdf", body="",
        reliability=source.reliability_level, source_mode=SourceMode.REAL, expected_profile=profile,
        document_status=DocumentStatus.FAILED, allow_empty_content=True,
    )
    assert failed.status == DocumentStatus.FAILED
    fetcher = _OfficialFetchFixture(FetchError("Fetch timed out"))
    repository._fetcher = fetcher

    await repository._fetch_official_filings(profile, [DiscoveryResult("FINANCIAL_RESULTS", source)], set())

    assert fetcher.urls == [source.url]
    assert not repository._category_is_fresh(profile.instrument_id, "FINANCIAL_RESULTS", datetime.now(timezone.utc))


@pytest.mark.asyncio
async def test_distinct_official_urls_are_not_collapsed_as_same_quarterly_document() -> None:
    repository = ResearchRepository(settings=Settings(research_live_enabled=True))
    profile = next(profile for profile in repository.profiles if profile.ticker == "RELIANCE")
    first = _official_financial_result_source(profile, "consolidated")
    second = _official_financial_result_source(profile, "standalone")
    repository._fetcher = _OfficialFetchFixture(FetchResult(
        final_url=first.url, status_code=200, content_type="text/html", bytes_read=200,
        text="<html><title>Reliance Industries Limited Financial Results</title><main>Reliance Industries Limited RELIANCE INE002A01018 consolidated quarterly financial results revenue 1000 crore PAT 100 crore.</main></html>",
    ))

    await repository._fetch_official_filings(profile, [DiscoveryResult("FINANCIAL_RESULTS", first)], set())
    repository._fetcher = _OfficialFetchFixture(FetchResult(
        final_url=second.url, status_code=200, content_type="text/html", bytes_read=200,
        text="<html><title>Reliance Industries Limited Financial Results</title><main>Reliance Industries Limited RELIANCE INE002A01018 standalone quarterly financial results revenue 900 crore PAT 90 crore.</main></html>",
    ))
    await repository._fetch_official_filings(profile, [DiscoveryResult("FINANCIAL_RESULTS", second)], set())

    assert len(repository.documents_for(profile.instrument_id, source_mode=SourceMode.REAL)) == 2


def test_existing_durable_financial_result_is_the_only_reason_a_failed_refresh_can_become_fresh() -> None:
    repository = ResearchRepository(settings=Settings(research_live_enabled=True))
    profile = next(profile for profile in repository.profiles if profile.ticker == "RELIANCE")
    now = datetime.now(timezone.utc)
    assert "FINANCIAL_RESULTS" in repository._missing_categories(profile, set(), now, {"FINANCIAL_RESULTS"})
    document = repository.ingest_fixture(
        original_url="https://nsearchives.nseindia.com/corporate/prior-result.pdf",
        source_type=SourceType.EXCHANGE_ANNOUNCEMENT, source_classification=SourceClassification.EXCHANGE,
        source_name="NSE corporate announcements", publisher="NSE", content_type="text/html",
        body="<html><title>Reliance Industries Limited Quarterly Financial Results</title><main>Reliance Industries Limited RELIANCE INE002A01018 quarterly financial results revenue 1000 crore PAT 100 crore.</main></html>",
        reliability=ReliabilityLevel.LEVEL_A, source_mode=SourceMode.REAL, expected_profile=profile,
    )

    repository._mark_qualifying_categories_fresh(profile.instrument_id, {"FINANCIAL_RESULTS"}, now)

    assert document.document_id in repository.documents
    assert "FINANCIAL_RESULTS" not in repository._missing_categories(profile, set(), now, {"FINANCIAL_RESULTS"})


@pytest.mark.asyncio
async def test_official_fetch_is_newest_first_and_bounded_without_concurrency() -> None:
    settings = Settings(research_live_enabled=True, research_official_document_max_attempts_per_refresh=3)
    repository = ResearchRepository(settings=settings)
    profile = next(profile for profile in repository.profiles if profile.ticker == "RELIANCE")
    sources = [_official_financial_result_source(profile, f"newest-{index}") for index in range(5)]
    class ConcurrentProbe:
        def __init__(self) -> None:
            self.urls: list[str] = []
            self.active = 0
            self.max_active = 0

        async def fetch(self, url: str) -> FetchResult:
            self.urls.append(url)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            await asyncio.sleep(0)
            self.active -= 1
            return FetchResult(
                final_url=url, status_code=200, content_type="text/html", bytes_read=200,
                text="<html><title>Reliance Industries Limited Financial Results</title><main>Reliance Industries Limited RELIANCE INE002A01018 financial results revenue 1000 crore PAT 100 crore.</main></html>",
            )

    fetcher = ConcurrentProbe()
    repository._fetcher = fetcher

    await repository._fetch_official_filings(profile, [DiscoveryResult("FINANCIAL_RESULTS", source) for source in sources], set())

    assert fetcher.urls == [source.url for source in sources[:3]]
    assert fetcher.max_active == 1


def test_reused_global_profile_hydrates_verified_nse_mapping_over_broker_alias() -> None:
    profile = CompanyResearchProfile(
        instrument_id=UUID("99999999-9999-9999-9999-999999999999"), company_id=UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
        company_name="Example Components Limited", ticker="BROKER_ALIAS", exchange="NSE", mic="XNSE", country="IN", currency="INR",
    )
    hydrated = _hydrate_verified_exchange_mappings(profile, {"nseSymbol": "VERIFIED_NSE_SYMBOL"})
    assert hydrated.provider_instrument_ids["NSE"] == "VERIFIED_NSE_SYMBOL"


def test_indian_search_queries_prefer_verified_exchange_mapping_over_broker_ticker() -> None:
    profile = CompanyResearchProfile(
        instrument_id=UUID("99999999-9999-9999-9999-999999999999"), company_id=UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
        company_name="Example Components Limited", ticker="BROKER_ALIAS", exchange="NSE", mic="XNSE", country="IN", currency="INR",
        provider_instrument_ids={"NSE": "VERIFIED_NSE_SYMBOL"},
    )
    queries = generate_search_queries(profile, "FINANCIAL_RESULTS", SearchDateWindow())
    assert '"VERIFIED_NSE_SYMBOL" NSE' in queries
    assert '"BROKER_ALIAS" NSE' not in queries
    assert '"VERIFIED_NSE_SYMBOL" BSE' in queries
    assert '"BROKER_ALIAS" BSE' not in queries


def test_invalid_nse_mapping_is_not_selected_when_verified_mapping_exists() -> None:
    repository = ResearchRepository()
    orchestrator = PortfolioResearchOrchestrator(repository, Settings())
    position = _portfolio_position("INE000A01010", "BROKER_ALIAS", "NSE", "Example Components Limited")
    position["instrument"]["globalInstrumentId"] = str(UUID("99999999-9999-9999-9999-999999999999"))
    position["instrument"]["providerMappings"] = [
        {"provider": "NSE", "providerSymbol": "INVALID_ALIAS", "status": "INVALID"},
        {"provider": "NSE", "providerSymbol": "VERIFIED_NSE_SYMBOL", "status": "VERIFIED"},
    ]
    try:
        instruments = orchestrator._dedupe_instruments([position])
        assert instruments[0]["nseSymbol"] == "VERIFIED_NSE_SYMBOL"
    finally:
        import asyncio
        asyncio.run(orchestrator._client.aclose())


@pytest.mark.asyncio
async def test_portfolio_research_summary_is_read_only_for_structured_market_provider() -> None:
    class MustNotCollect:
        def __init__(self) -> None:
            self.calls = 0

        async def collect(self, _instrument):
            self.calls += 1
            raise AssertionError("portfolio summary must not invoke a live structured provider")

    repository = ResearchRepository(settings=Settings(research_live_enabled=False, research_demo_enabled=False))
    global_instrument_id = repository.profiles[0].instrument_id
    position = _portfolio_position("DE000A0WMPJ6", "AIXA", "XETR", "AIXTRON SE")
    position["instrument"]["globalInstrumentId"] = str(global_instrument_id)
    provider = MustNotCollect()
    orchestrator = PortfolioResearchOrchestrator(
        repository, Settings(portfolio_service_base_url="http://portfolio-service"),
        client=_RecordingPortfolioClient([position]), structured_provider=provider,
    )

    await orchestrator.read_portfolio_summary(UUID("aaaaaaaa-1111-1111-1111-111111111111"))

    assert provider.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("broker_symbol", "verified_nse_symbol", "isin"),
    [
        ("INDR", "IRFC", "INE053F01010"),
        ("CHOINV", "CHOLAFIN", "INE121A01024"),
    ],
)
async def test_portfolio_refresh_uses_verified_nse_mapping_for_official_research_without_static_source(
    broker_symbol: str, verified_nse_symbol: str, isin: str
) -> None:
    class RecordingRefreshRepository(ResearchRepository):
        def __init__(self) -> None:
            super().__init__(settings=Settings(research_live_enabled=True, research_search_enabled=False, research_demo_enabled=False))
            self.refreshed_profiles: list[CompanyResearchProfile] = []

        async def refresh(self, instrument_id: UUID, **_kwargs):
            self.refreshed_profiles.append(self.profile(instrument_id))
            return self.summary(instrument_id, allow_demo=False)

    repository = RecordingRefreshRepository()
    global_instrument_id = UUID(int=uuid_int_from_text(f"{isin}|global"))
    position = _portfolio_position(isin, broker_symbol, "NSE", "Generic Indian Equity", provider="ICICI_DIRECT",
                                   provider_instrument_id=f"ISIN:{isin}", data_freshness="REAL_BROKER")
    position["instrument"].update({
        "globalInstrumentId": str(global_instrument_id),
        "country": "IN",
        "providerMappings": [
            {"provider": "ICICI_DIRECT", "providerSymbol": broker_symbol, "status": "VERIFIED", "resolutionSource": "LEGACY_ADOPTION"},
            {"provider": "NSE", "providerSymbol": broker_symbol, "status": "INVALID", "resolutionSource": "BROKER_IMPORT_IDENTITY"},
            {"provider": "NSE", "providerSymbol": verified_nse_symbol, "status": "VERIFIED", "resolutionSource": "NSE_OFFICIAL_ISIN_BOOTSTRAP"},
        ],
    })
    orchestrator = PortfolioResearchOrchestrator(
        repository, Settings(portfolio_service_base_url="http://portfolio-service", structured_provider_enabled=False,
                             research_live_enabled=True, research_search_enabled=False, research_demo_enabled=False),
        client=_RecordingPortfolioClient([position]), structured_provider=_UnavailableStructuredProvider(),
    )

    await orchestrator.refresh_portfolio(UUID("aaaaaaaa-1111-1111-1111-111111111111"))

    assert [profile.provider_instrument_ids["NSE"] for profile in repository.refreshed_profiles] == [verified_nse_symbol]


def test_scanned_report_is_retained_as_document_level_status_without_erasing_other_research() -> None:
    scanned = _document("https://www.nseindia.com/scanned.pdf", "").model_copy(update={
        "document_type": DocumentType.PDF_REFERENCE, "status": DocumentStatus.FAILED,
        "source_classification": SourceClassification.EXCHANGE,
    })
    company = PortfolioResearchCompany(company_name="Example", status="RESOLVED_PARTIAL_DATA")
    enrich_company_research(company, [scanned], [], Decimal("0.10"))
    assert company.quarterly_result_status == "PDF_SCANNED_OCR_REQUIRED"
    assert company.status == "RESOLVED_PARTIAL_DATA"


def _quarterly_fact(instrument_id: UUID, metric: str, value: str, period: str = "Q1 FY27") -> FinancialFact:
    return FinancialFact(
        FinancialFactKey(instrument_id, metric, period, "QUARTERLY", "CONSOLIDATED"),
        ProvenancedValue(
            value=Decimal(value), unit="INR crore" if metric != "eps" else "INR per share",
            source_url="https://nsearchives.nseindia.com/result.pdf", source_name="NSE",
            source_type="EXCHANGE_ANNOUNCEMENT", retrieved_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        ),
        FactSourceTier.OFFICIAL_NSE, "NSE", "document-id", SourceMode.REAL,
    )


def _selection_fact(
    instrument_id: UUID,
    metric: str,
    value: str,
    *,
    period: str,
    basis: str | None,
    tier: FactSourceTier,
    provider: str,
    period_type: str = "QUARTERLY",
) -> FinancialFact:
    source_name = "NSE" if tier == FactSourceTier.OFFICIAL_NSE else "Yahoo Finance"
    return FinancialFact(
        FinancialFactKey(instrument_id, metric, period, period_type, basis),
        ProvenancedValue(
            value=Decimal(value),
            unit="INR per share" if metric == "eps" else "INR crore",
            source_url=f"https://example.test/{provider.lower()}/{period}/{metric}",
            source_name=source_name,
            source_type="EXCHANGE_ANNOUNCEMENT" if tier == FactSourceTier.OFFICIAL_NSE else "STRUCTURED_FINANCIAL_STATEMENTS",
            published_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
            retrieved_at=datetime(2026, 8, 2, tzinfo=timezone.utc),
            confidence=0.91 if tier == FactSourceTier.OFFICIAL_NSE else 0.78,
        ),
        tier,
        provider,
        f"{provider}:{period}:{metric}",
        SourceMode.REAL,
    )


def test_latest_result_policy_newer_official_partial_period_beats_older_complete_yahoo() -> None:
    instrument_id = UUID("99999999-9999-9999-9999-999999999994")
    facts = [
        _selection_fact(instrument_id, "revenue", "120", period="2026-06-30", basis="CONSOLIDATED", tier=FactSourceTier.OFFICIAL_NSE, provider="NSE"),
        _selection_fact(instrument_id, "eps", "2.4", period="2026-06-30", basis="CONSOLIDATED", tier=FactSourceTier.OFFICIAL_NSE, provider="NSE"),
        *[_selection_fact(instrument_id, metric, value, period="2026-03-31", basis="UNKNOWN", tier=FactSourceTier.YAHOO, provider="YAHOO_FINANCE")
          for metric, value in (("revenue", "100"), ("pat", "10"), ("eps", "2.0"))],
    ]

    result = latest_quarterly_result_from_facts(facts)

    assert result is not None
    assert result.period == "2026-06-30"
    assert result.revenue.value == Decimal("120")
    assert result.eps.value == Decimal("2.4")
    assert result.pat is None


def test_latest_result_policy_composes_compatible_unknown_basis_fields_with_field_provenance() -> None:
    instrument_id = UUID("99999999-9999-9999-9999-999999999995")
    facts = [
        _selection_fact(instrument_id, "revenue", "120", period="2026-06-30", basis="UNKNOWN", tier=FactSourceTier.OFFICIAL_NSE, provider="NSE"),
        _selection_fact(instrument_id, "pat", "12", period="2026-06-30", basis="UNKNOWN", tier=FactSourceTier.OFFICIAL_NSE, provider="NSE"),
        _selection_fact(instrument_id, "eps", "2.4", period="2026-06-30", basis="UNKNOWN", tier=FactSourceTier.YAHOO, provider="YAHOO_FINANCE"),
    ]

    result = latest_quarterly_result_from_facts(facts)

    assert result is not None
    assert result.period == "2026-06-30"
    assert result.revenue.source_name == "NSE"
    assert result.pat.source_name == "NSE"
    assert result.eps.source_name == "Yahoo Finance"
    assert result.eps.source_url.endswith("/yahoo_finance/2026-06-30/eps")


def test_latest_result_policy_does_not_mix_yahoo_unknown_eps_into_official_consolidated_period() -> None:
    instrument_id = UUID("99999999-9999-9999-9999-999999999996")
    facts = [
        _selection_fact(instrument_id, "revenue", "120", period="2026-06-30", basis="CONSOLIDATED", tier=FactSourceTier.OFFICIAL_NSE, provider="NSE"),
        _selection_fact(instrument_id, "pat", "12", period="2026-06-30", basis="CONSOLIDATED", tier=FactSourceTier.OFFICIAL_NSE, provider="NSE"),
        _selection_fact(instrument_id, "eps", "2.4", period="2026-06-30", basis="UNKNOWN", tier=FactSourceTier.YAHOO, provider="YAHOO_FINANCE"),
    ]

    result = latest_quarterly_result_from_facts(facts)

    assert result is not None
    assert result.period == "2026-06-30"
    assert result.reporting_basis == "CONSOLIDATED"
    assert result.revenue.source_name == "NSE"
    assert result.pat.source_name == "NSE"
    assert result.eps is None


def test_latest_result_policy_newer_partial_official_beats_older_complete_official() -> None:
    instrument_id = UUID("99999999-9999-9999-9999-999999999997")
    facts = [
        _selection_fact(instrument_id, "revenue", "120", period="2026-06-30", basis="CONSOLIDATED", tier=FactSourceTier.OFFICIAL_NSE, provider="NSE"),
        *[_selection_fact(instrument_id, metric, value, period="2026-03-31", basis="CONSOLIDATED", tier=FactSourceTier.OFFICIAL_NSE, provider="NSE")
          for metric, value in (("revenue", "100"), ("pat", "10"), ("eps", "2.0"))],
    ]

    result = latest_quarterly_result_from_facts(facts)

    assert result is not None
    assert result.period == "2026-06-30"
    assert result.revenue.value == Decimal("120")
    assert result.pat is None
    assert result.eps is None


def test_latest_result_policy_earnings_event_does_not_advance_normalized_fact_period() -> None:
    instrument_id = UUID("99999999-9999-9999-9999-999999999998")
    facts = [_selection_fact(instrument_id, metric, value, period="2026-03-31", basis="CONSOLIDATED", tier=FactSourceTier.OFFICIAL_NSE, provider="NSE")
             for metric, value in (("revenue", "100"), ("pat", "10"), ("eps", "2.0"))]
    earnings_document = _document("https://www.nseindia.com/newer-earnings.pdf", "Earnings release.").model_copy(update={"instrument_id": instrument_id})
    earnings_event = ResearchEvent(
        instrument_id=instrument_id,
        company_id=UUID("99999999-9999-9999-9999-999999999997"),
        event_type=ResearchEventType.EARNINGS_RELEASE,
        event_date=datetime(2026, 6, 30, tzinfo=timezone.utc),
        title="Newer earnings release",
        summary="Earnings release evidence only.",
        source_document_id=earnings_document.document_id,
        source_url=earnings_document.canonical_url,
        source_type=SourceType.EXCHANGE_ANNOUNCEMENT,
        source_classification=SourceClassification.EXCHANGE,
        reliability=ReliabilityLevel.LEVEL_A,
        source_mode=SourceMode.REAL,
        confidence=0.9,
        impact=EventImpact.NEUTRAL,
        time_horizon=TimeHorizon.IMMEDIATE,
        raw_evidence_reference="Quarterly results announced for the quarter ended 30 June 2026.",
        published_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        retrieved_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )
    company = PortfolioResearchCompany(
        instrument_id=instrument_id, company_name="Example", status="RESOLVED_RESEARCH_AVAILABLE",
    )

    enrich_company_research(company, [earnings_document], [earnings_event], Decimal("0.10"), facts)

    assert earnings_event.event_type == ResearchEventType.EARNINGS_RELEASE
    assert company.latest_quarterly_result is not None
    assert company.latest_quarterly_result.period == "2026-03-31"


def test_latest_result_policy_financial_institution_does_not_interpret_non_result_metrics() -> None:
    instrument_id = UUID("99999999-9999-9999-9999-999999999999")
    facts = [
        *[_selection_fact(instrument_id, metric, value, period="2026-06-30", basis="CONSOLIDATED", tier=FactSourceTier.OFFICIAL_NSE, provider="NSE")
          for metric, value in (("revenue", "4"), ("pat", "1176.93"), ("eps", "19.15"))],
        _selection_fact(instrument_id, "debt_or_borrowings", "21710", period="2026-06-30", basis="CONSOLIDATED", tier=FactSourceTier.OFFICIAL_NSE, provider="NSE"),
        _selection_fact(instrument_id, "roe", "171", period="2026-06-30", basis="CONSOLIDATED", tier=FactSourceTier.OFFICIAL_NSE, provider="NSE"),
    ]

    result = latest_quarterly_result_from_facts(facts)

    assert result is not None
    assert result.revenue.value == Decimal("4")
    assert result.pat.value == Decimal("1176.93")
    assert result.eps.value == Decimal("19.15")
    assert result.debt_or_borrowings is None


def test_financial_history_returns_newest_four_explicit_quarters_without_deleting_history() -> None:
    instrument_id = UUID("99999999-9999-9999-9999-999999999990")
    periods = ["2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30", "2025-06-30"]
    facts = [_selection_fact(instrument_id, metric, value, period=period, basis="CONSOLIDATED", tier=FactSourceTier.OFFICIAL_NSE, provider="NSE")
             for period in periods for metric, value in (("revenue", "100"), ("pat", "10"))]
    history = financial_result_history_from_facts(facts, period_type="QUARTERLY")
    assert [item.period for item in history] == periods[:4]
    assert len({fact.key.period_end for fact in facts}) == 5
    latest = latest_quarterly_result_from_facts(facts)
    assert latest is not None and latest.period == history[0].period and latest.reporting_basis == history[0].reporting_basis


def test_financial_history_returns_newest_four_explicit_annuals_without_basis_composition() -> None:
    instrument_id = UUID("99999999-9999-9999-9999-999999999989")
    annuals = ["2026-03-31", "2025-03-31", "2024-03-31", "2023-03-31", "2022-03-31"]
    facts = [_selection_fact(instrument_id, metric, value, period=period, basis="CONSOLIDATED", tier=FactSourceTier.OFFICIAL_NSE, provider="NSE", period_type="ANNUAL")
             for period in annuals for metric, value in (("revenue", "100"), ("pat", "10"))]
    facts.extend(_selection_fact(instrument_id, "revenue", "9", period="2026-06-30", basis="STANDALONE", tier=FactSourceTier.OFFICIAL_NSE, provider="NSE") for _ in [0])
    history = financial_result_history_from_facts(facts, period_type="ANNUAL")
    assert [item.period for item in history] == annuals[:4]
    assert {item.reporting_basis for item in history} == {"CONSOLIDATED"}


def test_financial_history_mixed_optional_bases_is_deterministic_and_never_composes_fields() -> None:
    instrument_id = UUID("99999999-9999-9999-9999-999999999988")
    facts = [
        _selection_fact(instrument_id, "revenue", "120", period="2026-06-30", basis=None, tier=FactSourceTier.OFFICIAL_NSE, provider="NSE"),
        _selection_fact(instrument_id, "pat", "12", period="2026-06-30", basis="CONSOLIDATED", tier=FactSourceTier.OFFICIAL_NSE, provider="NSE"),
        _selection_fact(instrument_id, "revenue", "100", period="2026-03-31", basis="CONSOLIDATED", tier=FactSourceTier.OFFICIAL_NSE, provider="NSE"),
        _selection_fact(instrument_id, "pat", "10", period="2026-03-31", basis="CONSOLIDATED", tier=FactSourceTier.OFFICIAL_NSE, provider="NSE"),
    ]
    history = financial_result_history_from_facts(facts, period_type="QUARTERLY")
    assert [item.period for item in history] == ["2026-06-30", "2026-03-31"]
    assert {item.reporting_basis for item in history} == {"CONSOLIDATED"}
    assert history[0].revenue is None
    assert history[0].pat.value == Decimal("12")


def test_statement_history_projects_only_persisted_supported_metrics_without_basis_composition() -> None:
    instrument_id = UUID("99999999-9999-9999-9999-999999999987")
    facts = [
        _selection_fact(instrument_id, "total_assets", "500", period="2026-03-31", basis="CONSOLIDATED", tier=FactSourceTier.OFFICIAL_NSE, provider="NSE", period_type="AS_AT"),
        _selection_fact(instrument_id, "cash_and_cash_equivalents", "80", period="2026-03-31", basis="CONSOLIDATED", tier=FactSourceTier.OFFICIAL_NSE, provider="NSE", period_type="AS_AT"),
        _selection_fact(instrument_id, "total_assets", "900", period="2026-03-31", basis="STANDALONE", tier=FactSourceTier.OFFICIAL_NSE, provider="NSE", period_type="AS_AT"),
        _selection_fact(instrument_id, "operating_cash_flow", "100", period="2026-03-31", basis="CONSOLIDATED", tier=FactSourceTier.OFFICIAL_NSE, provider="NSE", period_type="ANNUAL"),
        _selection_fact(instrument_id, "pat", "10", period="2026-03-31", basis="CONSOLIDATED", tier=FactSourceTier.OFFICIAL_NSE, provider="NSE", period_type="ANNUAL"),
    ]
    balance = financial_statement_history_from_facts(facts, period_type="AS_AT", metrics={"total_assets", "cash_and_cash_equivalents"})
    cash_flow = financial_statement_history_from_facts(facts, period_type="ANNUAL", metrics={"operating_cash_flow"})

    assert len(balance) == 1
    assert balance[0].reporting_basis == "CONSOLIDATED"
    assert balance[0].metrics["total_assets"].value == Decimal("500")
    assert balance[0].metrics["cash_and_cash_equivalents"].value == Decimal("80")
    assert len(cash_flow) == 1
    assert cash_flow[0].metrics["operating_cash_flow"].value == Decimal("100")


def test_enrichment_uses_complete_persisted_quarterly_facts_without_document_parser(monkeypatch) -> None:
    instrument_id = UUID("99999999-9999-9999-9999-999999999991")
    company = PortfolioResearchCompany(instrument_id=instrument_id, company_name="Example", status="RESOLVED_RESEARCH_AVAILABLE")
    facts = [_quarterly_fact(instrument_id, metric, value) for metric, value in (
        ("revenue", "1000"), ("pat", "110"), ("eps", "2.5"),
    )]
    monkeypatch.setattr(
        structured_research,
        "latest_quarterly_result",
        lambda _documents: (_ for _ in ()).throw(AssertionError("document financial parser must be bypassed")),
    )

    enrich_company_research(company, [_document("https://www.nseindia.com/large.pdf", "large normalized financial statement")], [], Decimal("0.10"), facts)

    assert company.latest_quarterly_result is not None
    assert company.latest_quarterly_result.period == "Q1 FY27"
    assert company.latest_quarterly_result.revenue.value == Decimal("1000")
    assert company.latest_quarterly_result.pat.value == Decimal("110")
    assert company.latest_quarterly_result.eps.value == Decimal("2.5")


def test_enrichment_uses_document_financial_parser_when_persisted_facts_are_absent(monkeypatch) -> None:
    company = PortfolioResearchCompany(company_name="Example", status="RESOLVED_RESEARCH_AVAILABLE")
    document = _document("https://www.nseindia.com/q1.pdf", "Q1 FY27 revenue 1,000 PAT 110 EPS 2.5")
    original = structured_research.latest_quarterly_result
    calls = 0

    def recording_parser(documents):
        nonlocal calls
        calls += 1
        return original(documents)

    monkeypatch.setattr(structured_research, "latest_quarterly_result", recording_parser)
    enrich_company_research(company, [document], [], Decimal("0.10"), [])

    assert calls == 1
    assert company.latest_quarterly_result is not None
    assert company.latest_quarterly_result.revenue.value == Decimal("1000")


def test_partial_persisted_quarterly_facts_remain_primary_when_eps_is_missing(monkeypatch) -> None:
    instrument_id = UUID("99999999-9999-9999-9999-999999999992")
    company = PortfolioResearchCompany(instrument_id=instrument_id, company_name="Example", status="RESOLVED_RESEARCH_AVAILABLE")
    document = _document("https://www.nseindia.com/q1.pdf", "Q1 FY27 revenue 1,000 PAT 110 EPS 2.5")
    original = structured_research.latest_quarterly_result
    calls = 0

    def recording_parser(documents):
        nonlocal calls
        calls += 1
        return original(documents)

    monkeypatch.setattr(structured_research, "latest_quarterly_result", recording_parser)
    facts = [_quarterly_fact(instrument_id, "revenue", "999"), _quarterly_fact(instrument_id, "pat", "99")]
    enrich_company_research(company, [document], [], Decimal("0.10"), facts)

    assert calls == 0
    assert company.latest_quarterly_result is not None
    assert company.latest_quarterly_result.revenue.value == Decimal("999")
    assert company.latest_quarterly_result.pat.value == Decimal("99")
    assert company.latest_quarterly_result.eps is None


@pytest.mark.parametrize(
    ("status", "document_type"),
    [
        (DocumentStatus.FAILED, DocumentType.PDF_REFERENCE),
        ("FAILED", "PDF_REFERENCE"),
        (DocumentStatus.FAILED, "PDF_REFERENCE"),
        ("FAILED", DocumentType.PDF_REFERENCE),
    ],
)
def test_scanned_pdf_enrichment_accepts_enum_and_legacy_string_document_fields(status, document_type) -> None:
    base = _document("https://www.nseindia.com/scanned-legacy.pdf", "")
    legacy = base.model_dump() | {"status": status, "document_type": document_type}
    scanned = ResearchDocument.model_construct(**legacy)
    company = PortfolioResearchCompany(company_name="Example", status="RESOLVED_PARTIAL_DATA")

    enrich_company_research(company, [scanned], [], Decimal("0.10"))

    assert company.quarterly_result_status == "PDF_SCANNED_OCR_REQUIRED"


def test_non_failed_or_non_pdf_legacy_document_fields_preserve_not_available_status() -> None:
    base = _document("https://www.nseindia.com/normal.html", "")
    processed_pdf = ResearchDocument.model_construct(**(base.model_dump() | {"status": "PROCESSED", "document_type": "PDF_REFERENCE"}))
    failed_html = ResearchDocument.model_construct(**(base.model_dump() | {"status": "FAILED", "document_type": "HTML"}))
    company = PortfolioResearchCompany(company_name="Example", status="RESOLVED_PARTIAL_DATA")

    enrich_company_research(company, [processed_pdf, failed_html], [], Decimal("0.10"))

    assert company.quarterly_result_status == "NOT_AVAILABLE"


def test_portfolio_summary_finalization_accepts_legacy_string_document_fields() -> None:
    repository = ResearchRepository()
    instrument_id = UUID("99999999-9999-9999-9999-999999999998")
    base = _document("https://www.nseindia.com/finalized-scanned.pdf", "")
    scanned = ResearchDocument.model_construct(**(base.model_dump() | {
        "instrument_id": instrument_id, "status": "FAILED", "document_type": "PDF_REFERENCE"
    }))
    repository.documents[scanned.document_id] = scanned
    orchestrator = PortfolioResearchOrchestrator(repository, Settings())
    summary = PortfolioResearchSummary(
        portfolio_id=UUID("99999999-9999-9999-9999-999999999997"),
        companies=[PortfolioResearchCompany(
            instrument_id=instrument_id, company_name="Example", asset_type="EQUITY", status="RESOLVED_PARTIAL_DATA"
        )],
    )
    try:
        finalized = orchestrator._finalize(summary)
    finally:
        import asyncio
        asyncio.run(orchestrator._client.aclose())

    assert finalized.companies[0].quarterly_result_status == "PDF_SCANNED_OCR_REQUIRED"


def test_scanned_official_pdf_is_retained_against_global_instrument_even_without_extractable_metrics() -> None:
    repository = ResearchRepository(Settings(research_live_enabled=True))
    profile = next(profile for profile in repository.profiles if profile.ticker == "RELIANCE")
    document = repository.ingest_fixture(
        original_url="https://nsearchives.nseindia.com/corporate/scanned.pdf", source_type=SourceType.EXCHANGE_ANNOUNCEMENT,
        source_name="NSE corporate announcements", publisher="NSE", content_type="application/pdf", body="",
        reliability=ReliabilityLevel.LEVEL_A, source_mode=SourceMode.REAL, source_classification=SourceClassification.EXCHANGE,
        expected_profile=profile, document_status=DocumentStatus.FAILED, allow_empty_content=True,
    )
    assert document.instrument_id == profile.instrument_id
    assert document.status == DocumentStatus.FAILED
    assert document.document_id in repository.documents


def test_bank_quarterly_result_extracts_bank_metrics_without_industrial_ebitda_assumptions() -> None:
    document = _document(
        "https://www.nseindia.com/ujjivan-results.pdf",
        "Q1 FY27 total income 2,000 PAT 300 EPS 2.5 NIM 8.2% ROA 1.4% ROE 15.6% Gross NPA 2.1% Net NPA 0.4% deposits 40,000 advances 35,000 capital adequacy ratio 24.1% credit cost 0.8%",
    ).model_copy(update={"source_classification": SourceClassification.EXCHANGE, "source_name": "NSE"})
    result = latest_quarterly_result([document])
    assert result is not None
    assert result.nim.value == Decimal("8.2")
    assert result.gross_npa.value == Decimal("2.1")
    assert result.capital_adequacy.value == Decimal("24.1")
    assert result.ebitda is None


def test_shareholding_comparison_uses_compatible_periods_and_percentage_points() -> None:
    document = _document(
        "https://www.nseindia.com/shareholding/reliance",
        "Q1 FY27 Q4 FY26 promoter holding 50.20% 50.00% FII/FPI 22.35% 22.20% DII 12.00% 12.05% public holding 15.45% 15.75%",
    ).model_copy(update={"source_classification": SourceClassification.EXCHANGE, "source_name": "NSE"})
    changes = {change.category: change for change in shareholding_changes([document])}
    assert changes["PROMOTER"].change_percentage_points == Decimal("0.20")
    assert changes["FII_FPI"].change_percentage_points == Decimal("0.15")
    assert changes["DII"].change_percentage_points == Decimal("-0.05")
    assert changes["PROMOTER"].current_period == "Q1 FY27"
    assert changes["PROMOTER"].previous_period == "Q4 FY26"
    assert changes["PROMOTER"].current.source_name == "NSE"


def test_shareholding_ignores_unofficial_or_non_comparable_evidence() -> None:
    unofficial = _document("https://blog.example/ownership", "Q1 FY27 Q4 FY26 promoter holding 51% 50%")
    one_period = _document("https://www.nseindia.com/ownership", "Q1 FY27 promoter holding 51%") \
        .model_copy(update={"source_classification": SourceClassification.EXCHANGE})
    assert shareholding_changes([unofficial, one_period]) == []


def _structured_shareholding_snapshot(
    period: tuple[int, int, int],
    values: list[tuple[ShareholdingCategory, str]],
    *,
    source_identity: str,
) -> ShareholdingSnapshot:
    return ShareholdingSnapshot(
        instrument_id=UUID("99999999-9999-9999-9999-999999999993"),
        period_end=datetime(*period, tzinfo=timezone.utc),
        source_provider="NSE",
        source_type="NSE_SHAREHOLDING_XBRL",
        source_identity_key=source_identity,
        source_url=f"https://nsearchives.nseindia.com/corporate/xbrl/{source_identity}.xml",
        confidence=Decimal("0.90"),
        reliability_level=ReliabilityLevel.LEVEL_A,
        source_mode=SourceMode.REAL,
        values=[
            ShareholdingSnapshotValue(category=category, percentage=Decimal(value))
            for category, value in values
        ],
    )


def test_structured_shareholding_changes_use_latest_two_distinct_periods_without_aggregation() -> None:
    snapshots = [
        _structured_shareholding_snapshot((2026, 6, 30), [
            (ShareholdingCategory.PROMOTER, "82.90"),
            # A duplicate/nested source row is deliberately not summed.
            (ShareholdingCategory.PROMOTER, "57.68"),
            (ShareholdingCategory.FII_FPI, "8.10"),
        ], source_identity="jun-2026"),
        _structured_shareholding_snapshot((2026, 3, 31), [
            (ShareholdingCategory.PROMOTER, "84.65"),
            (ShareholdingCategory.FII_FPI, "7.80"),
        ], source_identity="mar-2026"),
        _structured_shareholding_snapshot((2025, 12, 31), [
            (ShareholdingCategory.PROMOTER, "86.36"),
        ], source_identity="dec-2025"),
        _structured_shareholding_snapshot((2025, 9, 30), [
            (ShareholdingCategory.PROMOTER, "86.36"),
        ], source_identity="sep-2025"),
        # A second source for the current period must not become "previous".
        _structured_shareholding_snapshot((2026, 6, 30), [
            (ShareholdingCategory.PROMOTER, "82.90"),
        ], source_identity="jun-2026-duplicate").model_copy(update={
            "retrieved_at": datetime(2020, 1, 1, tzinfo=timezone.utc),
        }),
    ]

    changes = {change.category: change for change in shareholding_changes_from_snapshots(snapshots)}

    promoter = changes["PROMOTER"]
    assert promoter.current_period == "2026-06-30"
    assert promoter.previous_period == "2026-03-31"
    assert promoter.current.value == Decimal("82.90")
    assert promoter.previous.value == Decimal("84.65")
    assert promoter.change_percentage_points == Decimal("-1.75")
    assert promoter.current.source_url.endswith("jun-2026.xml")
    assert promoter.previous.source_url.endswith("mar-2026.xml")
    assert changes["FII_FPI"].change_percentage_points == Decimal("0.30")
    assert [
        snapshot.values[0].percentage
        for snapshot in snapshots[:4]
    ] == [Decimal("82.90"), Decimal("84.65"), Decimal("86.36"), Decimal("86.36")]


def test_structured_shareholding_one_or_no_period_uses_legacy_document_fallback() -> None:
    document = _document(
        "https://www.nseindia.com/shareholding/reliance",
        "Q1 FY27 Q4 FY26 promoter holding 50.20% 50.00%",
    ).model_copy(update={"source_classification": SourceClassification.EXCHANGE, "source_name": "NSE"})
    snapshot = _structured_shareholding_snapshot(
        (2026, 6, 30), [(ShareholdingCategory.PROMOTER, "82.90")], source_identity="only-period",
    )
    company = PortfolioResearchCompany(
        company_name="Example", status="RESOLVED_RESEARCH_AVAILABLE", shareholding_snapshots=[snapshot],
    )

    enrich_company_research(company, [document], [], Decimal("0.10"))

    assert len(company.shareholding_changes) == 1
    assert company.shareholding_changes[0].current_period == "Q1 FY27"
    assert company.shareholding_changes[0].previous_period == "Q4 FY26"

    no_snapshot_company = PortfolioResearchCompany(company_name="Example", status="RESOLVED_RESEARCH_AVAILABLE")
    enrich_company_research(no_snapshot_company, [document], [], Decimal("0.10"))
    assert no_snapshot_company.shareholding_changes == company.shareholding_changes


def test_valuation_requires_context_and_source_diversity_counts_domains() -> None:
    official = _document("https://company.example/results", "Q1 FY27 current P/E 16 sector P/E 20 peer P/E 22 ROE 18%")
    exchange = _document("https://www.nseindia.com/results", "Q1 FY27 historical P/E 21").model_copy(update={"source_classification": SourceClassification.EXCHANGE})
    duplicate = official.model_copy()
    assert valuation_assessment([official, exchange]).state == "CHEAP"
    assert valuation_assessment([_document("https://media.example/story", "Q1 FY27 current P/E 16")]).state == "UNKNOWN"
    diversity = source_diversity([official, exchange, duplicate])
    assert diversity.sources_found == 2
    assert diversity.domains_found == 2
    assert diversity.exchange_sources == 1


@pytest.mark.parametrize(("current", "sector", "state"), [("10", "15", "CHEAP"), ("18", "20", "FAIR"), ("30", "20", "EXPENSIVE"), ("8", "10", "CHEAP"), ("8.01", "10", "FAIR"), ("11.99", "10", "FAIR"), ("12", "10", "EXPENSIVE")])
def test_valuation_state_evidence_preserves_existing_boundaries(current, sector, state):
    result = valuation_assessment([_document("https://sector.example/a", f"current P/E {current} sector P/E {sector}")])
    assert result.state == state and result.state_evidence is not None
    assert result.state_evidence.primary_metric == "CURRENT_PE"
    assert result.state_evidence.benchmark_value == Decimal(sector)
    assert result.state_evidence.comparison_ratio == Decimal(current) / Decimal(sector)
    assert result.state_evidence.benchmarks[0].kind == "SECTOR_PE"
    assert result.state_evidence.benchmarks[0].value.source_url == "https://sector.example/a"


def test_valuation_state_evidence_keeps_individual_benchmark_provenance_and_unknown_null():
    sector = _document("https://sector.example/a", "current P/E 15 sector P/E 20")
    peer = _document("https://peer.example/b", "peer P/E 25")
    historical = _document("https://history.example/c", "historical P/E 30")
    result = valuation_assessment([sector, peer, historical])
    assert result.state == "CHEAP" and result.state_evidence.benchmark_value == Decimal("25")
    assert result.state_evidence.comparison_ratio == Decimal("0.6")
    assert {(item.kind, item.value.source_url) for item in result.state_evidence.benchmarks} == {("SECTOR_PE", "https://sector.example/a"), ("PEER_PE", "https://peer.example/b"), ("HISTORICAL_PE", "https://history.example/c")}
    assert valuation_assessment([_document("https://x", "sector P/E 20")]).state_evidence is None
    assert valuation_assessment([_document("https://x", "current P/E 20 peer P/E 0")]).state_evidence is None


@pytest.mark.parametrize(("text", "expected"), [
    ("Return Metrics: RoA improved 22 bps YoY to 1.22% and RoE expanded 171 bps YoY to 12.01%.", "12.01"),
    ("ROE increased 150 bps YoY to 14.50%.", "14.50"),
    ("ROE declined 80 bps YoY to 11.20%.", "11.20"),
    ("ROE stood at 13.64%.", "13.64"),
    ("ROE expanded to 13.69%.", "13.69"),
])
def test_document_roe_prefers_percentage_level_over_basis_point_delta(text, expected) -> None:
    value = valuation_assessment([_document("https://nse.example/federal", text)]).roe
    assert value is not None and value.value == Decimal(expected)
    assert value.value not in {Decimal("171"), Decimal("150"), Decimal("80")}


def test_category_freshness_uses_independent_configurable_windows() -> None:
    settings = Settings(
        research_quarterly_freshness_seconds=100,
        research_shareholding_freshness_seconds=200,
        research_catalyst_freshness_seconds=10,
        research_analyst_freshness_seconds=50,
    )
    repository = ResearchRepository(settings=settings)
    instrument_id = repository.profiles[0].instrument_id
    now = datetime.now(timezone.utc)
    repository._category_refresh[(instrument_id, "FINANCIAL_RESULTS")] = now
    assert not repository._category_is_fresh(instrument_id, "FINANCIAL_RESULTS", now)
    document = _document("https://exchange.example/results", "Quarterly financial results revenue and profit").model_copy(update={
        "instrument_id": instrument_id, "source_mode": SourceMode.REAL, "status": DocumentStatus.PROCESSED,
        "title": "Quarterly Financial Results",
    })
    repository.documents[document.document_id] = document
    assert repository._category_is_fresh(instrument_id, "FINANCIAL_RESULTS", now)
    assert repository.documents[document.document_id] is document
    assert not repository._category_is_fresh(instrument_id, "FINANCIAL_RESULTS", now.replace(year=now.year + 1))


def test_quarterly_financial_result_eligibility_uses_explicit_reporting_period_window() -> None:
    repository = ResearchRepository(settings=Settings())
    instrument_id = repository.profiles[0].instrument_id
    document = _document(
        "https://exchange.example/results",
        "Quarterly financial results for the quarter ended June 30, 2026 revenue 100 profit after tax 10",
    ).model_copy(update={
        "instrument_id": instrument_id,
        "source_mode": SourceMode.REAL,
        "status": DocumentStatus.PROCESSED,
        "title": "Quarterly Financial Results",
        "retrieved_at": datetime(2026, 7, 15, tzinfo=timezone.utc),
    })
    repository.documents[document.document_id] = document

    eligible, next_eligible = repository._category_is_eligible_to_check(
        instrument_id, "FINANCIAL_RESULTS", datetime(2026, 8, 15, tzinfo=timezone.utc)
    )
    assert eligible is False
    assert next_eligible == datetime(2026, 9, 1, tzinfo=timezone.utc)
    eligible, _ = repository._category_is_eligible_to_check(
        instrument_id, "FINANCIAL_RESULTS", datetime(2026, 9, 1, tzinfo=timezone.utc)
    )
    assert eligible is True


@pytest.mark.asyncio
async def test_failed_attempt_states_do_not_make_financial_results_fresh_or_skip_official_retry() -> None:
    class RecordingOfficialDiscovery:
        def __init__(self) -> None:
            self.calls: list[set[str]] = []

        async def discover(self, _profile, categories, _seen_urls):
            self.calls.append(set(categories))
            return []

    official = RecordingOfficialDiscovery()
    repository = ResearchRepository(settings=Settings(research_search_enabled=False), official_filing_discovery=official)
    profile = next(profile for profile in repository.profiles if profile.exchange in {"NSE", "XNSE"})
    now = datetime.now(timezone.utc)
    repository._category_refresh[(profile.instrument_id, "FINANCIAL_RESULTS")] = now
    for terminal in ("ZERO_CANDIDATES", "SEARCH_RETURNED_ZERO_RESULTS", "RESULTS_REJECTED", "DOCUMENT_FETCH_FAILED",
                     "EXTRACTION_EMPTY", "SEARCH_PROVIDER_UNAVAILABLE", "SKIPPED"):
        repository.last_live_error[profile.instrument_id] = terminal
        assert not repository._category_is_fresh(profile.instrument_id, "FINANCIAL_RESULTS", now)

    await repository._refresh_targeted(profile, set())

    assert any("FINANCIAL_RESULTS" in categories for categories in official.calls)
    assert not repository._category_is_fresh(profile.instrument_id, "FINANCIAL_RESULTS", datetime.now(timezone.utc))


def test_refresh_category_aliases_share_one_canonical_obligation() -> None:
    assert _canonical_refresh_category("Guidance") == "GUIDANCE"
    assert _canonical_refresh_category("GUIDANCE") == "GUIDANCE"
    assert _canonical_refresh_category("Customers") == "CLIENTS"
    assert _canonical_refresh_category("Orders & Backlog") == "ORDERS_BACKLOG"
    assert _canonical_refresh_category("Ownership") == "INSTITUTIONAL_ACTIVITY"
    assert _canonical_refresh_category("Regulatory") == "REGULATORY"
    assert _canonical_refresh_category("REGULATORY") == "REGULATORY"


def test_successful_no_change_check_throttles_missing_category_without_faking_evidence() -> None:
    repository = ResearchRepository(settings=Settings())
    instrument_id = repository.profiles[0].instrument_id
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)

    repository._mark_successful_categories_checked(instrument_id, {"Guidance"}, now)

    assert not repository._category_has_qualifying_evidence(instrument_id, "GUIDANCE")
    assert not repository._category_is_fresh(instrument_id, "GUIDANCE", now)
    eligible, next_eligible = repository._category_is_eligible_to_check(instrument_id, "GUIDANCE", now)
    assert eligible is False
    assert next_eligible == now + timedelta(days=1)
    eligible, _ = repository._category_is_eligible_to_check(instrument_id, "GUIDANCE", now + timedelta(days=1))
    assert eligible is True


@pytest.mark.asyncio
async def test_backfill_bypasses_due_gate_but_reuses_global_refresh_single_flight(monkeypatch) -> None:
    repository = ResearchRepository(settings=Settings(research_live_enabled=True, research_demo_enabled=False))
    instrument_id = repository.profiles[0].instrument_id
    calls = 0

    async def record_live(refreshed_instrument_id, _categories, *, force=False):
        nonlocal calls
        assert refreshed_instrument_id == instrument_id
        assert force is True
        calls += 1
        await asyncio.sleep(0)

    monkeypatch.setattr(repository, "_refresh_live", record_live)
    await asyncio.gather(repository.backfill(instrument_id), repository.backfill(instrument_id))

    assert calls == 1


@pytest.mark.asyncio
async def test_targeted_refresh_constrains_official_discovery_to_precomputed_due_categories(monkeypatch) -> None:
    class RecordingOfficialDiscovery:
        def __init__(self) -> None:
            self.calls: list[set[str]] = []

        async def discover(self, _profile, categories, _seen_urls):
            self.calls.append(set(categories))
            return []

    official = RecordingOfficialDiscovery()
    repository = ResearchRepository(
        settings=Settings(research_search_enabled=False),
        official_filing_discovery=official,
    )
    profile = next(profile for profile in repository.profiles if profile.exchange in {"NSE", "XNSE"})
    guidance_due = _InstrumentRefreshGate({"GUIDANCE"}, False, False, "INCOMPLETE")
    monkeypatch.setattr(repository, "_instrument_refresh_gate", lambda *_args, **_kwargs: guidance_due)

    await repository._refresh_targeted(profile, set())

    # A stale/incomplete instrument does not authorize financial filing work
    # when FINANCIAL_RESULTS was filtered out before discovery.
    assert official.calls == []


@pytest.mark.asyncio
async def test_financial_results_due_category_is_the_only_official_discovery_category(monkeypatch) -> None:
    class RecordingOfficialDiscovery:
        def __init__(self) -> None:
            self.calls: list[set[str]] = []

        async def discover(self, _profile, categories, _seen_urls):
            self.calls.append(set(categories))
            return []

    official = RecordingOfficialDiscovery()
    repository = ResearchRepository(
        settings=Settings(research_search_enabled=False),
        official_filing_discovery=official,
    )
    profile = next(profile for profile in repository.profiles if profile.exchange in {"NSE", "XNSE"})
    financial_due = _InstrumentRefreshGate({"FINANCIAL_RESULTS"}, False, False, "INCOMPLETE")
    monkeypatch.setattr(repository, "_instrument_refresh_gate", lambda *_args, **_kwargs: financial_due)

    await repository._refresh_targeted(profile, set())

    assert official.calls == [{"FINANCIAL_RESULTS"}]


@pytest.mark.asyncio
async def test_provider_degraded_zero_result_does_not_record_successful_no_change_check(monkeypatch) -> None:
    class Stats:
        candidate_count = 0
        accepted_count = 0
        rejected_reasons: dict[str, int] = {}
        provider_failure_count = 1

        def reject(self, _reason: str) -> None:
            pass

    class DegradedSearchDiscovery:
        provider = type("Provider", (), {"provider_name": "fixture-search"})()

        def __init__(self) -> None:
            self.last_stats = Stats()

        async def discover(self, _profile, _categories, _seen_urls):
            return []

    repository = ResearchRepository(settings=Settings(
        research_search_enabled=True,
        research_search_provider="searxng",
        research_search_endpoint="https://search.example/search",
    ))
    profile = repository.profiles[0]
    repository._search_discovery = DegradedSearchDiscovery()
    guidance_due = _InstrumentRefreshGate({"GUIDANCE"}, False, False, "INCOMPLETE")
    monkeypatch.setattr(repository, "_instrument_refresh_gate", lambda *_args, **_kwargs: guidance_due)

    await repository._refresh_targeted(profile, set())

    assert (profile.instrument_id, "GUIDANCE") not in repository._category_successful_no_change_checks


@pytest.mark.asyncio
async def test_regulatory_successful_no_change_is_throttled_under_its_canonical_key(monkeypatch) -> None:
    class Stats:
        candidate_count = 0
        accepted_count = 0
        rejected_reasons: dict[str, int] = {}
        provider_failure_count = 0

        def reject(self, _reason: str) -> None:
            pass

    class ZeroResultSearchDiscovery:
        provider = type("Provider", (), {"provider_name": "fixture-search"})()

        def __init__(self) -> None:
            self.last_stats = Stats()
            self.categories: list[set[str]] = []

        async def discover(self, _profile, categories, _seen_urls):
            self.categories.append(set(categories))
            return []

    repository = ResearchRepository(settings=Settings(
        research_search_enabled=True,
        research_search_provider="searxng",
        research_search_endpoint="https://search.example/search",
    ))
    profile = repository.profiles[0]
    search = ZeroResultSearchDiscovery()
    repository._search_discovery = search
    regulatory_due = _InstrumentRefreshGate({"REGULATORY"}, False, False, "INCOMPLETE")
    monkeypatch.setattr(repository, "_instrument_refresh_gate", lambda *_args, **_kwargs: regulatory_due)

    await repository._refresh_targeted(profile, set())

    assert search.categories == [{"Regulatory"}]
    checked_at = repository._category_successful_no_change_checks[(profile.instrument_id, "REGULATORY")]
    eligible, next_eligible = repository._category_is_eligible_to_check(profile.instrument_id, "Regulatory", checked_at)
    assert eligible is False
    assert next_eligible == checked_at + timedelta(days=1)


@pytest.mark.asyncio
async def test_regulatory_provider_failure_remains_immediately_retryable(monkeypatch) -> None:
    class Stats:
        candidate_count = 0
        accepted_count = 0
        rejected_reasons: dict[str, int] = {}
        provider_failure_count = 1

        def reject(self, _reason: str) -> None:
            pass

    class FailedSearchDiscovery:
        provider = type("Provider", (), {"provider_name": "fixture-search"})()

        def __init__(self) -> None:
            self.last_stats = Stats()

        async def discover(self, _profile, _categories, _seen_urls):
            return []

    repository = ResearchRepository(settings=Settings(
        research_search_enabled=True,
        research_search_provider="searxng",
        research_search_endpoint="https://search.example/search",
    ))
    profile = repository.profiles[0]
    repository._search_discovery = FailedSearchDiscovery()
    regulatory_due = _InstrumentRefreshGate({"REGULATORY"}, False, False, "INCOMPLETE")
    monkeypatch.setattr(repository, "_instrument_refresh_gate", lambda *_args, **_kwargs: regulatory_due)

    await repository._refresh_targeted(profile, set())

    assert (profile.instrument_id, "REGULATORY") not in repository._category_successful_no_change_checks
    eligible, next_eligible = repository._category_is_eligible_to_check(
        profile.instrument_id, "REGULATORY", datetime.now(timezone.utc)
    )
    assert eligible is True
    assert next_eligible is None


@pytest.mark.asyncio
async def test_portfolio_refresh_executes_public_research_once_for_duplicate_global_instrument(monkeypatch) -> None:
    repository = ResearchRepository(settings=Settings(research_live_enabled=True, research_search_enabled=False))
    global_instrument_id = UUID("77777777-7777-7777-7777-777777777778")
    first = _portfolio_position("INE000K01002", "BROKER_ONE", "NSE", "Example Components Limited")
    second = _portfolio_position("INE000K01002", "BROKER_TWO", "NSE", "Example Components Limited")
    for position in (first, second):
        position["instrument"].update({
            "globalInstrumentId": str(global_instrument_id),
            "country": "IN",
            "providerMappings": [{"provider": "NSE", "providerSymbol": "EXAMPLE", "status": "VERIFIED"}],
        })
    calls: list[UUID] = []

    async def record_refresh(instrument_id, **_kwargs):
        calls.append(instrument_id)
        return repository.summary(instrument_id, allow_demo=False)

    monkeypatch.setattr(repository, "refresh", record_refresh)
    orchestrator = PortfolioResearchOrchestrator(
        repository,
        Settings(portfolio_service_base_url="http://portfolio-service", research_live_enabled=True, research_search_enabled=False),
        client=_RecordingPortfolioClient([first, second]),
        structured_provider=_UnavailableStructuredProvider(),
    )
    refresh_instrument_calls: list[UUID] = []
    original_refresh_instrument = orchestrator.refresh_instrument

    async def record_orchestration_refresh(instrument_id: UUID, **kwargs):
        refresh_instrument_calls.append(instrument_id)
        return await original_refresh_instrument(instrument_id, **kwargs)

    monkeypatch.setattr(orchestrator, "refresh_instrument", record_orchestration_refresh)

    await orchestrator.refresh_portfolio(UUID("aaaaaaaa-1111-1111-1111-111111111111"))

    assert calls == [global_instrument_id]
    assert refresh_instrument_calls == [global_instrument_id]


@pytest.mark.asyncio
@pytest.mark.parametrize(("concurrency", "expected_peak"), [(1, 1), (2, 2)])
async def test_portfolio_refresh_uses_bounded_instrument_concurrency_and_preserves_order(monkeypatch, concurrency, expected_peak) -> None:
    repository = ResearchRepository(settings=Settings(research_live_enabled=True, research_search_enabled=False))
    positions = []
    expected_ids: list[UUID] = []
    for index in range(3):
        instrument_id = UUID(f"77777777-7777-7777-7777-7777777777{80 + index}")
        expected_ids.append(instrument_id)
        position = _portfolio_position(f"INE000K010{10 + index}", f"ALIAS{index}", "NSE", f"Example {index} Limited")
        position["instrument"].update({
            "globalInstrumentId": str(instrument_id),
            "country": "IN",
            "providerMappings": [{"provider": "NSE", "providerSymbol": f"EXAMPLE{index}", "status": "VERIFIED"}],
        })
        positions.append(position)
    active = 0
    peak = 0
    calls: list[UUID] = []

    async def delayed_refresh(instrument_id, **_kwargs):
        nonlocal active, peak
        calls.append(instrument_id)
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        return repository.summary(instrument_id, allow_demo=False)

    monkeypatch.setattr(repository, "refresh", delayed_refresh)
    orchestrator = PortfolioResearchOrchestrator(
        repository,
        Settings(
            portfolio_service_base_url="http://portfolio-service",
            research_live_enabled=True,
            research_search_enabled=False,
            structured_provider_enabled=False,
            portfolio_refresh_instrument_concurrency=concurrency,
        ),
        client=_RecordingPortfolioClient(positions),
        structured_provider=_UnavailableStructuredProvider(),
    )

    result = await orchestrator.refresh_portfolio(UUID("aaaaaaaa-1111-1111-1111-111111111111"))

    assert peak == expected_peak
    assert calls == expected_ids
    assert [company.instrument_id for company in result.companies] == expected_ids


@pytest.mark.asyncio
async def test_portfolio_refresh_isolates_one_instrument_failure_from_other_workers(monkeypatch) -> None:
    repository = ResearchRepository(settings=Settings(research_live_enabled=True, research_search_enabled=False))
    first_id = UUID("77777777-7777-7777-7777-777777777790")
    second_id = UUID("77777777-7777-7777-7777-777777777791")
    positions = []
    for instrument_id, ticker, isin in ((first_id, "FAIL", "INE000K01020"), (second_id, "OK", "INE000K01021")):
        position = _portfolio_position(isin, ticker, "NSE", f"Example {ticker} Limited")
        position["instrument"].update({
            "globalInstrumentId": str(instrument_id), "country": "IN",
            "providerMappings": [{"provider": "NSE", "providerSymbol": ticker, "status": "VERIFIED"}],
        })
        positions.append(position)
    calls: list[UUID] = []

    async def selectively_fail(instrument_id, **_kwargs):
        calls.append(instrument_id)
        if instrument_id == first_id:
            raise RuntimeError("fixture failure")
        return repository.summary(instrument_id, allow_demo=False)

    monkeypatch.setattr(repository, "refresh", selectively_fail)
    orchestrator = PortfolioResearchOrchestrator(
        repository,
        Settings(portfolio_service_base_url="http://portfolio-service", research_live_enabled=True,
                 research_search_enabled=False, structured_provider_enabled=False,
                 portfolio_refresh_instrument_concurrency=2),
        client=_RecordingPortfolioClient(positions), structured_provider=_UnavailableStructuredProvider(),
    )

    result = await orchestrator.refresh_portfolio(UUID("aaaaaaaa-1111-1111-1111-111111111111"))

    assert calls == [first_id, second_id]
    assert result.companies[0].safe_error_code == "RuntimeError"
    assert result.companies[1].instrument_id == second_id


def test_indian_etf_identity_is_classified_before_company_orchestration() -> None:
    assert _instrument_asset_type({"assetType": "EQUITY", "securityType": "STK", "ticker": "GOLDBEES",
        "canonicalName": "NIPPON INDIA ETF GOLD BEES"}) == "ETF"
    assert _instrument_asset_type({"assetType": "EQUITY", "securityType": "STK", "ticker": "HDFCN50",
        "companyName": "HDFC NIFTY Next 50 ETF"}) == "ETF"


def test_dynamic_eu_legal_names_have_public_page_aliases() -> None:
    assert _company_aliases("Arcadis NV", "ARCAD") == ["Arcadis", "ARCAD"]
    assert _company_aliases("Aalberts N.V.", "AALB") == ["Aalberts", "AALB"]
    assert _company_aliases("RENK Group AG", "R3NK") == ["RENK Group", "R3NK"]


def test_exchange_source_is_authoritative_without_being_a_company_domain() -> None:
    repository = ResearchRepository()
    profile = next(profile for profile in repository.profiles if profile.ticker == "RELIANCE")
    source = RegisteredResearchSource(
        source_id="nse-results", instrument_id=profile.instrument_id,
        url="https://www.nseindia.com/companies-listing/corporate-filings-financial-results",
            source_type=SourceType.EXCHANGE_ANNOUNCEMENT, source_classification=SourceClassification.EXCHANGE,
        source_name="NSE", publisher="NSE", reliability_level=ReliabilityLevel.LEVEL_A,
        priority=2, categories=("FINANCIAL_RESULTS",),
    )
    repository._validate_registered_source(profile, source)


def _pdf_network_fixture() -> NetworkFetchResult:
    return NetworkFetchResult(
        final_url="https://nsearchives.nseindia.com/corporate/fixture.pdf",
        status_code=200,
        content_type="application/pdf",
        content=b"%PDF-fixture",
        headers={},
        is_redirect=False,
        encoding=None,
        headers_elapsed_ms=1,
        body_elapsed_ms=1,
        network_elapsed_ms=2,
    )


@pytest.mark.asyncio
async def test_pdf_extraction_runs_off_event_loop_and_allows_event_loop_progress(monkeypatch) -> None:
    fetcher = HttpResearchFetcher(Settings(research_pdf_extraction_concurrency=1))
    main_thread = threading.get_ident()
    worker_started = threading.Event()
    release_worker = threading.Event()
    worker_thread_ids: list[int] = []

    def blocking_extract(_response, *, max_bytes=None):
        worker_thread_ids.append(threading.get_ident())
        worker_started.set()
        release_worker.wait(timeout=2)
        return FetchResult("https://nsearchives.nseindia.com/corporate/fixture.pdf", 200, "application/pdf", "text", 10)

    monkeypatch.setattr(fetcher, "process_network_response", blocking_extract)
    task = asyncio.create_task(fetcher.process_network_response_async(_pdf_network_fixture(), extraction_timeout_seconds=1))
    while not worker_started.is_set():
        await asyncio.sleep(0)
    progressed = False
    await asyncio.sleep(0)
    progressed = True
    assert progressed is True
    assert worker_thread_ids == [worker_thread_ids[0]]
    assert worker_thread_ids[0] != main_thread
    release_worker.set()
    assert (await task).text == "text"


@pytest.mark.asyncio
async def test_timed_out_pdf_thread_retains_extraction_permit_until_actual_completion(monkeypatch) -> None:
    fetcher = HttpResearchFetcher(Settings(research_pdf_extraction_concurrency=1))
    first_started = threading.Event()
    release_first = threading.Event()
    calls = 0
    active = 0
    peak = 0
    lock = threading.Lock()

    def blocking_first_then_fast(_response, *, max_bytes=None):
        nonlocal calls, active, peak
        with lock:
            calls += 1
            ordinal = calls
            active += 1
            peak = max(peak, active)
        try:
            if ordinal == 1:
                first_started.set()
                release_first.wait(timeout=2)
            return FetchResult("https://nsearchives.nseindia.com/corporate/fixture.pdf", 200, "application/pdf", "text", 10)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(fetcher, "process_network_response", blocking_first_then_fast)
    first = asyncio.create_task(fetcher.process_network_response_async(_pdf_network_fixture(), extraction_timeout_seconds=0.01))
    while not first_started.is_set():
        await asyncio.sleep(0)
    with pytest.raises(PdfExtractionTimeoutError, match="PDF_EXTRACTION_TIMEOUT"):
        await first
    second = asyncio.create_task(fetcher.process_network_response_async(_pdf_network_fixture(), extraction_timeout_seconds=1))
    await asyncio.sleep(0.03)
    assert calls == 1
    assert peak == 1
    release_first.set()
    assert (await second).text == "text"
    assert calls == 2
    assert peak == 1


@pytest.mark.asyncio
async def test_pdf_extraction_concurrency_two_never_runs_more_than_two_workers(monkeypatch) -> None:
    fetcher = HttpResearchFetcher(Settings(research_pdf_extraction_concurrency=2))
    started = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    active = 0
    peak = 0

    def blocking_extract(_response, *, max_bytes=None):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            if active == 2:
                started.set()
        release.wait(timeout=2)
        with lock:
            active -= 1
        return FetchResult("https://nsearchives.nseindia.com/corporate/fixture.pdf", 200, "application/pdf", "text", 10)

    monkeypatch.setattr(fetcher, "process_network_response", blocking_extract)
    tasks = [asyncio.create_task(fetcher.process_network_response_async(_pdf_network_fixture(), extraction_timeout_seconds=1)) for _ in range(3)]
    while not started.is_set():
        await asyncio.sleep(0)
    await asyncio.sleep(0.02)
    assert peak == 2
    release.set()
    await asyncio.gather(*tasks)
    assert peak == 2


@pytest.mark.asyncio
async def test_cancelled_pdf_caller_keeps_permit_until_worker_exits(monkeypatch) -> None:
    fetcher = HttpResearchFetcher(Settings(research_pdf_extraction_concurrency=1))
    started = threading.Event()
    release = threading.Event()
    calls = 0

    def blocking_first_then_fast(_response, *, max_bytes=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            release.wait(timeout=2)
        return FetchResult("https://nsearchives.nseindia.com/corporate/fixture.pdf", 200, "application/pdf", "text", 10)

    monkeypatch.setattr(fetcher, "process_network_response", blocking_first_then_fast)
    first = asyncio.create_task(fetcher.process_network_response_async(_pdf_network_fixture(), extraction_timeout_seconds=1))
    while not started.is_set():
        await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    second = asyncio.create_task(fetcher.process_network_response_async(_pdf_network_fixture(), extraction_timeout_seconds=1))
    await asyncio.sleep(0.02)
    assert calls == 1
    release.set()
    assert (await second).text == "text"
    assert calls == 2


@pytest.mark.asyncio
async def test_pdf_network_download_is_not_serialized_by_extraction_admission(monkeypatch) -> None:
    fetcher = HttpResearchFetcher(Settings(research_pdf_extraction_concurrency=1))
    downloads_active = 0
    download_peak = 0

    async def concurrent_network(_url, **_kwargs):
        nonlocal downloads_active, download_peak
        downloads_active += 1
        download_peak = max(download_peak, downloads_active)
        await asyncio.sleep(0.01)
        downloads_active -= 1
        return _pdf_network_fixture()

    monkeypatch.setattr(fetcher, "fetch_network", concurrent_network)
    monkeypatch.setattr(fetcher, "process_network_response", lambda response, *, max_bytes=None: FetchResult(response.final_url, 200, "application/pdf", "text", 10))
    await asyncio.gather(fetcher.fetch("https://example.test/one.pdf"), fetcher.fetch("https://example.test/two.pdf"))
    assert download_peak == 2


def _document(url: str, text: str) -> ResearchDocument:
    return ResearchDocument(
        canonical_url=canonicalize_url(url),
        original_url=url,
        title="Fixture",
        source_type=SourceType.INVESTOR_RELATIONS,
        source_name="Fixture",
        publisher="DEMO",
        published_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        content_type="text/html",
        document_type="HTML",
        normalized_text=text,
        content_hash=content_hash(text),
        status=DocumentStatus.PARSED,
        reliability_level=ReliabilityLevel.LEVEL_B,
    )


def _aixtron_live_fixture() -> str:
    return """
    <html>
      <head><title>Strong momentum in optoelectronics continues</title></head>
      <body>
        <main>
          <p>Herzogenrath, April 14, 2026</p>
          <p>AIXTRON SE AIXA XETR DE000A0WMPJ6 announced a new order worth EUR 350 million from a leading optoelectronics customer.</p>
          <p>The company will invest EUR 120 million in CAPEX to expand production capacity and confirms guidance for the fiscal year.</p>
        </main>
      </body>
    </html>
    """


def _aixtron_mojibake_fixture() -> str:
    return """
    <html>
      <head><title>Strong momentum in optoelectronics continues</title></head>
      <body>
        <nav>AIXTRON Press Information &amp; Releases :: AIXTRON Navigation Suche EN German English Facebook Instagram linkedIn Xing Close search Search / HOME / press / Press Releases Annual Report</nav>
        <main>
          <h1>Strong momentum in optoelectronics continues</h1>
          <p>Order intake in H1 up 54% yoy / Q2 results in line with guidance / Strong free cash-flow generation / Volume ramp fully on track / Raised fullâyear 2026 guidance confirmed</p>
          <p>Herzogenrath, Germany, July 30, 2026 - AIXTRON SE (FSE: AIXA, ISIN DE000A0WMPJ6) benefitted from a strong order intake of EUR 214.5 million (+81% yoy) in the second quarter 2026, compared with EUR 284.6 million a year earlier and from EUR 257 million in the prior period.</p>
          <p>To serve all customers with shipments at their requested delivery dates, the company is now ramping up production capacity at its own premises and in close collaboration with its suppliers.</p>
          <p>The Executive Board confirms guidance for the full year 2026.</p>
        </main>
      </body>
    </html>
    """


def _registered_source(
    source_id: str,
    url: str,
    *,
    priority: int = 1,
    categories: tuple[str, ...] = ("Customers",),
    allowed: bool = True,
    reliability: ReliabilityLevel = ReliabilityLevel.LEVEL_B,
) -> RegisteredResearchSource:
    return RegisteredResearchSource(
        source_id=source_id,
        instrument_id=AIXTRON_INSTRUMENT_ID,
        url=url,
        source_type=SourceType.INVESTOR_RELATIONS,
        source_name="AIXTRON targeted source",
        publisher="AIXTRON SE",
        reliability_level=reliability,
        domain="www.aixtron.com",
        company_id=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1"),
        allowed=allowed,
        discovery_method="TEST_TARGETED",
        priority=priority,
        categories=categories,
    )


class _StaticDiscovery(ApprovedSourceDiscovery):
    def __init__(self, sources: list[RegisteredResearchSource]) -> None:
        self.sources = sources

    def discover(self, profile, missing_categories: set[str], already_seen_urls: set[str]) -> list[DiscoveryResult]:
        results: list[DiscoveryResult] = []
        for source in self.sources:
            if not source.allowed:
                continue
            if canonicalize_url(source.url) in already_seen_urls:
                continue
            if not set(source.categories) & missing_categories:
                continue
            if source.host != "www.aixtron.com":
                continue
            for category in sorted(set(source.categories) & missing_categories):
                results.append(DiscoveryResult(category=category, source=source))
        return results


class _StaticSearchProvider:
    provider_name = "test-search"

    def __init__(self, candidates: list[CandidateSearchResult]) -> None:
        self.candidates = candidates

    async def discover(self, company, category: str, date_window: SearchDateWindow) -> list[CandidateSearchResult]:
        return [candidate for candidate in self.candidates if candidate.category == category]


class _FailingSearchProvider:
    provider_name = "failing-search"

    def __init__(self, reason: str) -> None:
        self.reason = reason

    async def discover(self, company, category: str, date_window: SearchDateWindow) -> list[CandidateSearchResult]:
        raise SearchProviderError(self.reason)


class _RecordingSearchClient:
    def __init__(self, response_or_exception) -> None:
        self.response_or_exception = response_or_exception
        self.calls: list[dict] = []

    async def get(self, url: str, params: dict, headers: dict | None = None) -> httpx.Response:
        self.calls.append({"url": url, "params": params, "headers": headers or {}})
        if isinstance(self.response_or_exception, Exception):
            raise self.response_or_exception
        return self.response_or_exception


class _RecordingPortfolioClient:
    def __init__(self, positions: list[dict]) -> None:
        self.positions = positions
        self.calls: list[dict] = []

    async def get(self, url: str, headers: dict | None = None) -> httpx.Response:
        self.calls.append({"url": url, "headers": headers or {}})
        return httpx.Response(200, json=self.positions, request=httpx.Request("GET", url))


def _portfolio_position(
    isin: str | None,
    ticker: str,
    exchange: str,
    company_name: str,
    *,
    provider: str | None = None,
    provider_instrument_id: str | None = None,
    asset_type: str = "EQUITY",
    data_freshness: str = "DEMO",
    broker_symbol: str | None = None,
    broker_description: str | None = None,
    broker_exchange: str | None = None,
    canonical_symbol: str | None = None,
    canonical_name: str | None = None,
    canonical_exchange: str | None = None,
    canonical_mic: str | None = None,
    security_type: str | None = None,
    provider_mappings: list[dict] | None = None,
) -> dict:
    return {
        "positionId": str(UUID(int=uuid_int_from_text(f"{isin}|{ticker}|position"))),
        "dataFreshness": data_freshness,
        "instrument": {
            "instrumentId": str(UUID(int=uuid_int_from_text(f"{isin}|{exchange}|{ticker}"))),
            "provider": provider,
            "providerInstrumentId": provider_instrument_id,
            "isin": isin,
            "ticker": ticker,
            "exchange": exchange,
            "mic": exchange,
            "companyName": company_name,
            "assetType": asset_type,
            "tradingCurrency": "EUR",
            "brokerSymbol": broker_symbol,
            "brokerDescription": broker_description,
            "brokerExchange": broker_exchange,
            "canonicalSymbol": canonical_symbol,
            "canonicalName": canonical_name,
            "canonicalExchange": canonical_exchange,
            "canonicalMic": canonical_mic,
            "securityType": security_type,
            "providerMappings": provider_mappings or [],
        },
    }


def _instrument_fixture_name(instrument: dict) -> str:
    return str(instrument.get("canonicalName") or instrument.get("companyName"))


def repo_etf_profile(isin: str, ticker: str, exchange: str, name: str, provider: str, provider_instrument_id: str) -> EtfResearchProfile:
    return EtfResearchProfile(
        instrument_id=UUID(int=uuid_int_from_text(f"{isin}|{exchange}|{ticker}")),
        fund_id=UUID(int=uuid_int_from_text(f"etf|{isin}|{ticker}|{exchange}|{name}")),
        fund_name=name,
        ticker=ticker,
        exchange=exchange,
        mic=exchange,
        provider=provider,
        provider_instrument_id=provider_instrument_id,
        isin=isin,
        currency="EUR",
        fund_provider="iShares",
        underlying_index="S&P 500",
        known_domains=["www.ishares.com"],
    )


def _company_fixture_domain(company_name: str) -> str:
    blocked = {"ag", "group", "n", "nv", "plc", "technologies"}
    for token in "".join(char.lower() if char.isalnum() else " " for char in company_name).split():
        if len(token) >= 4 and token not in blocked:
            return token
    return "company"


def uuid_int_from_text(value: str) -> int:
    return int.from_bytes(content_hash(value).encode("utf-8")[:16], "big")


def _candidate(url: str, category: str) -> CandidateSearchResult:
    return CandidateSearchResult(
        title="Candidate",
        url=url,
        snippet="Search snippet is discovery only",
        discovered_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        provider="test-search",
        query_id=f"{category}:1",
        query=f"AIXTRON {category} 2026",
        category=category,
    )
