"""Offline authorization, durability and real Radar serialization regressions.

All fund content is synthetic. No genuine AMC artifact is available in this
checkout; genuine-document acceptance remains a separate deployment blocker.
"""
from dataclasses import replace
from datetime import date
from decimal import Decimal
import json
import socket
from threading import RLock
from types import SimpleNamespace
from uuid import UUID

import pytest
import httpx

from app.etf_acquisition import EtfAcquisitionService
from app.etf_document_discovery import (
    OfficialDocumentRegistry, ingest_all_registered_documents, ingest_etf_facts_from_document,
)
from app.etf_evidence import EtfFact, EtfMetric as M, EtfProvenance
from app.etf_readiness import evaluate_etf_readiness
from app.etf_screening import screening_evidence
from app.etf_opportunity_cycle import run_etf_radar_cycle_async, run_etf_radar_cycle
from app.persistence import SqliteResearchPersistence
from test_etf_document_trust import (
    NOW, ID, ISIN, URL, HEADER, profile, document, registry_for, seed_listing, store,
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("These tests must not contact a provider")
    # Windows asyncio creates its own loopback socketpair during loop setup.
    # Block provider transports/DNS, while permitting that interpreter plumbing.
    monkeypatch.setattr(httpx.Client, "send", blocked)
    monkeypatch.setattr(httpx.AsyncClient, "send", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


def ingest(store, text, *, registry=None):
    return ingest_etf_facts_from_document(store, profile(), text, document_url=URL,
        registry=registry if registry is not None else registry_for(text), clock=lambda: NOW)


def test_registry_instances_do_not_share_authorization(store):
    text = HEADER + "AUM: INR 10 crore"
    approved = registry_for(text)
    unrelated = OfficialDocumentRegistry()
    assert approved.has_sources(ID) and not unrelated.has_sources(ID)
    with pytest.raises(ValueError, match="NOT_AUTHORIZED"):
        ingest(store, text, registry=unrelated)
    assert store.etf_facts(ID) == []
    assert ingest(store, text, registry=approved)["aum"]["outcome"] == "SUCCESS_WITH_DATA"


def test_registration_limits_duplicates_and_requires_review():
    registry = registry_for(HEADER)
    source, = registry.sources_for(ID)
    with pytest.raises(ValueError, match="already registered"):
        registry.register(source)
    for i in range(4):
        registry.register(replace(source, document_url=f"{URL}/{i}"))
    with pytest.raises(ValueError, match="LIMIT_EXCEEDED"):
        registry.register(replace(source, document_url=URL + "/overflow"))
    for change in ({"approval_reference": ""}, {"normalized_text_sha256": "fixture"}, {"scheme_isin": ""}):
        with pytest.raises(ValueError, match="APPROVAL_REQUIRED"):
            OfficialDocumentRegistry().register(replace(source, **change))


def test_batch_rejects_unregistered_content_and_reports_missing_content(store):
    registry = registry_for(HEADER)
    with pytest.raises(ValueError, match="UNREGISTERED"):
        ingest_all_registered_documents(store, profile(), {URL + "/other": HEADER}, registry=registry)
    result = ingest_all_registered_documents(store, profile(), {}, registry=registry)
    assert result["status"] == "PARTIAL_FAILURE"
    assert result["sources_processed"] == 0 and result["missing_documents"] == [URL]
    assert store.etf_facts(ID) == []


def test_registry_authority_is_enforced(store):
    text = HEADER + "AUM: INR 10 crore"
    result = ingest(store, text, registry=registry_for(text, authority="OFFICIAL_EXCHANGE"))
    assert result["aum"]["outcome"] == "TECHNICAL_FAILURE"
    assert "PROVENANCE_MISMATCH" in result["aum"]["reason"]
    assert store.etf_facts(ID) == []


def test_verified_registered_reporting_date_reaches_both_facts(store):
    text = HEADER + "AUM: INR 10 crore\nTER: 0.35%\nAUM and TER as of 2026-09-30"
    registry = registry_for(text, as_of_date=date(2026, 9, 30))
    result = ingest_all_registered_documents(store, profile(), {URL: text}, registry=registry, clock=lambda: NOW)
    assert result["status"] == "COMPLETED"
    assert {f.as_of_date for f in store.etf_facts(ID)} == {date(2026, 9, 30)}
    assert len(store.etf_facts(ID)) == 2


@pytest.mark.parametrize("line", ["", "Published: 2026-09-30", "Uploaded: 2026-09-30",
                                  "AUM and TER as of 2026-09-29"])
def test_registered_date_cannot_override_missing_or_conflicting_reporting_date(store, line):
    text = HEADER + "AUM: INR 10 crore\nTER: 0.35%\n" + line
    result = ingest(store, text, registry=registry_for(text, as_of_date=date(2026, 9, 30)))
    assert all(row["outcome"] == "TECHNICAL_FAILURE" for row in result.values())
    assert store.etf_facts(ID) == []


def test_separate_metric_dates_are_not_interchanged(store):
    text = HEADER + ("AUM: INR 10 crore\nTER: 0.35%\n"
                     "AUM reporting date: 2026-09-30\nTER effective date: 2026-10-01")
    result = ingest(store, text)
    assert result["aum"]["as_of_date"] == date(2026, 9, 30)
    assert result["expense_ratio"]["as_of_date"] == date(2026, 10, 1)


def test_reporting_date_cannot_postdate_retrieval(store):
    text = HEADER + "AUM: INR 10 crore (as of 2026-10-09)"
    service = EtfAcquisitionService(store, clock=lambda: NOW,
        document_registry=registry_for(text, retrieved_at=NOW.replace(day=8)))
    result = service.acquire_official_document(profile(), document(text,
        retrieved_at=NOW.replace(day=8)), M.AUM)
    assert result.outcome == "TECHNICAL_FAILURE"
    assert store.etf_facts(ID) == []


def test_ingestion_preserves_actual_document_retrieval_time(store):
    text = HEADER + "AUM: INR 10 crore (as of 2026-09-30)"
    registry = registry_for(text, retrieved_at=NOW.replace(day=8))
    assert ingest(store, text, registry=registry)["aum"]["outcome"] == "SUCCESS_WITH_DATA"
    fact, = store.etf_facts(ID)
    assert fact.provenance.retrieved_at == NOW.replace(day=8)
    assert fact.as_of_date == date(2026, 9, 30)


def test_repeat_ingestion_is_idempotent_and_conflicting_revision_rejected(store):
    text = HEADER + "AUM: INR 10 crore (as of 2026-09-30)"
    first = ingest(store, text)["aum"]
    repeated = ingest(store, text)["aum"]
    assert repeated["evidence_ids"] == first["evidence_ids"]
    assert len(store.etf_facts(ID, M.AUM)) == 1
    conflict = ingest(store, text.replace("10 crore", "11 crore"))["aum"]
    assert conflict["outcome"] == "TECHNICAL_FAILURE"
    assert "CONFLICTING_OFFICIAL_METRIC" in conflict["reason"]
    assert len(store.etf_facts(ID, M.AUM)) == 1
    assert store.etf_facts(ID, M.AUM)[0].value == Decimal("100000000")


def test_result_describes_current_attempt_not_highest_precedence_fact(store):
    recent = HEADER + "AUM: INR 10 crore (as of 2026-09-30)"
    older = HEADER + "AUM: INR 20 crore (as of 2026-08-31)"
    ingest(store, recent)
    result = ingest(store, older)["aum"]
    assert result["value"] == Decimal("200000000")
    assert result["as_of_date"] == date(2026, 8, 31)
    assert store.etf_facts(ID, M.AUM)[0].value == Decimal("100000000")


def test_non_inr_aum_is_preserved_without_currency_conversion_in_screening(store):
    result = ingest(store, HEADER + "AUM: USD 1.5 billion (as of 2026-09-30)")
    assert result["aum"]["unit"] == "USD"
    fact, = store.etf_facts(ID)
    assert fact.unit == "USD" and fact.value == Decimal("1500000000")
    screened = screening_evidence(store, ID, {"AUM": fact}, {}, now=NOW)
    assert screened["screening_metrics"]["aum"]["quality"] == "UNAVAILABLE"
    assert screened["screening_metrics"]["aum"]["value"] is None


def seed_market_evidence(store):
    seed_listing(store)
    for metric, value, unit in [(M.MARKET_PRICE, "100", "INR"), (M.TRADING_VOLUME, "500000", "shares")]:
        store.save_etf_evidence(EtfFact(instrument_id=ID, metric=metric, value=Decimal(value),
            unit=unit, as_of_date=NOW.date(), provenance=EtfProvenance(provider="NSE",
                source_type="EXCHANGE_ANNOUNCEMENT", source_identity="synthetic-quote:" + metric,
                source_url="https://www.nseindia.com/fixture", authority="OFFICIAL_EXCHANGE",
                retrieved_at=NOW, reliability_level="LEVEL_A")))


@pytest.mark.asyncio
@pytest.mark.parametrize("dated", [True, False])
async def test_document_to_reopened_database_to_actual_radar_candidate(tmp_path, monkeypatch, dated):
    path = tmp_path / "document-to-candidate.sqlite"
    store = SqliteResearchPersistence(path)
    seed_market_evidence(store)
    text = HEADER + "AUM: INR 5 crore\nTER: 0.35%"
    if dated:
        text += "\nAUM reporting date: 2026-09-30\nTER effective date: 2026-10-01"
    try:
        result = ingest(store, text)
        assert all(row["outcome"] == "SUCCESS_WITH_DATA" for row in result.values())
    finally:
        store._connection.close()
    reopened = SqliteResearchPersistence(path)
    try:
        facts = {fact.metric: fact for fact in reopened.etf_facts(ID)}
        assert facts[M.AUM].value == Decimal("50000000")
        assert facts[M.EXPENSE_RATIO].value == Decimal("0.35")
        assert facts[M.AUM].provenance.source_metadata["scheme_isin"] == ISIN
        assert facts[M.AUM].provenance.source_metadata["approval_reference"] == "offline-fixture-review"
        statuses, _ = evaluate_etf_readiness(reopened, ID, now=NOW)
        for status in statuses:
            if status.requirement in {M.AUM, M.EXPENSE_RATIO}:
                assert status.status == ("READY_FRESH" if dated else "MISSING")
                assert status.freshness == ("FRESH" if dated else "UNKNOWN_AS_OF")
        # Inject only the clock; exercise the actual async evaluate/serialize/enrich/save path.
        monkeypatch.setattr("app.etf_opportunity_cycle.run_etf_radar_cycle",
            lambda *args, **kwargs: run_etf_radar_cycle(*args, **kwargs, now=NOW))
        repository = SimpleNamespace(persistence=reopened, _persistence_worker_lock=RLock())
        payloads = []
        for cycle_id in (UUID(int=501), UUID(int=502)):
            await run_etf_radar_cycle_async(repository, None, candidate_ids=[ID], cycle_id=cycle_id)
            payloads.append(reopened.etf_radar_cycle(cycle_id))
        assert payloads[0]["candidates"] == payloads[1]["candidates"]
        payload = payloads[0]
        candidate, = payload["candidates"]
        assert candidate["disposition"] == "EVALUATED"
        assert candidate["global_instrument_id"] == str(ID)
        cost = next(f for f in candidate["rule_engine_result"]["factor_results"] if f["factor"] == "COST")
        assert cost["status"] == ("SCORED" if dated else "UNAVAILABLE")
        assert cost["normalized_score"] == (79.0 if dated else None)
        low_aum = next(g for g in candidate["risk_gates"] if g["gate_id"] == "VERY_LOW_AUM")
        assert low_aum["triggered"] is dated
        for key, expected, unit in [("aum", "50000000", "INR"), ("expense_ratio", "0.35", "PERCENT_PA")]:
            observation = candidate["screening_metrics"][key]
            assert observation == {"value": expected if dated else None, "unit": unit,
                                   "quality": "VALID" if dated else "UNAVAILABLE"}
        assert json.loads(json.dumps(payload)) == payload
    finally:
        reopened._connection.close()
    final_store = SqliteResearchPersistence(path)
    try:
        assert final_store.etf_radar_cycle(UUID(int=501)) == payload
    finally:
        final_store._connection.close()
