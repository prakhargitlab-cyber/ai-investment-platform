from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

import httpx
import pytest
import respx

from app.deduplication import DocumentDeduplicator
from app.entity_resolution import EntityResolver
from app.extraction import RuleBasedEventExtractor
from app.models import DocumentStatus, EventImpact, ReliabilityLevel, ResearchDocument, SourceType
from app.normalization import canonicalize_url, content_hash, normalize_numbers
from app.repository import ResearchRepository
from app.research_fetching import HttpResearchFetcher, RestrictedFetchError
from app.scoring import CatalystScorer
from app.settings import Settings
from app.url_security import UnsafeUrlError, validate_public_http_url


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
    assert any(event.impact == EventImpact.NEGATIVE for event in events)
    assert all(0 < event.confidence <= 1 for event in events)


def test_catalyst_score_uses_negative_events_and_temporal_decay() -> None:
    repo = ResearchRepository()
    instrument_id = UUID("33333333-3333-3333-3333-333333333333")
    events = repo.events_for(instrument_id)
    score = CatalystScorer().score(instrument_id, events)
    assert 0 <= score.overall_score <= 100
    assert score.overall_score < 60


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
    assert repo.summary(UUID("11111111-1111-1111-1111-111111111111")).demo is True


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
async def test_http_fetch_does_not_retry_restricted_sources() -> None:
    settings = Settings(research_max_retries=2)
    fetcher = HttpResearchFetcher(settings)
    route = respx.get("https://example.com/private").mock(return_value=httpx.Response(403))
    with pytest.raises(RestrictedFetchError):
        await fetcher.fetch("https://example.com/private")
    assert route.call_count == 1


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
