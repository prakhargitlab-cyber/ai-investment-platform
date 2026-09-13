from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import httpx
import pytest

from app.global_scanner import CanonicalEquityUniverse, GlobalPreScore, GlobalScanner
from app.models import MarketPriceObservation, ProvenancedValue
from app.persistence import SqliteResearchPersistence
from app.fact_precedence import FinancialFact, FinancialFactKey, FactSourceTier
from test_structured_market_persistence import _record

NOW = datetime(2026, 9, 13, tzinfo=timezone.utc)


def instrument(n=1, **updates):
    return dict(globalInstrumentId=str(UUID(int=n)), canonicalName=f"Company {n}", ticker=f"C{n}",
                exchange="NSE", country="IN", currency="INR", status="ACTIVE", assetType="EQUITY",
                providerMappings=[dict(provider="YAHOO_FINANCE", providerSymbol="ABC.NS", status="VERIFIED")], **updates)


def persisted(store, item, metrics=None, age=0, price=True):
    key = UUID(item["globalInstrumentId"])
    stamp = NOW - timedelta(days=age)
    record = _record(key)
    record.currency = item["currency"]
    record.retrieved_at = record.market_as_of = stamp
    record.snapshot.facts = {k: ProvenancedValue(value=v, source_url="https://example.test", source_name="test", retrieved_at=stamp, as_of_date=stamp)
                             for k, v in (metrics if metrics is not None else {"profitMargin": 10, "roe": 5}).items()}
    store.upsert_structured_market_snapshot(record)
    if price:
        store.upsert_market_price_observation(MarketPriceObservation(instrument_id=key, observed_at=stamp, retrieved_at=stamp,
            price=Decimal(100), currency=item["currency"], provider="YAHOO_FINANCE", source_url="https://example.test"))


async def scan(items, store, **kwargs):
    def handler(request):
        assert request.method == "GET" and request.url.path == "/api/v1/instruments"
        assert request.url.params["status"] == "ACTIVE" and request.url.params["assetType"] == "EQUITY"
        return httpx.Response(200, json={"instruments": items, "totalElements": len(items)})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await GlobalScanner(CanonicalEquityUniverse(client, "http://canonical"), store).scan(as_of=NOW, top_n=kwargs.get("top_n", 10))


@pytest.mark.asyncio
@pytest.mark.parametrize("change,reason", [({}, None), ({"status": "INACTIVE"}, "INACTIVE"),
    ({"assetType": "ETF"}, "NON_EQUITY"), ({"providerMappings": []}, "UNTRUSTED_CANONICAL_IDENTITY"),
    ({"canonicalName": None}, "UNTRUSTED_CANONICAL_IDENTITY")])
async def test_eligibility(change, reason):
    store = SqliteResearchPersistence()
    item = instrument() | change
    persisted(store, item)
    result = await scan([item], store)
    candidate = result.candidates[0]
    assert candidate.eligible_for_deep_analysis == (reason is None)
    if reason: assert reason in candidate.exclusion_reasons


@pytest.mark.asyncio
async def test_no_price_and_stale_price_fail_critical_gate():
    store = SqliteResearchPersistence()
    items = [instrument(n) for n in range(1, 4)]
    persisted(store, items[0])
    persisted(store, items[1], price=False)
    persisted(store, items[2], age=20)
    result = await scan(items, store)
    by_id = {c.global_instrument_id.int: c for c in result.candidates}
    assert "NO_USABLE_PRICE" in by_id[2].exclusion_reasons
    assert "STALE_PRICE" in by_id[3].exclusion_reasons
    assert by_id[3].confidence < by_id[1].confidence
    assert by_id[3].stale_inputs == ["price"]


@pytest.mark.asyncio
async def test_missing_optional_renormalizes_and_actual_zero_survives():
    store = SqliteResearchPersistence()
    items = [instrument(n) for n in range(1, 4)]
    persisted(store, items[0], {"profitMargin": 10, "roe": 5, "revenueGrowth": None})
    persisted(store, items[1], {"profitMargin": 10, "roe": 5, "revenueGrowth": 0})
    persisted(store, items[2], {"profitMargin": 10, "roe": 5, "revenueGrowth": -10})
    result = await scan(items, store)
    a, b, c = result.candidates
    assert a.dimensions["GROWTH_QUALITY"].score is None
    assert b.dimensions["GROWTH_QUALITY"].score == 50
    assert c.dimensions["GROWTH_QUALITY"].score == 0
    assert a.pre_score > b.pre_score > c.pre_score


@pytest.mark.asyncio
async def test_determinism_ties_top_n_and_membership_independence():
    store = SqliteResearchPersistence()
    items = [instrument(n) for n in range(1, 13)]
    for item in items: persisted(store, item)
    first = await scan(items, store, top_n=7)
    private_items = [item | {"portfolioId": "private", "quantity": 900, "averageCost": 1, "PnL": -500,
                            "allocation": .9, "watchlistMembership": True} for item in reversed(items)]
    second = await scan(private_items, store, top_n=7)
    assert first == second
    assert [c.global_instrument_id.int for c in first.candidates] == list(range(1, 13))
    assert len(first.deep_analysis_candidate_ids) == 7


@pytest.mark.asyncio
@pytest.mark.parametrize("country,exchange,currency", [("IN", "NSE", "INR"), ("US", "XNAS", "USD"), ("DE", "XETR", "EUR")])
async def test_region_neutral_public_evidence(country, exchange, currency):
    store = SqliteResearchPersistence()
    item = instrument() | dict(country=country, exchange=exchange, currency=currency)
    persisted(store, item)
    candidate = (await scan([item], store)).candidates[0]
    assert candidate.eligible_for_deep_analysis
    assert candidate.market == exchange and candidate.country == country


@pytest.mark.asyncio
async def test_provider_adapters_and_v1_never_invoked(monkeypatch):
    def forbidden(*args, **kwargs): raise AssertionError("Provider or V1 invoked")
    from app.structured_market import YahooFinanceProvider
    from app.stock_rule_engine import StockRuleEngineService
    monkeypatch.setattr(YahooFinanceProvider, "__init__", forbidden)
    monkeypatch.setattr(StockRuleEngineService, "analyze", forbidden)
    monkeypatch.setattr(httpx.Client, "send", forbidden)
    store = SqliteResearchPersistence()
    item = instrument()
    persisted(store, item)
    assert (await scan([item], store)).eligible_candidates == 1


@pytest.mark.asyncio
async def test_conflicting_financial_evidence_and_future_prices():
    store = SqliteResearchPersistence()
    item = instrument()
    persisted(store, item, {"profitMargin": 10})
    store.upsert_financial_fact(FinancialFact(FinancialFactKey(UUID(int=1), "profitMargin", None, "ANNUAL"),
        ProvenancedValue(value=-10, source_url="https://example.test", source_name="official", retrieved_at=NOW),
        FactSourceTier.OFFICIAL_REGULATORY, "OFFICIAL", "fact"))
    candidate = (await scan([item], store)).candidates[0]
    assert candidate.dimensions["PROFITABILITY_QUALITY"].state == "CONFLICTING"
    assert not candidate.eligible_for_deep_analysis
    store = SqliteResearchPersistence()
    persisted(store, item, age=-1)
    assert "NO_USABLE_PRICE" in (await scan([item], store)).candidates[0].exclusion_reasons


@pytest.mark.asyncio
async def test_stale_financial_evidence_cannot_be_revived_by_retrieval():
    store = SqliteResearchPersistence()
    item = instrument()
    persisted(store, item, {"profitMargin": 5}, age=600)
    record = store.load_structured_market_snapshots({UUID(int=1)})[0]
    record.retrieved_at = NOW
    store.upsert_structured_market_snapshot(record)
    candidate = (await scan([item], store)).candidates[0]
    assert candidate.dimensions["PROFITABILITY_QUALITY"].state == "STALE"
    assert "profitMargin" in candidate.stale_inputs


@pytest.mark.asyncio
async def test_empty_universe_performs_no_persistence_reads():
    class NoReads:
        def __getattr__(self, name): raise AssertionError(name)
    assert (await scan([], NoReads())).candidates == []


def test_history_counts_distinct_days_and_rejects_currency_mismatch():
    store = SqliteResearchPersistence()
    item = instrument()
    persisted(store, item)
    prices = store.load_market_price_observations({UUID(int=1)})
    scorer = GlobalPreScore(history_points=2)
    candidate = scorer.score(item, [], [], prices*50, as_of=NOW)
    assert not candidate.technical_history_available
    prices[0].currency = "USD"
    assert not scorer.score(item, [], [], prices, as_of=NOW).price_data_available


@pytest.mark.asyncio
async def test_equal_prescore_orders_confidence_before_id():
    store = SqliteResearchPersistence()
    items = [instrument(1), instrument(2)]
    persisted(store, items[0], {"profitMargin": 10})
    persisted(store, items[1], {"profitMargin": 10, "roe": 5, "revenueGrowth": 10})
    candidates = (await scan(items, store)).candidates
    assert candidates[0].pre_score == candidates[1].pre_score
    assert candidates[0].confidence > candidates[1].confidence
    assert candidates[0].global_instrument_id.int == 2


@pytest.mark.asyncio
async def test_scanner_batches_requested_instruments_only():
    from app.persistence import DisabledResearchPersistence
    calls = []
    class Store(DisabledResearchPersistence):
        def load_financial_facts(self, ids): calls.append(("facts", ids)); return []
        def load_market_price_observations(self, ids): calls.append(("prices", ids)); return []
        def load_structured_market_snapshots(self, ids): calls.append(("snapshots", ids)); return []
    result = await scan([instrument(n) for n in range(1, 502)], Store())
    assert len(result.candidates) == 501
    assert [len(ids) for name, ids in calls if name == "facts"] == [250, 250, 1]
    assert len(calls) == 9


@pytest.mark.asyncio
async def test_untrusted_mapping_and_conflicting_price_fail_closed():
    store = SqliteResearchPersistence()
    item = instrument()
    persisted(store, item)
    price = store.load_market_price_observations({UUID(int=1)})[0]
    item["providerMappings"].append(dict(provider="OTHER", providerSymbol="ABC", status="VERIFIED"))
    store.upsert_market_price_observation(price.model_copy(update={"provider": "OTHER", "price": Decimal(200)}))
    candidate = (await scan([item], store)).candidates[0]
    assert "CONFLICTING_PRICE" in candidate.exclusion_reasons
    assert candidate.dimensions["PRICE_DATA_QUALITY"].score is None
    item["providerMappings"] = [dict(provider="YAHOO_FINANCE", providerSymbol="ABC.NS", status="VERIFIED", resolutionSource="BROKER_IMPORT_IDENTITY")]
    assert "UNTRUSTED_CANONICAL_IDENTITY" in (await scan([item], store)).candidates[0].exclusion_reasons


@pytest.mark.asyncio
async def test_official_persisted_profit_and_equity_support_india_without_structured_financials():
    store = SqliteResearchPersistence()
    item = instrument()
    persisted(store, item, {})
    for metric in ["pat", "equity"]:
        store.upsert_financial_fact(FinancialFact(FinancialFactKey(UUID(int=1), metric, "2026-06-30", "QUARTERLY"),
            ProvenancedValue(value=10, source_url="https://example.test", source_name="NSE", retrieved_at=NOW),
            FactSourceTier.OFFICIAL_NSE, "NSE", metric))
    candidate = (await scan([item], store)).candidates[0]
    assert candidate.eligible_for_deep_analysis
    assert candidate.dimensions["BALANCE_SHEET_QUALITY"].score == 100


@pytest.mark.asyncio
async def test_nonfinite_optional_values_stay_missing():
    store = SqliteResearchPersistence()
    item = instrument()
    persisted(store, item, {"profitMargin": 10, "revenueGrowth": "NaN", "earningsGrowth": "Infinity"})
    candidate = (await scan([item], store)).candidates[0]
    assert candidate.dimensions["GROWTH_QUALITY"].state == "MISSING"
    assert candidate.dimensions["GROWTH_QUALITY"].score is None


@pytest.mark.asyncio
async def test_different_financial_period_types_are_not_false_conflicts():
    store = SqliteResearchPersistence()
    item = instrument()
    persisted(store, item, {})
    for period_type, value in [("ANNUAL", 100), ("QUARTERLY", 25)]:
        store.upsert_financial_fact(FinancialFact(FinancialFactKey(UUID(int=1), "pat", "2026-03-31", period_type),
            ProvenancedValue(value=value, source_url="https://example.test", source_name="official", retrieved_at=NOW),
            FactSourceTier.OFFICIAL_REGULATORY, "OFFICIAL", period_type))
    candidate = (await scan([item], store)).candidates[0]
    assert candidate.eligible_for_deep_analysis
    assert candidate.dimensions["PROFITABILITY_QUALITY"].state == "PARTIAL"
