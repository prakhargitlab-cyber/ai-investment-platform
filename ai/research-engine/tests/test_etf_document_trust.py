"""Offline regressions. Values and example.com documents are synthetic fixtures."""
from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
from uuid import UUID

import pytest

from app.etf_acquisition import EtfAcquisitionService
from app.etf_document_discovery import OfficialDocumentRegistry, register_official_document
from app.etf_evidence import EtfListing, EtfMetric as M, EtfProvenance, etf_freshness
from app.models import EtfResearchProfile, ResearchDocument
from app.persistence import SqliteResearchPersistence

NOW = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
ID = UUID(int=42)
ISIN = "IN0000000042"
URL = "https://amc.example.com/factsheet"
HEADER = f"Scheme: Fixture ETF\nISIN: {ISIN}\n"


def profile():
    return EtfResearchProfile(instrument_id=ID, fund_id=ID, fund_name="Fixture ETF",
        ticker="FIXTURE", exchange="NSE", mic="XNSE", isin=ISIN, currency="INR",
        known_domains=["amc.example.com"])


def document(text, **updates):
    fields = dict(instrument_id=ID, canonical_url=URL, original_url=URL,
        source_type="COMPANY_WEBSITE", source_classification="OFFICIAL_COMPANY",
        source_name="Synthetic fixture", content_type="text/plain", document_type="HTML",
        normalized_text=text, content_hash=sha256(text.encode()).hexdigest(),
        status="PROCESSED", reliability_level="LEVEL_A", retrieved_at=NOW,
        published_at=NOW, source_mode="REAL")
    return ResearchDocument(**(fields | updates))


@pytest.fixture
def store():
    result = SqliteResearchPersistence(":memory:")
    seed_listing(result)
    yield result
    result._connection.close()


def seed_listing(result):
    result.save_etf_evidence(EtfListing(instrument_id=ID, isin=ISIN, symbol="FIXTURE",
        name="Fixture ETF", provenance=EtfProvenance(provider="NSE",
            source_type="NSE_OFFICIAL_ETF_SECURITY_LIST", source_identity="fixture-list",
            source_url="https://nsearchives.nseindia.com/content/equities/eq_etfseclist.csv",
            authority="OFFICIAL_EXCHANGE", reliability_level="LEVEL_A", retrieved_at=NOW)))


def registry_for(text, **updates):
    registry = OfficialDocumentRegistry()
    fields = dict(instrument_id=ID, document_url=URL, canonical_url=URL,
        source_name="Synthetic fixture", scheme_isin=ISIN, approval_reference="offline-fixture-review",
        normalized_text_sha256=sha256(text.encode()).hexdigest(), published_at=NOW, retrieved_at=NOW)
    register_official_document(registry=registry, **(fields | updates))
    return registry


def acquire(store, text, metric=M.AUM, **updates):
    return EtfAcquisitionService(store, clock=lambda: NOW, document_registry=registry_for(text)).acquire_official_document(
        profile(), document(text, **updates), metric)


@pytest.mark.parametrize("text", [
    HEADER.replace(ISIN, "IN0000000099") + "AUM: INR 10 crore",
    "AUM: INR 10 crore",
    HEADER.replace("Fixture ETF", "Different ETF") + "AUM: INR 10 crore",
    HEADER + "AUM: INR 10 crore\nScheme: Different ETF\nISIN: IN0000000099\nTER: 0.3%",
])
def test_scheme_identity_is_independent_of_caller_uuid(store, text):
    result = acquire(store, text)
    assert result.outcome == "TECHNICAL_FAILURE"
    assert store.etf_facts(ID) == []


@pytest.mark.parametrize("updates", [
    {"original_url": "https://untrusted.example/factsheet"},
    {"source_type": "EXCHANGE_ANNOUNCEMENT"},
])
def test_inconsistent_provenance_fails_closed(store, updates):
    assert acquire(store, HEADER + "AUM: INR 10 crore", **updates).outcome == "TECHNICAL_FAILURE"
    assert store.etf_facts(ID) == []


@pytest.mark.parametrize("text,metric", [
    ("Investor flows: INR 10 crore", M.AUM),
    ("AUM: INR 1,5 crore", M.AUM),
    ("AUM: -INR 10 crore", M.AUM),
    ("TER: 101%", M.EXPENSE_RATIO),
])
def test_unsafe_numeric_values_never_become_facts(store, text, metric):
    result = acquire(store, HEADER + text, metric)
    assert result.outcome != "SUCCESS_WITH_DATA"
    assert store.etf_facts(ID) == []


@pytest.mark.parametrize("metric,text,expected_date", [
    (M.AUM, "AUM: INR 10 crore (as of 2026-09-30)", date(2026, 9, 30)),
    (M.EXPENSE_RATIO, "TER: 0.35% (effective 2026-10-01)", date(2026, 10, 1)),
])
def test_explicit_metric_dates_reach_facts(store, metric, text, expected_date):
    assert acquire(store, HEADER + text, metric).outcome == "SUCCESS_WITH_DATA"
    fact, = store.etf_facts(ID, metric)
    assert fact.as_of_date == expected_date
    assert etf_freshness(fact, metric, now=NOW) == "FRESH"


def test_future_metric_date_rejected(store):
    assert acquire(store, HEADER + "AUM: INR 10 crore (as of 2026-10-10)").outcome == "TECHNICAL_FAILURE"
    assert store.etf_facts(ID) == []


def test_publication_date_is_not_metric_date(store):
    assert acquire(store, HEADER + "AUM: INR 10 crore").outcome == "SUCCESS_WITH_DATA"
    fact, = store.etf_facts(ID)
    assert fact.as_of_date is None
    assert etf_freshness(fact, M.AUM, now=NOW) == "UNKNOWN_AS_OF"


@pytest.mark.parametrize("updates", [
    {"instrument_id": UUID(int=99)}, {"source_mode": "DEMO"}, {"status": "DISCOVERED"},
    {"reliability_level": "LEVEL_C"}, {"source_classification": "EXCHANGE"},
    {"canonical_url": "https://amc.example.com.evil.example/factsheet"},
    {"canonical_url": "http://amc.example.com/factsheet"},
    {"canonical_url": "https://user:password@amc.example.com/factsheet"},
    {"canonical_url": "https://amc.example.com:8443/factsheet"},
    {"canonical_url": "https://127.0.0.1/factsheet"},
    {"content_hash": "not-the-approved-content"},
])
def test_existing_and_provenance_gates_are_preserved(store, updates):
    result = acquire(store, HEADER + "AUM: INR 10 crore", **updates)
    assert result.outcome == "TECHNICAL_FAILURE"
    assert store.etf_facts(ID) == []


def test_registry_required_even_for_direct_service(store):
    result = EtfAcquisitionService(store, clock=lambda: NOW).acquire_official_document(
        profile(), document(HEADER + "AUM: INR 10 crore"), M.AUM)
    assert result.outcome == "TECHNICAL_FAILURE"
    assert "NOT_AUTHORIZED" in result.reason


@pytest.mark.parametrize("content", [
    HEADER + "ISIN: IN0000000099\nAUM: INR 10 crore",
    HEADER + "Symbol: OTHERETF\nAUM: INR 10 crore",
    HEADER + "Scheme: Different ETF\nAUM: INR 10 crore",
])
def test_conflicting_content_identifiers_rejected(store, content):
    assert acquire(store, content).outcome == "TECHNICAL_FAILURE"
    assert store.etf_facts(ID) == []


def test_content_change_invalidates_authorization(store):
    text = HEADER + "AUM: INR 10 crore"
    service = EtfAcquisitionService(store, clock=lambda: NOW, document_registry=registry_for(text))
    result = service.acquire_official_document(profile(), document(text.replace("10", "20")), M.AUM)
    assert "CONTENT_HASH_MISMATCH" in result.reason
    assert store.etf_facts(ID) == []


@pytest.mark.parametrize("span_kind,accepted", [("whole", False), ("target", True), ("other", False)])
def test_multiple_schemes_need_reviewed_section_boundaries(store, span_kind, accepted):
    target = HEADER + "AUM: INR 10 crore\nTER: 0.35%\nAUM and TER as of 2026-09-30\n"
    other = "Scheme: Different ETF\nISIN: IN0000000099\nAUM: INR 99 crore\nTER: 0.8%\n"
    text = target + other
    span = {"whole": None, "target": (0, len(target)), "other": (len(target), len(text))}[span_kind]
    service = EtfAcquisitionService(store, clock=lambda: NOW, document_registry=registry_for(text, section_span=span))
    for metric in (M.AUM, M.EXPENSE_RATIO):
        result = service.acquire_official_document(profile(), document(text), metric)
        assert (result.outcome == "SUCCESS_WITH_DATA") == accepted, result.reason
    if accepted:
        assert store.etf_facts(ID, M.AUM)[0].value == Decimal("100000000")
        assert store.etf_facts(ID, M.EXPENSE_RATIO)[0].value == Decimal("0.35")
    else:
        assert store.etf_facts(ID) == []


def test_canonical_listing_is_required_separately_from_profile(store):
    empty = SqliteResearchPersistence(":memory:")
    try:
        result = acquire(empty, HEADER + "AUM: INR 10 crore")
        assert "CANONICAL_ETF_IDENTITY_NOT_READY" in result.reason
        assert empty.etf_facts(ID) == []
    finally:
        empty._connection.close()


def test_existing_nse_identity_conflict_gate_blocks_documents(store):
    from app.etf_evidence import EtfAcquisitionAttempt
    store.save_etf_acquisition([], EtfAcquisitionAttempt(instrument_id=ID, metric="IDENTITY",
        provider="NSE", attempted_at=NOW, outcome="TECHNICAL_FAILURE", reason="NSE_METADATA_IDENTITY_CONFLICT"))
    result = acquire(store, HEADER + "AUM: INR 10 crore")
    assert "CANONICAL_ETF_IDENTITY_NOT_READY" in result.reason
    assert store.etf_facts(ID) == []


@pytest.mark.parametrize("updates", [{"isin": None}, {"isin": "IN0000000099"}, {"ticker": "WRONG"}])
def test_caller_profile_cannot_override_official_listing(store, updates):
    text = HEADER + "AUM: INR 10 crore"
    service = EtfAcquisitionService(store, clock=lambda: NOW, document_registry=registry_for(text))
    result = service.acquire_official_document(profile().model_copy(update=updates), document(text), M.AUM)
    assert result.outcome == "TECHNICAL_FAILURE"
    assert store.etf_facts(ID) == []


@pytest.mark.parametrize("text", ["", HEADER + "CAPTCHA\nAUM: INR 10 crore"])
def test_empty_and_blocked_documents_are_failures(store, text):
    result = acquire(store, text)
    assert result.outcome == "TECHNICAL_FAILURE"
    assert store.etf_facts(ID) == []


@pytest.mark.parametrize("span", [(1, 10), (0, 1), (0, 100000), (10, 0)])
def test_invalid_section_bounds_cannot_extract_a_metric(store, span):
    text = HEADER + "AUM: INR 10 crore"
    service = EtfAcquisitionService(store, clock=lambda: NOW, document_registry=registry_for(text, section_span=span))
    result = service.acquire_official_document(profile(), document(text), M.AUM)
    assert result.outcome == "TECHNICAL_FAILURE"
    assert store.etf_facts(ID) == []
