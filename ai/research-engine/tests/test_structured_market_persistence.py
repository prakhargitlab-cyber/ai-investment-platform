import json
import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from app.models import ProvenancedValue, StructuredInstrumentResolution, StructuredMarketSnapshot, StructuredMarketSnapshotRecord
from app.persistence import SqliteResearchPersistence, _structured_snapshot_from_row
from app.portfolio_orchestration import _market_fundamentals_from_record, _public_analyst_from_record
from app.portfolio_orchestration import PortfolioResearchOrchestrator, _structured_due_classes
from app.repository import ResearchRepository
from app.settings import Settings
from app.structured_market import _is_financial_identity


def _record(instrument_id):
    now = datetime(2026, 9, 7, 5, tzinfo=timezone.utc)
    snapshot = StructuredMarketSnapshot(
        resolution=StructuredInstrumentResolution(provider="YAHOO_FINANCE", provider_ticker="ABC.NS", company_name="ABC", exchange="NSE", currency="INR", confidence=.9, resolved_at=now),
        status="SUCCESS", retrieved_at=now, market_as_of=now, source_url="https://example.test", facts={
            "latestPrice": ProvenancedValue(value=Decimal("250"), unit="INR", source_url="https://example.test", source_name="Yahoo", retrieved_at=now),
            "previousClose": ProvenancedValue(value=Decimal("245"), unit="INR", source_url="https://example.test", source_name="Yahoo", retrieved_at=now),
        },
    )
    return StructuredMarketSnapshotRecord(instrument_id=instrument_id, provider="YAHOO_FINANCE", provider_instrument_id="ABC.NS", exchange="NSE", currency="INR", source_url=snapshot.source_url, retrieved_at=now, persisted_at=now, last_price_at=now, last_success_at=now, last_provider_attempt_at=now, snapshot=snapshot)


def test_durable_structured_snapshot_survives_reconstruction_and_failure_preserves_payload(tmp_path):
    path = tmp_path / "structured.sqlite"
    instrument_id = uuid4()
    first = SqliteResearchPersistence(path)
    first.upsert_structured_market_snapshot(_record(instrument_id))
    first.record_structured_market_failure(instrument_id, "YAHOO_FINANCE", datetime.now(timezone.utc), "HTTP_503", "unavailable")
    second = SqliteResearchPersistence(path)
    record = second.load_structured_market_snapshots({instrument_id})[0]
    assert record.snapshot.facts["latestPrice"].value == Decimal("250")
    assert record.acquisition_status == "PROVIDER_UNAVAILABLE"
    assert record.last_failure_code == "HTTP_503"
    assert second.load_financial_facts() == []


def test_latest_price_snapshot_records_a_durable_market_price_observation(tmp_path):
    persistence = SqliteResearchPersistence(tmp_path / "prices.sqlite")
    instrument_id = uuid4()
    persistence.upsert_structured_market_snapshot(_record(instrument_id))
    observations = persistence.load_market_price_observations({instrument_id})
    assert len(observations) == 1
    assert observations[0].instrument_id == instrument_id
    assert observations[0].price == Decimal("250")
    assert observations[0].currency == "INR"


def test_structured_snapshot_row_accepts_sqlite_json_text_and_psycopg_decoded_jsonb(tmp_path):
    persistence = SqliteResearchPersistence(tmp_path / "payload.sqlite")
    instrument_id = uuid4()
    record = _record(instrument_id).model_copy(update={
        "provider_instrument_id": "FEDERALBNK.NS",
        "snapshot": _record(instrument_id).snapshot.model_copy(update={
            "facts": {
                "latestPrice": _record(instrument_id).snapshot.facts["latestPrice"].model_copy(update={"value": Decimal("344.6")}),
                "previousClose": _record(instrument_id).snapshot.facts["previousClose"],
            },
        }),
    })
    persistence.upsert_structured_market_snapshot(record)
    row = dict(persistence._connection.execute("SELECT * FROM global_structured_market_snapshots").fetchone())
    text_record = _structured_snapshot_from_row(row)
    row["facts_json"] = json.loads(row["facts_json"])
    native_record = _structured_snapshot_from_row(row)
    for reconstructed in (text_record, native_record):
        assert reconstructed.snapshot.facts["latestPrice"].value == Decimal("344.6")
        assert reconstructed.snapshot.facts["previousClose"].value == Decimal("245")
        assert reconstructed.provider == "YAHOO_FINANCE"
        assert reconstructed.provider_instrument_id == "FEDERALBNK.NS"
        assert reconstructed.snapshot.status == "SUCCESS"
        assert reconstructed.source_url == "https://example.test"


def test_durable_public_analyst_projection_is_partial_safe_and_keeps_legacy_targets_unset():
    record = _record(uuid4())
    now = record.retrieved_at
    facts = dict(record.snapshot.facts)
    for key, value in {
        "publicAnalystTargetLowPrice": "284.0", "publicAnalystTargetMedianPrice": "365.0", "publicAnalystTargetMeanPrice": "364.97144",
        "publicAnalystTargetHighPrice": "425.0", "publicAnalystCount": "35", "publicAnalystRecommendationMean": "2.02857",
    }.items():
        facts[key] = ProvenancedValue(value=Decimal(value), unit="INR" if "Target" in key else "ratio", source_url="https://finance.yahoo.com", source_name="Yahoo Finance", source_type="STRUCTURED_MARKET_PROVIDER", retrieved_at=now)
    facts["publicAnalystConsensus"] = ProvenancedValue(value="buy", source_url="https://finance.yahoo.com", source_name="Yahoo Finance", source_type="STRUCTURED_MARKET_PROVIDER", retrieved_at=now)
    analyst = _public_analyst_from_record(record.model_copy(update={"provider_instrument_id": "FEDERALBNK.NS", "last_analyst_at": now, "snapshot": record.snapshot.model_copy(update={"facts": facts})}), now, Settings())
    assert analyst.target_mean_price == Decimal("364.97144")
    assert analyst.target_low_price == Decimal("284.0")
    assert analyst.analyst_count == 35 and analyst.consensus == "buy"
    assert analyst.provider == "YAHOO_FINANCE" and analyst.provider_instrument_id == "FEDERALBNK.NS"
    assert analyst.source_name == "Yahoo Finance" and analyst.freshness == "FRESH"
    partial = _public_analyst_from_record(record.model_copy(update={"snapshot": record.snapshot.model_copy(update={"facts": {"publicAnalystConsensus": facts["publicAnalystConsensus"]}})}), now, Settings())
    assert partial.consensus == "buy" and partial.target_mean_price is None


def test_federal_like_durable_market_fundamentals_projection_is_exact_and_partial_safe():
    record = _record(uuid4()).model_copy(update={"provider_instrument_id": "FEDERALBNK.NS"})
    now = record.retrieved_at
    values = {"marketCap":"851075006464","enterpriseValue":"924042723328","trailingPE":"18.437668","forwardPE":"12.767692","priceToBook":"2.1213334","bookValue":"162.445","priceToSales":"5.5830674","evToRevenue":"6.062","trailingEps":"18.69","forwardEps":"26.99","profitMargin":"30.721","operatingMargin":"42.963","revenueGrowth":"21.400","earningsGrowth":"32.700","totalCash":"268973195264","totalDebt":"331932499968"}
    facts = {key: ProvenancedValue(value=Decimal(value), unit="INR", source_url="https://finance.yahoo.com/quote/FEDERALBNK.NS", source_name="Yahoo Finance", source_type="STRUCTURED_MARKET_PROVIDER", retrieved_at=now) for key, value in values.items()}
    full = record.model_copy(update={"last_fundamentals_at": now, "snapshot": record.snapshot.model_copy(update={"facts": facts})})
    projected = _market_fundamentals_from_record(full, now, Settings())
    assert projected.market_cap == Decimal(values["marketCap"])
    assert projected.enterprise_value == Decimal(values["enterpriseValue"])
    assert projected.trailing_pe == Decimal(values["trailingPE"])
    assert projected.book_value_per_share == Decimal(values["bookValue"])
    assert projected.trailing_eps == Decimal(values["trailingEps"]) and projected.forward_eps == Decimal(values["forwardEps"])
    assert projected.provider == "YAHOO_FINANCE" and projected.provider_instrument_id == "FEDERALBNK.NS"
    assert projected.source_name == "Yahoo Finance" and projected.freshness == "FRESH"
    partial = _market_fundamentals_from_record(full.model_copy(update={"snapshot": full.snapshot.model_copy(update={"facts": {"marketCap": facts["marketCap"], "trailingPE": facts["trailingPE"], "bookValue": facts["bookValue"]}})}), now, Settings())
    assert partial.market_cap == Decimal(values["marketCap"]) and partial.forward_pe is None and partial.book_value_per_share == Decimal(values["bookValue"])
    assert partial.roe is None


def test_financial_entity_raw_metrics_are_retained_with_semantic_restrictions():
    record = _record(uuid4())
    now = record.retrieved_at
    facts = dict(record.snapshot.facts)
    facts.update({
        "sector": ProvenancedValue(value="Financial Services", source_url="https://finance.yahoo.com", source_name="Yahoo Finance", retrieved_at=now),
        "debtToEquity": ProvenancedValue(value=Decimal("3"), source_url="https://finance.yahoo.com", source_name="Yahoo Finance", retrieved_at=now),
        "evToEbitda": ProvenancedValue(value=Decimal("9"), source_url="https://finance.yahoo.com", source_name="Yahoo Finance", retrieved_at=now),
        "operatingMargin": ProvenancedValue(value=Decimal("42"), source_url="https://finance.yahoo.com", source_name="Yahoo Finance", retrieved_at=now),
    })
    projected = _market_fundamentals_from_record(record.model_copy(update={"snapshot": record.snapshot.model_copy(update={"facts": facts})}), now, Settings())
    assert projected.debt_to_equity == Decimal("3") and projected.ev_to_ebitda == Decimal("9")
    assert projected.metric_semantics["debt_to_equity"] == "BANK_SPECIFIC_INTERPRETATION_REQUIRED"
    assert projected.metric_semantics["ev_to_ebitda"] == "NOT_MEANINGFUL_FOR_FINANCIAL_ENTITY"


def test_federal_shriram_and_industrial_identity_semantics_are_distinct():
    assert _is_financial_identity({"sector": "Financial Services", "industry": "Banks - Regional", "longName": "Federal Bank Limited"})
    assert _is_financial_identity({"sector": "Financial Services", "industry": "Credit Services", "longName": "Shriram Finance Limited"})
    assert not _is_financial_identity({"sector": "Industrials", "industry": "Aerospace & Defense", "longName": "Zen Technologies Limited"})


def test_durable_structured_due_classes_keep_initial_and_slower_work_eligible_when_market_closed():
    now = datetime(2026, 9, 7, 20, tzinfo=timezone.utc)
    settings = Settings()
    assert _structured_due_classes(None, "CLOSED", settings, now) == {"PRICE", "VALUATION", "FUNDAMENTALS", "ANALYST"}
    fresh = _record(uuid4()).model_copy(update={"last_price_at": now, "last_valuation_at": now, "last_fundamentals_at": now, "last_analyst_at": now, "last_success_at": now})
    assert _structured_due_classes(fresh, "CLOSED", settings, now) == set()
    price_stale = fresh.model_copy(update={"last_price_at": now.replace(year=2025)})
    assert _structured_due_classes(price_stale, "CLOSED", settings, now) == set()
    valuation_stale = fresh.model_copy(update={"last_valuation_at": now.replace(year=2025)})
    assert _structured_due_classes(valuation_stale, "UNKNOWN", settings, now) == {"VALUATION"}


def test_missing_durable_snapshot_reconciles_once_from_verified_mapping_and_fresh_snapshot_reuses_it():
    class Provider:
        provider_name = "YAHOO_FINANCE"
        def __init__(self): self.calls = 0
        async def collect(self, _instrument):
            self.calls += 1
            return _record(instrument_id).snapshot

    instrument_id = uuid4()
    repository = ResearchRepository(settings=Settings(research_demo_enabled=False))
    provider = Provider()
    orchestrator = PortfolioResearchOrchestrator(repository, Settings(), structured_provider=provider)
    instrument = {"instrumentId": str(instrument_id), "assetType": "EQUITY", "companyName": "Example Limited", "ticker": "EXAMPLE", "exchange": "NSE", "structuredProviderTicker": "EXAMPLE.NS", "structuredProviderStatus": "VERIFIED"}

    first = asyncio.run(orchestrator._reconcile_structured_market(instrument_id, instrument))
    fresh_at = datetime.now(timezone.utc)
    records = [_record(instrument_id).model_copy(update={
        "last_price_at": fresh_at, "last_valuation_at": fresh_at,
        "last_fundamentals_at": fresh_at, "last_analyst_at": fresh_at,
        "last_success_at": fresh_at,
    })]
    second = asyncio.run(orchestrator._reconcile_structured_market(instrument_id, instrument, records=records))

    assert first.snapshot is not None and provider.calls == 1
    assert records[0].snapshot.facts["latestPrice"].value == Decimal("250")
    assert second.due_classes == frozenset() and provider.calls == 1
