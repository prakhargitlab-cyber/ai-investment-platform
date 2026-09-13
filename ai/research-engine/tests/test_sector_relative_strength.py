from datetime import timedelta
from uuid import UUID

import pytest

from app.sector_relative_strength import (
    BenchmarkReference, SectorContext, SectorRelativeStrengthConfig, SectorRelativeStrengthEngine,
)
from test_technical_features import NOW, STOCK, history


SECTOR, MARKET = UUID(int=2), UUID(int=3)


def context(currency="INR", region="INDIA"):
    return SectorContext("Financial Services", "CANONICAL_UNIVERSE", NOW-timedelta(days=1), region,
                         BenchmarkReference(SECTOR, currency), BenchmarkReference(MARKET, currency))


def rate_series(rate, count=160):
    return [100*(1+rate)**i for i in range(count)]


def evaluate(stock, sector=None, market=None, mapping=None, currency="INR"):
    histories = {}
    if sector is not None: histories[SECTOR] = history(sector, SECTOR, currency)
    if market is not None: histories[MARKET] = history(market, MARKET, currency)
    return SectorRelativeStrengthEngine().compute(STOCK, history(stock, currency=currency), as_of=NOW,
        currency=currency, context=mapping or context(currency), benchmark_histories=histories)


@pytest.mark.parametrize("rates,state", [((.002, .001, .0005), "LEADING"), ((-.002, 0, .001), "LAGGING"), ((0, 0, 0), "NEUTRAL")])
def test_outperforming_lagging_and_flat_relative_series(rates, state):
    result = evaluate(*(rate_series(rate) for rate in rates))
    assert result.sector_state == state
    assert result.confidence == 100
    assert result.sector == "Financials"
    assert result.relative_vs_sector1_m == pytest.approx(result.stock_return1_m-result.sector_return1_m)
    assert result.relative_vs_market3_m == pytest.approx(result.stock_return3_m-result.market_return3_m)
    assert (result.relative_strength_score > 50 if state == "LEADING" else
            result.relative_strength_score < 50 if state == "LAGGING" else result.relative_strength_score == 50)


def test_beating_sector_but_lagging_market_keeps_separate_legs():
    result = evaluate(rate_series(.001), rate_series(0), rate_series(.002))
    assert result.relative_vs_sector1_m > 0
    assert result.relative_vs_market1_m < 0
    assert result.sector_return1_m != result.market_return1_m


@pytest.mark.parametrize("anchors,state", [((99, 98, 105, 110), "IMPROVING"), ((101, 102, 95, 90), "WEAKENING")])
def test_relative_trend_changes_use_horizon_normalization(anchors, state):
    stock = [100]*160
    for offset, value in zip((5, 21, 63, 126), anchors): stock[-offset-1] = value
    result = evaluate(stock, [100]*160, [100]*160)
    assert result.sector_state == state


def test_missing_sector_history_does_not_substitute_market():
    result = evaluate(rate_series(.001), market=rate_series(.0005))
    assert result.sector_return1_m is result.relative_vs_sector1_m is None
    assert "SECTOR_HISTORY" in result.missing_inputs
    assert result.market_return1_m is not None
    assert result.sector_state == "INSUFFICIENT_DATA"
    assert result.confidence == 50


def test_missing_market_mapping_and_no_history_do_not_become_zero():
    mapping = SectorContext("Industrials", "CANONICAL", NOW, "INDIA", BenchmarkReference(SECTOR, "INR"))
    result = evaluate(rate_series(.001), sector=rate_series(.0005), mapping=mapping)
    assert result.market_return1_m is result.relative_vs_market1_m is None
    assert "MARKET_BENCHMARK_MAPPING" in result.missing_inputs
    missing = evaluate(rate_series(.001), mapping=SectorContext())
    assert missing.relative_strength_score is None and missing.confidence == 0
    assert missing.stock_return1_m is not None


@pytest.mark.parametrize("count", [0, 5, 6, 21])
def test_insufficient_lookback_cannot_claim_multi_period_consistency(count):
    result = evaluate([100]*count, [100]*count, [100]*count)
    assert result.sector_state == "INSUFFICIENT_DATA"
    assert result.relative_strength_score is None
    assert result.stock_return1_m is None


@pytest.mark.parametrize("currency,region", [("INR", "INDIA"), ("USD", "USA"), ("EUR", "EUROPE")])
def test_region_neutral_fixture(currency, region):
    result = evaluate(rate_series(.002), rate_series(.001), rate_series(.0005), mapping=context(currency, region), currency=currency)
    assert result.sector_state == "LEADING" and result.region == region
    assert result.market_benchmark_id == MARKET and result.sector_benchmark_id == SECTOR


def test_exact_date_alignment_prevents_mismatched_period_comparison():
    stock = history(rate_series(.002))
    sector = history(rate_series(.001), SECTOR)
    sector.pop(-22)  # Remove precisely the 1M stock reference date, not the latest.
    result = SectorRelativeStrengthEngine().compute(STOCK, stock, as_of=NOW, context=context(),
        benchmark_histories={SECTOR: sector, MARKET: history(rate_series(.001), MARKET)})
    assert result.relative_vs_sector1_m is None
    assert "SECTOR_ALIGNED_DATES_1M" in result.missing_inputs
    assert result.relative_vs_sector3_m is not None
    assert result.comparison_windows["1M"] == (stock[-22].observed_at.date(), stock[-1].observed_at.date())


def test_stale_and_conflicting_benchmarks_are_explicit():
    stock = history(rate_series(.002))
    sector = history(rate_series(.001), SECTOR, end=NOW-timedelta(days=20))
    result = SectorRelativeStrengthEngine().compute(STOCK, stock, as_of=NOW, context=context(), benchmark_histories={SECTOR: sector})
    assert result.relative_vs_sector1_m is None
    assert result.stale_inputs == ["SECTOR_HISTORY"]
    sector = history(rate_series(.001), SECTOR)
    sector.append(sector[-1].model_copy(update={"price": 999, "provider": "OTHER"}))
    result = SectorRelativeStrengthEngine().compute(STOCK, stock, as_of=NOW, context=context(), benchmark_histories={SECTOR: sector})
    assert "CONFLICTING_SECTOR_PRICE" in result.missing_inputs
    assert result.relative_strength_score is None


def test_determinism_no_network_and_future_classification_rejected(monkeypatch):
    import socket
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("Network invoked"))
    engine = SectorRelativeStrengthEngine()
    stock, sector, market = history(rate_series(.002)), history(rate_series(.001), SECTOR), history(rate_series(.0005), MARKET)
    first = engine.compute(STOCK, stock, as_of=NOW, context=context(), benchmark_histories={SECTOR: sector, MARKET: market})
    second = engine.compute(STOCK, reversed(stock), as_of=NOW, context=context(), benchmark_histories={MARKET: list(reversed(market)), SECTOR: list(reversed(sector))})
    assert first == second
    future = SectorContext("Financials", "CANONICAL", NOW+timedelta(days=1), "INDIA", BenchmarkReference(SECTOR, "INR"))
    result = engine.compute(STOCK, stock, as_of=NOW, context=future, benchmark_histories={SECTOR: sector})
    assert result.sector is None and result.relative_vs_sector1_m is None


def test_configuration_and_self_benchmark_rejected():
    with pytest.raises(ValueError): SectorRelativeStrengthConfig(period_weights=(0, 0, 0, 0))
    with pytest.raises(ValueError): SectorRelativeStrengthConfig(full_scale_edge_per_observation_pct=0)
    result = evaluate(rate_series(.001), mapping=SectorContext("Financials", "CANONICAL", NOW, "INDIA", BenchmarkReference(STOCK, "INR")))
    assert "INVALID_SECTOR_BENCHMARK_MAPPING" in result.missing_inputs


@pytest.mark.asyncio
async def test_stage_b_batches_only_deep_eligible_and_preserves_phase1(monkeypatch):
    from app.global_scanner import GlobalScanner
    from app.persistence import SqliteResearchPersistence
    from app.stock_rule_engine import StockRuleEngineService
    from test_global_scanner import instrument, persisted, scan
    def forbidden(*a, **k): pytest.fail("Provider/V1/universe invoked by Stage B")
    monkeypatch.setattr(StockRuleEngineService, "analyze", forbidden)
    store = SqliteResearchPersistence()
    items = [instrument(n) for n in (1, 4, 5)]
    for item in items: persisted(store, item)
    # Candidate5 lacks critical financial evidence, despite a complete price history.
    persisted(store, items[2], {})
    for key in (STOCK, UUID(int=4), UUID(int=5), SECTOR, MARKET):
        for row in history(rate_series(.001), key):
            store.upsert_market_price_observation(row.model_copy(update={"provider": "YAHOO_FINANCE"}))
    initial = await scan(items, store)
    before = initial.model_dump()
    queries = []
    store._connection.set_trace_callback(queries.append)
    class NoUniverse:
        active_global_equities = forbidden
    scanner = GlobalScanner(NoUniverse(), store)
    enriched = scanner.enrich_candidates(initial, sector_contexts={STOCK: context()})
    assert {c.global_instrument_id for c in enriched} == {STOCK, UUID(int=4)}
    assert initial.model_dump() == before
    assert len(queries) == 2
    daily_query = next(q for q in queries if "global_daily_market_bars" in q)
    close_query = next(q for q in queries if "global_market_price_observations" in q)
    assert "instrument_id IN" in close_query
    assert all(str(UUID(int=5)) not in q for q in queries)
    assert str(SECTOR) in close_query and str(MARKET) in close_query
    assert str(SECTOR) not in daily_query and str(MARKET) not in daily_query
    assert all(c.technical_feature_snapshot.global_instrument_id == c.global_instrument_id for c in enriched)
    # Private inputs are not part of the enrichment contract.
    assert enriched == scanner.enrich_candidates(initial, sector_contexts={STOCK: context()})


@pytest.mark.asyncio
async def test_stage_b_missing_sector_renormalizes_and_order_is_deterministic():
    from app.global_scanner import GlobalScanner
    from app.persistence import SqliteResearchPersistence
    from test_global_scanner import instrument, persisted, scan
    store = SqliteResearchPersistence()
    items = [instrument(n) for n in (1, 4)]
    for item in items:
        persisted(store, item)
        for row in history(rate_series(.001), UUID(item["globalInstrumentId"])):
            store.upsert_market_price_observation(row.model_copy(update={"provider": "YAHOO_FINANCE"}))
    result = await scan(items, store)
    enriched = GlobalScanner(None, store).enrich_candidates(result)
    assert [c.global_instrument_id.int for c in enriched] == [1, 4]
    for candidate in enriched:
        assert candidate.sector_score is None
        assert candidate.stage_b_score == candidate.technical_score
        assert candidate.score_coverage == 70
        assert candidate.confidence == pytest.approx(candidate.technical_feature_snapshot.confidence*.7)
