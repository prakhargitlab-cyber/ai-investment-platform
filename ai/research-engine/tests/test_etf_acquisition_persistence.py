"""ETF-2 tests: local SQLite and mocked transports only."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
from pydantic import ValidationError

from app.etf_acquisition import EtfAcquisitionService, parse_nse_etf_list, NSE_ETF_LIST_URL
from app.etf_evidence import (
    EtfAcquisitionAttempt, EtfAcquisitionOutcome as Outcome, EtfAuthority as Authority,
    EtfFact, EtfMetric as M, EtfNavObservation, EtfHoldingsSnapshot, EtfProvenance,
    evidence_id, etf_freshness,
)
from app.etf_persistence import SQLITE_SCHEMA
from app.models import EtfHolding, ResearchDocument, StructuredInstrumentResolution, StructuredMarketSnapshot, ProvenancedValue
from app.persistence import SqliteResearchPersistence
from test_etf_domain import profile

NOW = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)
ID = UUID(int=1)
HEADER = "Symbol,Underlying,SecurityName,DateofListing,MarketLot,ISINNumber,FaceValue\n"
CSV = HEADER + "ANY,Some reference,Example Fund,11-Aug-22,1,IN0000000001,1\n"


def provenance(**updates):
    data = dict(provider="NSE", source_type="EXCHANGE_ANNOUNCEMENT", source_identity="document:1",
        source_url="https://nsearchives.nseindia.com/document", authority=Authority.OFFICIAL_EXCHANGE,
        retrieved_at=NOW, reliability_level="LEVEL_A")
    return EtfProvenance(**(data | updates))


def fact(metric=M.EXPENSE_RATIO, value="0", **updates):
    data = dict(instrument_id=ID, metric=metric, value=Decimal(value), unit="percent",
        as_of_date=NOW.date(), provenance=provenance())
    return EtfFact(**(data | updates))


@pytest.fixture
def store(tmp_path):
    result = SqliteResearchPersistence(tmp_path / "etf.sqlite")
    yield result
    result._connection.close()


def test_discovery_normalizes_and_preserves_unresolved_identity():
    text = CSV.replace("IN0000000001", " in0000000001 ")
    listing, = parse_nse_etf_list(text, provenance())
    assert listing.isin == "IN0000000001" and listing.asset_type == "ETF"
    assert listing.instrument_id is None
    assert listing.underlying_reference == "Some reference"
    assert "inception_date" not in type(listing).model_fields


@pytest.mark.parametrize("asset_type,expected", [("ETF", ID), ("EQUITY", None)])
def test_master_binding_does_not_reclassify_equity(asset_type, expected):
    master = {"globalInstrumentId": str(ID), "assetType": asset_type, "isin": "IN0000000001", "primaryExchange": "NSE"}
    listing, = parse_nse_etf_list(CSV, provenance(), [master])
    assert listing.instrument_id == expected
    assert master["assetType"] == asset_type


def test_listing_rechecks_do_not_erase_canonical_identity(store):
    listing, = parse_nse_etf_list(CSV, provenance())
    bound = listing.model_copy(update={"instrument_id": ID})
    store.save_etf_evidence(bound)
    store.save_etf_evidence(listing.model_copy(update={"provenance": provenance(retrieved_at=NOW + timedelta(days=1))}))
    assert store.etf_listings() == [bound]


@pytest.mark.parametrize("text", ["<html>CAPTCHA</html>", "Symbol\nANY", CSV.replace("IN0000000001", "invalid"),
    CSV.replace("Example Fund", ""), CSV + "OTHER,Reference,Other Fund,1,1,IN0000000001,1\n"])
def test_discovery_malformed_is_not_empty(text):
    with pytest.raises((ValueError, ValidationError)):
        parse_nse_etf_list(text, provenance())


@pytest.mark.parametrize("symbol", ["RANDOM", "GOLD", "SILVER", "ANY123"])
def test_no_ticker_allowlist(symbol):
    listing, = parse_nse_etf_list(CSV.replace("ANY", symbol), provenance())
    assert listing.symbol == symbol


@pytest.mark.asyncio
@pytest.mark.parametrize("status,text,outcome", [(200, CSV, Outcome.SUCCESS_WITH_DATA),
    (200, HEADER, Outcome.SUCCESS_EMPTY), (200, "CAPTCHA", Outcome.TECHNICAL_FAILURE),
    (403, "denied", Outcome.TECHNICAL_FAILURE), (429, "limited", Outcome.TECHNICAL_FAILURE),
    (500, "error", Outcome.TECHNICAL_FAILURE)])
async def test_discovery_outcomes_and_durability(store, status, text, outcome):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(status, text=text))) as client:
        result = await EtfAcquisitionService(store, clock=lambda: NOW).discover_nse(client)
    assert result.outcome == outcome
    assert store.etf_attempts() == [result]
    assert len(store.etf_listings()) == (1 if outcome == Outcome.SUCCESS_WITH_DATA else 0)


@pytest.mark.asyncio
async def test_timeout_is_technical(store):
    client = SimpleNamespace(get=AsyncMock(side_effect=httpx.ReadTimeout("timed out")))
    result = await EtfAcquisitionService(store, clock=lambda: NOW).discover_nse(client)
    assert result.outcome == Outcome.TECHNICAL_FAILURE


@pytest.mark.parametrize("metric,value,unit", [
    (M.SUBTYPE, "DEBT", None), (M.ASSET_CLASS, "Debt", None), (M.BENCHMARK_INDEX, "Example Index", None),
    (M.AUM, Decimal("0"), "INR"), (M.EXPENSE_RATIO, Decimal("0"), "percent"),
    (M.AMC_FUND_HOUSE, "Example AMC", None), (M.FUND_INCEPTION_DATE, "2020-01-01", None),
    (M.REPLICATION_METHOD, "Physical", None), (M.TRACKING_ERROR, Decimal("0"), "percent"),
    (M.TRACKING_DIFFERENCE, Decimal("-1.25"), "percent"),
])
def test_all_fact_types_roundtrip_and_idempotent(store, metric, value, unit):
    original = EtfFact(instrument_id=ID, metric=metric, value=value, unit=unit, provenance=provenance())
    key = store.save_etf_evidence(original)
    again = original.model_copy(update={"provenance": provenance(retrieved_at=NOW + timedelta(days=1))})
    assert store.save_etf_evidence(again) == key
    assert store.etf_facts(ID, metric) == [original]


def test_facts_survive_reopening_database(store):
    original = fact()
    store.save_etf_evidence(original)
    reopened = SqliteResearchPersistence(store.database_path)
    try:
        assert reopened.etf_facts(ID) == [original]
    finally:
        reopened._connection.close()


def test_nav_separate_from_price_and_exact_decimal_identity(store):
    store.save_etf_evidence(fact(M.MARKET_PRICE, "100", unit="INR"))
    assert store.etf_navs(ID) == []
    nav = EtfNavObservation(instrument_id=ID, nav=Decimal("0.00"), currency="INR", nav_date=NOW.date(), provenance=provenance())
    first = store.save_etf_evidence(nav)
    assert store.save_etf_evidence(nav.model_copy(update={"nav": Decimal("0")})) == first
    assert len(store.etf_navs(ID)) == 1 and store.etf_navs(ID)[0].nav == 0
    assert store.etf_facts(ID)[0].value == 100
    with pytest.raises(ValidationError):
        fact(M.NAV)
    with pytest.raises(ValidationError):
        EtfNavObservation(instrument_id=ID, nav=None, currency="INR", nav_date=NOW.date(), provenance=provenance())


@pytest.mark.parametrize("reverse", [False, True])
def test_stronger_evidence_wins_independently_of_insertion_or_recency(store, reverse):
    official = fact(value="0.5", as_of_date=date(2026, 1, 1))
    secondary = fact(value="0.1", provenance=provenance(provider="YAHOO_FINANCE", authority=Authority.SECONDARY,
        retrieved_at=NOW + timedelta(days=10)))
    for row in ([secondary, official] if reverse else [official, secondary]):
        store.save_etf_evidence(row)
    assert store.etf_facts(ID, M.EXPENSE_RATIO)[0] == official
    assert etf_freshness(store.etf_facts(ID)[0], M.EXPENSE_RATIO, now=NOW) == "STALE"
    correction = fact(value="0.4", as_of_date=date(2026, 9, 1))
    store.save_etf_evidence(correction)
    assert store.etf_facts(ID)[0] == correction and len(store.etf_facts(ID)) == 3


def test_demo_cannot_displace_real(store):
    store.save_etf_evidence(fact(provenance=provenance(source_mode="DEMO")))
    assert store.etf_facts(ID) == []


def test_holdings_unresolved_missing_zero_and_no_shareholding_reuse(store):
    holding = EtfHolding(etf_instrument_id=ID, constituent_name="Unresolved bond", weight_percentage=Decimal("0"),
        as_of_date=NOW.date(), source_provider="NSE", source_locator="table:1:row:1")
    snapshot = EtfHoldingsSnapshot(instrument_id=ID, as_of_date=NOW.date(), provenance=provenance(), holdings=[holding])
    store.save_etf_evidence(snapshot)
    store.save_etf_evidence(snapshot)
    persisted, = store.etf_holdings(ID)
    assert persisted.holdings[0].constituent_identifier is None
    assert persisted.holdings[0].quantity is None and persisted.holdings[0].value is None
    assert persisted.holdings[0].weight_percentage == 0
    assert store.load_shareholding_snapshots() == []
    assert store._connection.execute("SELECT COUNT(*) FROM etf_holdings_positions").fetchone()[0] == 1


@pytest.mark.parametrize("metric,age,state", [(M.NAV, 0, "FRESH"), (M.NAV, 2, "STALE"),
    (M.AUM, 34, "FRESH"), (M.AUM, 36, "STALE"), (M.EXPENSE_RATIO, 91, "STALE"),
    (M.BENCHMARK_INDEX, 366, "STALE"), (M.NAV, -1, "FUTURE_DATED")])
def test_etf_only_freshness(metric, age, state):
    evidence = SimpleNamespace(as_of_date=NOW.date() - timedelta(days=age))
    assert etf_freshness(evidence, metric, now=NOW) == state


def test_failed_check_does_not_advance_evidence_clock(store):
    old = fact(as_of_date=date(2026, 1, 1))
    store.save_etf_evidence(old)
    attempt = EtfAcquisitionAttempt(instrument_id=ID, metric=M.EXPENSE_RATIO, provider="NSE", attempted_at=NOW,
        outcome=Outcome.TECHNICAL_FAILURE, reason="HTTP_429")
    store.save_etf_acquisition([], attempt)
    assert store.etf_attempts(ID) == [attempt]
    assert store.etf_facts(ID) == [old]
    assert etf_freshness(old, M.EXPENSE_RATIO, now=NOW) == "STALE"
    assert etf_freshness(fact(as_of_date=None), M.EXPENSE_RATIO, now=NOW) == "UNKNOWN_AS_OF"
    assert etf_freshness(None, M.NAV, now=NOW) == "MISSING"


def test_unavailable_requires_authoritative_declaration():
    with pytest.raises(ValidationError):
        EtfAcquisitionAttempt(instrument_id=ID, metric=M.NAV, provider="NSE", attempted_at=NOW,
            outcome=Outcome.GENUINELY_UNAVAILABLE, reason="HTTP_403")


def official_document(text):
    return ResearchDocument(instrument_id=ID, canonical_url="https://fund.example/factsheet", original_url="https://fund.example/factsheet",
        source_type="COMPANY_WEBSITE", source_classification="OFFICIAL_COMPANY", source_name="Fund",
        content_type="text/html", document_type="HTML", normalized_text=text, content_hash="fixture",
        status="PROCESSED", reliability_level="LEVEL_A", retrieved_at=NOW, source_mode="REAL")


# INR AUM formats (tested separately below)
TEST_AUM_TER_FORMATS = [
    (M.EXPENSE_RATIO, "Expense ratio: 0%", Outcome.SUCCESS_WITH_DATA, Decimal("0")),
    (M.AUM, "AUM: USD 1.25 bn", Outcome.SUCCESS_WITH_DATA, Decimal("1250000000")),
    (M.AUM, "AUM: EUR 500 mn", Outcome.SUCCESS_WITH_DATA, Decimal("500000000")),
    (M.AUM, "AUM: GBP 1.5 bn", Outcome.SUCCESS_WITH_DATA, Decimal("1500000000")),
    (M.EXPENSE_RATIO, "No fee information here", Outcome.SUCCESS_EMPTY, None),
    (M.EXPENSE_RATIO, "TER: not published", Outcome.GENUINELY_UNAVAILABLE, None),
    (M.EXPENSE_RATIO, "TER: unavailable after CAPTCHA", Outcome.TECHNICAL_FAILURE, None),
    (M.EXPENSE_RATIO, "Expense ratio: 0.2% TER: 0.3%", Outcome.TECHNICAL_FAILURE, None),
    (M.NAV, "NAV 100", Outcome.NOT_IMPLEMENTED, None),
]

TEST_AUM_INR_FORMATS = [
    (M.AUM, "AUM: ₹10 crore", Outcome.SUCCESS_WITH_DATA, Decimal("100000000")),
    (M.AUM, "AUM: ₹10.5 crore", Outcome.SUCCESS_WITH_DATA, Decimal("105000000")),
    (M.AUM, "AUM: ₹1,00,000 crore", Outcome.SUCCESS_WITH_DATA, Decimal("1000000000000")),  # 1 lakh crore = 1 trillion
    (M.AUM, "AUM: INR 50 crore", Outcome.SUCCESS_WITH_DATA, Decimal("500000000")),
    (M.AUM, "AUM: Rs. 25 lakh", Outcome.SUCCESS_WITH_DATA, Decimal("2500000")),
    (M.AUM, "AUM: Rs 10 lakh", Outcome.SUCCESS_WITH_DATA, Decimal("1000000")),
    (M.AUM, "AUM: ₹5 crore", Outcome.SUCCESS_WITH_DATA, Decimal("50000000")),
    (M.AUM, "AUM: INR 100 lakh", Outcome.SUCCESS_WITH_DATA, Decimal("10000000")),
    (M.AUM, "AUM: ₹10,000 crore", Outcome.SUCCESS_WITH_DATA, Decimal("100000000000")),
    (M.AUM, "AUM: ₹500 mn", Outcome.SUCCESS_WITH_DATA, Decimal("500000000")),  # ₹ with mn (million)
    (M.AUM, "AUM: ₹500 million", Outcome.SUCCESS_WITH_DATA, Decimal("500000000")),  # ₹ with million
]

TEST_TER_FORMATS = [
    (M.EXPENSE_RATIO, "Expense ratio: 0%", Outcome.SUCCESS_WITH_DATA, Decimal("0")),
    (M.EXPENSE_RATIO, "TER: 0.25%", Outcome.SUCCESS_WITH_DATA, Decimal("0.25")),
    (M.EXPENSE_RATIO, "Total expense ratio: 1.50%", Outcome.SUCCESS_WITH_DATA, Decimal("1.50")),
    (M.EXPENSE_RATIO, "TER: 0%", Outcome.SUCCESS_WITH_DATA, Decimal("0")),
    (M.EXPENSE_RATIO, "TER: not published", Outcome.GENUINELY_UNAVAILABLE, None),
]
@pytest.mark.parametrize("metric,text,outcome,value", TEST_AUM_TER_FORMATS)
def test_document_acquisition_distinguishes_missing_unsupported_and_failure(store, metric, text, outcome, value):
    result = EtfAcquisitionService(store, clock=lambda: NOW).acquire_official_document(
        profile(known_domains=["fund.example"]), official_document(text), metric)
    assert result.outcome == outcome
    rows = store.etf_facts(ID)
    if value is not None:
        assert rows[0].value == value and rows[0].as_of_date is None
        assert rows[0].provenance.authority == Authority.OFFICIAL_FUND
    else:
        assert rows == []
    if outcome == Outcome.GENUINELY_UNAVAILABLE:
        assert result.unavailability_evidence.source_url == "https://fund.example/factsheet"


@pytest.mark.parametrize("metric,text,outcome,value", TEST_AUM_INR_FORMATS)
def test_inr_aum_formats(store, metric, text, outcome, value):
    """INR AUM parsing: ₹, INR, Rs, Rs. with crore/lakh scales."""
    result = EtfAcquisitionService(store, clock=lambda: NOW).acquire_official_document(
        profile(known_domains=["fund.example"]), official_document(text), metric)
    assert result.outcome == outcome
    rows = store.etf_facts(ID)
    if value is not None:
        assert rows[0].value == value and rows[0].unit == "INR"
        assert rows[0].as_of_date is None
        assert rows[0].provenance.authority == Authority.OFFICIAL_FUND
    else:
        assert rows == []


@pytest.mark.parametrize("metric,text,outcome,value", TEST_TER_FORMATS)
def test_ter_formats(store, metric, text, outcome, value):
    """TER/expense ratio parsing with explicit percentage."""
    result = EtfAcquisitionService(store, clock=lambda: NOW).acquire_official_document(
        profile(known_domains=["fund.example"]), official_document(text), metric)
    assert result.outcome == outcome
    rows = store.etf_facts(ID)
    if value is not None:
        assert rows[0].value == value and rows[0].unit == "percent"
        assert rows[0].as_of_date is None
        assert rows[0].provenance.authority == Authority.OFFICIAL_FUND
    else:
        assert rows == []


def test_unverified_document_not_promoted_to_authoritative(store):
    result = EtfAcquisitionService(store).acquire_official_document(profile(), official_document("TER: 0.1%"), M.EXPENSE_RATIO)
    assert result.outcome == Outcome.TECHNICAL_FAILURE


def quote_snapshot(*, empty=False, undated=False):
    return StructuredMarketSnapshot(resolution=StructuredInstrumentResolution(instrument_id=ID, provider="YAHOO_FINANCE",
        provider_ticker="EXAMPLE.NS", company_name="Example Fund", quote_type="ETF", confidence=0.99, resolved_at=NOW),
        status="STRUCTURED_PROVIDER_AVAILABLE", retrieved_at=NOW, source_url="https://finance.yahoo.com/quote/EXAMPLE.NS",
        facts={} if empty else {"latestPrice": ProvenancedValue(value=Decimal("100"), unit="INR",
            source_name="Yahoo Finance", source_url="https://finance.yahoo.com/quote/EXAMPLE.NS", retrieved_at=NOW,
            as_of_date=None if undated else NOW)})


@pytest.mark.asyncio
@pytest.mark.parametrize("variant,outcome", [("data", Outcome.SUCCESS_WITH_DATA), ("empty", Outcome.SUCCESS_EMPTY),
    ("undated", Outcome.TECHNICAL_FAILURE), ("malformed", Outcome.TECHNICAL_FAILURE), ("timeout", Outcome.TECHNICAL_FAILURE)])
async def test_secondary_acquisition_truthful_outcomes(store, variant, outcome):
    provider = SimpleNamespace(provider_name="YAHOO_FINANCE", collect=AsyncMock(
        return_value={} if variant == "malformed" else quote_snapshot(empty=variant == "empty", undated=variant == "undated"),
        side_effect=httpx.ReadTimeout("timeout") if variant == "timeout" else None))
    result = await EtfAcquisitionService(store).acquire_secondary_quote(profile(), M.MARKET_PRICE, provider,
        {"instrumentId": str(ID), "assetType": "ETF"})
    assert result.outcome == outcome
    assert store.etf_navs(ID) == []


@pytest.mark.asyncio
async def test_yahoo_nav_quote_timestamp_is_not_accepted_as_nav_date(store):
    provider = SimpleNamespace(provider_name="YAHOO_FINANCE", collect=AsyncMock(return_value=quote_snapshot()))
    result = await EtfAcquisitionService(store).acquire_secondary_quote(profile(), M.NAV, provider,
        {"instrumentId": str(ID), "assetType": "ETF"})
    assert result.outcome == Outcome.NOT_IMPLEMENTED
    provider.collect.assert_not_called()
    assert store.etf_navs(ID) == []


def test_migration_adds_only_etf_tables_and_matches_sqlite_schema():
    path = Path(__file__).resolve().parents[3] / "services/research-service/src/main/resources/db/migration/V20__etf_evidence.sql"
    migration = path.read_text()
    assert SQLITE_SCHEMA.replace("IF NOT EXISTS ", "") in migration
    statements = [line.strip() for line in migration.splitlines() if line.strip() and not line.startswith("--")]
    assert not any(line.startswith(("ALTER", "DROP", "DELETE", "UPDATE")) for line in statements)
    # 6 ETF-1/2/3 evidence tables plus etf_radar_cycles (ETF Radar cycle
    # persistence, added alongside app.etf_opportunity_cycle).
    assert migration.count("CREATE TABLE etf_") == 7


def test_attempt_and_evidence_atomic_rollback(store, monkeypatch):
    original = store._insert_etf_evidence
    def fail_second(evidence):
        if evidence.metric == M.AUM:
            raise RuntimeError("write failure")
        return original(evidence)
    monkeypatch.setattr(store, "_insert_etf_evidence", fail_second)
    rows = [fact(), fact(M.AUM)]
    attempt = EtfAcquisitionAttempt(instrument_id=ID, metric="BATCH", provider="NSE", attempted_at=NOW,
        outcome=Outcome.SUCCESS_WITH_DATA, reason="fixture", evidence_ids=[evidence_id(row) for row in rows])
    with pytest.raises(RuntimeError):
        store.save_etf_acquisition(rows, attempt)
    assert store.etf_facts(ID) == [] and store.etf_attempts(ID) == []


def test_reuse_is_read_only_and_secondary_requires_explicit_opt_in(store):
    service = EtfAcquisitionService(store, clock=lambda: NOW)
    secondary = fact(M.MARKET_PRICE, "100", unit="INR", provenance=provenance(authority=Authority.SECONDARY))
    store.save_etf_evidence(secondary)
    assert service.reusable_evidence(ID, M.MARKET_PRICE) is None
    assert service.reusable_evidence(ID, M.MARKET_PRICE, allow_secondary=True) == secondary
    stale_official = fact(M.MARKET_PRICE, "90", as_of_date=date(2026, 1, 1), unit="INR")
    store.save_etf_evidence(stale_official)
    assert service.reusable_evidence(ID, M.MARKET_PRICE, allow_secondary=True) is None
    assert store.etf_attempts(ID) == []


def test_nav_and_holdings_authority_and_deduplication(store):
    primary = EtfNavObservation(instrument_id=ID, nav=Decimal("10"), currency="INR", nav_date=date(2026, 1, 1), provenance=provenance())
    secondary = primary.model_copy(update={"nav_date": NOW.date(), "nav": Decimal("20"),
        "provenance": provenance(authority=Authority.SECONDARY)})
    for row in [secondary, primary]:
        store.save_etf_evidence(row)
    assert store.etf_navs(ID)[0] == primary
    holdings = [EtfHolding(etf_instrument_id=ID, constituent_name=name, source_provider="NSE", as_of_date=NOW.date())
                for name in ["Unresolved A", "Unresolved B"]]
    snapshot = EtfHoldingsSnapshot(instrument_id=ID, as_of_date=NOW.date(), provenance=provenance(), holdings=holdings)
    key = store.save_etf_evidence(snapshot)
    assert store.save_etf_evidence(snapshot.model_copy(update={"holdings": list(reversed(holdings))})) == key
    assert len(store.etf_holdings(ID)) == 1


@pytest.mark.parametrize("metric,key", [(M.TRADING_VOLUME, "volume"), (M.BID, "bid"), (M.ASK, "ask")])
@pytest.mark.asyncio
async def test_secondary_supported_fields_and_zero(store, metric, key):
    snapshot = quote_snapshot()
    snapshot.facts[key] = snapshot.facts["latestPrice"].model_copy(update={"value": Decimal("0"), "unit": "shares" if key == "volume" else "INR"})
    provider = SimpleNamespace(provider_name="YAHOO_FINANCE", collect=AsyncMock(return_value=snapshot))
    result = await EtfAcquisitionService(store).acquire_secondary_quote(profile(), metric, provider,
        {"instrumentId": str(ID), "assetType": "ETF"})
    assert result.outcome == Outcome.SUCCESS_WITH_DATA
    assert store.etf_facts(ID, metric)[0].value == 0


@pytest.mark.asyncio
async def test_secondary_rejects_equity_identity_without_provider_call(store):
    provider = SimpleNamespace(provider_name="YAHOO_FINANCE", collect=AsyncMock(return_value=quote_snapshot()))
    result = await EtfAcquisitionService(store).acquire_secondary_quote(profile(), M.MARKET_PRICE, provider,
        {"instrumentId": str(ID), "assetType": "EQUITY"})
    assert result.outcome == Outcome.TECHNICAL_FAILURE
    provider.collect.assert_not_called()


def test_postgres_adapter_inherits_etf_sql_contract(store):
    """Exercise parameter translation and transactions; not a live PG test."""
    from app.postgres_persistence import PostgresResearchPersistence, _PostgresConnectionAdapter
    statements = []
    class ConnectionDouble:
        def execute(self, sql, params=None):
            statements.append(sql)
            assert "?" not in sql
            return store._connection.execute(sql.replace("%s", "?"), params or ())
        def commit(self):
            store._connection.commit()
        def rollback(self):
            store._connection.rollback()
    pg = object.__new__(PostgresResearchPersistence)
    pg._connection = _PostgresConnectionAdapter(ConnectionDouble())
    original = fact()
    pg.save_etf_evidence(original)
    pg.save_etf_evidence(original)
    assert pg.etf_facts(ID) == [original]
    assert any("ON CONFLICT DO NOTHING" in sql for sql in statements)
