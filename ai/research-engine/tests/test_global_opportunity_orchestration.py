from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest

from app.global_opportunity_orchestration import GlobalOpportunityOrchestrator
from app.global_scanner import GlobalScanner
from app.persistence import SqliteResearchPersistence
from app.portfolio_orchestration import PortfolioResearchOrchestrator
from app.repository import ResearchRepository
from app.settings import Settings
from test_global_scanner import instrument, persisted, NOW
from test_global_opportunity_ranker import inputs


def setup(monkeypatch, count=3, *, stub_deep=True):
    store = SqliteResearchPersistence()
    repo = ResearchRepository(persistence=store)
    hydrator = PortfolioResearchOrchestrator(repo, Settings(), client=object())
    service = GlobalOpportunityOrchestrator(repo, store,
        profile_hydrator=hydrator.register_global_profile_metadata, clock=lambda:NOW)
    rows = [instrument(n) for n in range(1,count+1)]
    for row in rows: persisted(store, row)
    pairs = {UUID(int=n):inputs(n) for n in range(1,count+1)}
    monkeypatch.setattr(GlobalScanner, 'enrich_candidates', lambda self, scan, **kwargs:
        [pairs[c.global_instrument_id][0] for c in reversed(scan.candidates) if c.eligible_for_deep_analysis])
    if stub_deep:
        service.readiness.read = AsyncMock(return_value=object())
        async def analyze(profile, readiness, **kwargs):
            assert kwargs == dict(allow_partial=False, now=NOW)
            return pairs[profile.instrument_id][1]
        service.rule_engine.analyze = AsyncMock(side_effect=analyze)
    return service, rows, pairs, store


@pytest.mark.asyncio
async def test_shortlist_limit_order_and_v1_only_shortlist(monkeypatch):
    service, rows, pairs, _ = setup(monkeypatch, 4)
    pairs[UUID(int=3)][0].stage_b_score = 100
    result = await service.run(reversed(rows), as_of=NOW, shortlist_limit=2)
    assert [d.global_instrument_id.int for d in result.diagnostics] == [3,1]
    assert result.universe_count == result.phase1_eligible_count == result.stage_b_count == 4
    assert result.shortlist_count == result.deep_evaluated_count == 2
    assert service.rule_engine.analyze.await_count == 2
    # Final opportunity rank uses the ranker, not shortlist order.
    assert [entry.global_instrument_id.int for entry in result.top_n] == [1,3]


@pytest.mark.asyncio
async def test_sector_unavailable_allowed_and_fewer_than_top_n(monkeypatch):
    service, rows, pairs, _ = setup(monkeypatch, 1)
    stage = pairs[UUID(int=1)][0]
    stage.sector_score = stage.sector_relative_strength_snapshot.relative_strength_score = None
    stage.sector_relative_strength_snapshot.sector_state = 'INSUFFICIENT_DATA'
    result = await service.run(rows, as_of=NOW, top_n=10)
    assert result.rank_eligible_count == len(result.top_n) == 1
    assert result.top_n[0].sector_score is None
    assert result.top_n[0].score_coverage == 97


@pytest.mark.asyncio
async def test_failure_isolated_safe_and_suppressed_not_top_n(monkeypatch):
    service, rows, pairs, _ = setup(monkeypatch)
    pairs[UUID(int=2)][1].partial = True
    async def analyze(profile, readiness, **kwargs):
        if profile.instrument_id.int == 1: raise RuntimeError('secret-cookie/password')
        return pairs[profile.instrument_id][1]
    service.rule_engine.analyze.side_effect = analyze
    result = await service.run(rows, as_of=NOW)
    assert [d.status for d in result.diagnostics] == ['FAILED','SUPPRESSED','RANK_ELIGIBLE']
    assert result.diagnostics[0].failure_reason == 'RULE_ENGINE_UNAVAILABLE'
    assert result.deep_evaluated_count == 2 and result.rank_eligible_count == 1
    assert [e.global_instrument_id.int for e in result.top_n] == [3]
    assert 'secret' not in result.model_dump_json()


@pytest.mark.asyncio
async def test_repeat_membership_independence_uuid_ties_and_no_network(monkeypatch):
    service, rows, pairs, _ = setup(monkeypatch)
    def forbidden(*a, **k): pytest.fail('Provider/network/membership acquisition attempted')
    import socket
    import yfinance
    from app.nse_historical_daily import NseHistoricalDailyProvider
    from app.nse_index_history import NseIndexHistoryProvider
    from app.market_data_population import IndiaMarketDataPopulationJobs
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(httpx.AsyncClient, 'request', forbidden)
    monkeypatch.setattr(httpx.Client, 'request', forbidden)
    monkeypatch.setattr(yfinance, 'Ticker', forbidden)
    monkeypatch.setattr(NseHistoricalDailyProvider, 'fetch', forbidden)
    monkeypatch.setattr(NseIndexHistoryProvider, 'fetch', forbidden)
    monkeypatch.setattr(IndiaMarketDataPopulationJobs, 'backfill_daily_bars', forbidden)
    service.readiness.ensure = forbidden
    service.repository.holdings = forbidden
    service.repository.watchlist = forbidden
    first = await service.run(rows, as_of=NOW, top_n=2)
    service.clock = lambda: NOW+timedelta(seconds=1)
    second = await service.run([r | dict(portfolioId='irrelevant',watchlistMember=True) for r in reversed(rows)], as_of=NOW, top_n=2)
    assert first.model_dump(exclude={'generated_at'}) == second.model_dump(exclude={'generated_at'})
    assert [r.global_instrument_id.int for r in second.top_n] == [1,2]
    assert [r.rank for r in second.top_n] == [1,2]
    assert second.top_n[0].company_name == 'Company 1'
    assert 'decisionSignal' not in second.model_dump_json(by_alias=True)


@pytest.mark.asyncio
async def test_phase1_exclusion_not_deep_evaluated(monkeypatch):
    service, rows, pairs, store = setup(monkeypatch)
    persisted(store, rows[0], {})
    result = await service.run(rows, as_of=NOW)
    assert result.phase1_eligible_count == result.stage_b_count == 2
    assert all(d.global_instrument_id.int != 1 for d in result.diagnostics)


@pytest.mark.asyncio
async def test_empty_universe(monkeypatch):
    service, _, _, store = setup(monkeypatch, 0)
    queries = []
    store._connection.set_trace_callback(queries.append)
    result = await service.run([], as_of=NOW)
    assert not result.top_n and not result.diagnostics and not queries
    assert result.universe_count == result.stage_b_count == result.shortlist_count == 0
    assert service.rule_engine.analyze.await_count == 0


@pytest.mark.asyncio
async def test_real_readiness_v1_fingerprint_cache_without_refresh(monkeypatch):
    service, rows, _, store = setup(monkeypatch, 1, stub_deep=False)
    def forbidden(*a, **k): pytest.fail('Network or refresh attempted')
    monkeypatch.setattr(httpx.AsyncClient, 'request', forbidden)
    monkeypatch.setattr(httpx.Client, 'request', forbidden)
    import socket
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    service.readiness.ensure = forbidden
    first = await service.run(rows, as_of=NOW)
    second = await service.run(rows, as_of=NOW)
    third = await service.run([r | dict(portfolioId='irrelevant',watchlistMember=True) for r in rows], as_of=NOW)
    assert first.deep_evaluated_count == second.deep_evaluated_count == 1
    assert first.diagnostics[0].cache_hit is False and second.diagnostics[0].cache_hit is True
    assert first.top_n == second.top_n and second == third
    assert not first.top_n  # Sparse evidence cannot be promoted merely to fill Top-N.
    assert store._connection.execute('SELECT count(*) FROM global_stock_rule_engine_results').fetchone()[0] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('kwargs', [dict(shortlist_limit=0),dict(shortlist_limit=101),dict(top_n=-1),dict(top_n=True)])
async def test_invalid_limits(monkeypatch, kwargs):
    service, rows, _, _ = setup(monkeypatch)
    with pytest.raises(ValueError, match='INVALID_OPPORTUNITY_LIMIT'):
        await service.run(rows, as_of=NOW, **kwargs)


@pytest.mark.asyncio
async def test_shortlist_uses_screening_confidence_then_prescore(monkeypatch):
    service, rows, pairs, _ = setup(monkeypatch)
    pairs[UUID(int=2)][0].confidence = 100
    pairs[UUID(int=1)][0].stage_b_score = None
    result = await service.run(rows, as_of=NOW)
    assert [d.global_instrument_id.int for d in result.diagnostics] == [2,3,1]


@pytest.mark.asyncio
async def test_exact_top_n_uses_opportunity_order_not_screening_order(monkeypatch):
    service, rows, pairs, _ = setup(monkeypatch)
    for key, score in ((1,40),(2,90),(3,60)):
        for area in pairs[UUID(int=key)][1].area_scores:
            area.raw_score = score
    result = await service.run(rows, as_of=NOW, top_n=2)
    assert [d.global_instrument_id.int for d in result.diagnostics] == [1,2,3]
    assert [e.global_instrument_id.int for e in result.top_n] == [2,3]
    assert result.rank_eligible_count == 3
    assert result.top_n[0].opportunity_score > result.top_n[1].opportunity_score
