from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from uuid import UUID, uuid4
from unittest.mock import AsyncMock
import pytest

from app.persistence import SqliteResearchPersistence
from app.recommendation_engine import RecommendationEngineV1, lifecycle, ranges, RANGE_KEYS
from app.global_opportunity_cycle import snapshot_from_entry, prepare_cycle, nse_equities
from app.recommendation_backtesting import evaluate_backtest, evidence_available
from app.models import MarketPriceObservation
from test_global_scanner import NOW, instrument
from test_global_opportunity_orchestration import setup


def snapshot(n=1):
    return dict(snapshot_id=str(uuid4()), cycle_id=str(uuid4()), global_instrument_id=str(UUID(int=n)),
        generated_at=NOW.isoformat(), market='NSE', current_price=1000., opportunity_score=80.,
        opportunity_confidence=80., score_coverage=90., rule_engine_score=80., rank_eligible=True,
        rule_engine_version='STOCK_RULE_ENGINE_V1', ranker_version='GLOBAL_OPPORTUNITY_RANKER_V1',
        top_positive_reasons=['SUPPORT:QUALITY'], top_negative_reasons=[], symbol=f'C{n}',
        evidence_state={'technical': {'technical_state': 'UPTREND', 'support_level': 980., 'resistance_level': 1150., 'atr14': 50.},
            'rule': {'area_scores': [{'area': a, 'raw_score': 80} for a in ('FUNDAMENTAL_BUSINESS_QUALITY', 'VALUATION', 'GROWTH', 'BALANCE_SHEET')]}})


def publish(store, snapshots, top_n=4):
    cid = str(uuid4())
    for s in snapshots: s['cycle_id'] = cid
    history, states, selection = prepare_cycle(store, snapshots, cycle_id=cid, now=snapshots[0]['generated_at'], top_n=top_n)
    store.publish_opportunity_cycle(snapshots, history, states, selection)
    return selection


def test_actions_independent_and_missing_not_negative():
    s = snapshot()
    r = RecommendationEngineV1().evaluate(s)
    assert (r['short_term_action'], r['long_term_action']) == ('BUY', 'ACCUMULATE')
    s['evidence_state']['technical']['technical_state'] = 'OVEREXTENDED'
    r = RecommendationEngineV1().evaluate(s)
    assert (r['short_term_action'], r['long_term_action']) == ('WAIT', 'ACCUMULATE')
    s['opportunity_score'] = None
    r = RecommendationEngineV1().evaluate(s)
    assert (r['new_investor_action'], r['existing_holder_action']) == ('WATCH_WAIT', 'HOLD_NO_NEW_MONEY')
    s['evidence_state']['rule']['area_scores'] = []
    assert RecommendationEngineV1().evaluate(s)['long_term_action'] == 'HOLD'
    s['evidence_state']['rule']['area_scores'] = [{'area': a, 'raw_score': 0} for a in ('GROWTH', 'BALANCE_SHEET')]
    assert RecommendationEngineV1().evaluate(s)['long_term_action'] == 'EXIT_REVIEW'


def test_wait_recommendation_does_not_churn_history():
    store = SqliteResearchPersistence()
    s = snapshot()
    s['opportunity_score'] = None
    publish(store, [s])
    s = deepcopy(s)
    s.update(snapshot_id=str(uuid4()), generated_at=(NOW+timedelta(minutes=1)).isoformat())
    publish(store, [s])
    assert len(store.recommendation_history()) == 1


def test_ranges_and_determinism():
    s = snapshot()
    r = RecommendationEngineV1().evaluate(s)
    assert r == RecommendationEngineV1().evaluate(deepcopy(s))
    assert r['short_target_1'] == 1150 and r['short_target_2'] == 1200
    assert r['long_fair_value'] is None and 'PRICE_RANGE_EVIDENCE_INSUFFICIENT' in r['top_negative_reasons']
    s['evidence_state']['technical'] = {}
    assert all(v is None for v in ranges(s).values())
    s['evidence_state']['valuation'] = {'fair_value': 1500., 'bull_target': 1800., 'invalidation': 900.}
    assert ranges(s)['long_entry_low'] == 1200


@pytest.mark.parametrize('price,state,action', [(1120, 'TARGET_APPROACHING', 'BUY'),
    (1150, 'PARTIAL_PROFIT', 'PARTIAL_PROFIT'), (1185, 'PARTIAL_PROFIT', 'PARTIAL_PROFIT'),
    (930, 'INVALIDATED', 'EXIT'), (950, 'INVALIDATION_APPROACHING', 'BUY'),
    (990, 'IN_ENTRY_ZONE', 'BUY'), (1010, 'ENTRY_APPROACHING', 'BUY')])
def test_lifecycle(price, state, action):
    r = RecommendationEngineV1().evaluate(snapshot())
    r['long_term_action'] = 'HOLD'
    actual = lifecycle(r, r, price, {'short_term_state': 'NEW'})
    assert actual['short_term_state'] == state and actual['current_short_action'] == action
    assert actual['current_long_action'] == 'HOLD'
    if price == 1185:
        assert 'TARGET_2_APPROACHING' in actual['lifecycle_reasons']


def test_thesis_break_exit_review_overrides_targets():
    s = snapshot()
    prior = RecommendationEngineV1().evaluate(s)
    s['evidence_state']['rule']['risk_overrides'] = [{'severity': 'CRITICAL', 'code': 'VALIDATED_GOVERNANCE_RISK'}]
    now = RecommendationEngineV1().evaluate(s)
    state = lifecycle(prior, now, 1185, {})
    assert state['current_long_action'] == 'EXIT_REVIEW' and state['current_short_action'] == 'EXIT'


def test_dedupe_history_immutable_state_updates_and_restart(tmp_path):
    path = tmp_path/'recommendations.db'
    store = SqliteResearchPersistence(path)
    s = snapshot()
    publish(store, [s])
    original = store.recommendation_history()
    again = deepcopy(s)
    again.update(snapshot_id=str(uuid4()), generated_at=(NOW+timedelta(hours=1)).isoformat())
    publish(store, [again])
    assert store.recommendation_history() == original
    assert store.recommendation_states()[0]['updated_at'] == again['generated_at']
    moved = deepcopy(again)
    moved.update(snapshot_id=str(uuid4()), current_price=1185., generated_at=(NOW+timedelta(hours=2)).isoformat())
    publish(store, [moved])
    assert store.recommendation_states()[0]['current_short_action'] == 'PARTIAL_PROFIT'
    assert store.recommendation_history()[0] == original[0]
    assert SqliteResearchPersistence(path).opportunity_current()['previous_recommendations'][0]['current_short_action'] == 'PARTIAL_PROFIT'


def test_database_rejects_history_mutation_and_failed_publish_rolls_back():
    store = SqliteResearchPersistence()
    s = snapshot()
    selection = publish(store, [s])
    for table in ('stock_recommendation_history', 'global_opportunity_snapshot'):
        with pytest.raises(Exception, match='IMMUTABLE'):
            with store._connection:
                store._connection.execute(f'UPDATE {table} SET payload = payload')
        with pytest.raises(Exception, match='IMMUTABLE'):
            with store._connection:
                store._connection.execute(f'DELETE FROM {table}')
    failed = snapshot(2)
    def build(persisted):
        assert persisted[0]['global_instrument_id'] == failed['global_instrument_id']
        raise ValueError('SIMULATED_FAILURE')
    with pytest.raises(ValueError, match='SIMULATED_FAILURE'):
        store.publish_opportunity_cycle([failed], [], [], {'cycle_id': failed['cycle_id']}, build=build)
    assert store.opportunity_snapshots(failed['cycle_id']) == []
    assert store.opportunity_current() == selection


def test_global_top_order_membership_independence_and_suppression():
    rows = [snapshot(i) for i in range(1, 7)]
    rows[-1]['rank_eligible'] = False
    first = publish(SqliteResearchPersistence(), rows, 4)
    second = publish(SqliteResearchPersistence(), [dict(r, held=True, watchlisted=True) for r in reversed(rows)], 4)
    keys = lambda r: [c['global_instrument_id'] for c in r['top_short_term']]
    assert keys(first) == keys(second) == [str(UUID(int=n)) for n in range(1, 5)]
    assert len(first['top_long_term']) == 4
    assert len(publish(SqliteResearchPersistence(), [snapshot(i) for i in range(1, 6)], 2)['top_short_term']) == 2
    assert nse_equities([instrument(), instrument(2) | {'exchange': 'NYSE'}, instrument(3) | {'assetType': 'ETF'}]) == [instrument()]


@pytest.mark.asyncio
async def test_real_scanner_ranker_snapshot_persisted(monkeypatch):
    service, rows, pairs, store = setup(monkeypatch, 3)
    ranking = await service.run(rows, as_of=NOW)
    snapshots = [snapshot_from_entry(e, str(uuid4()), NOW.isoformat()) for e in ranking.evaluated_entries]
    selection = publish(store, snapshots)
    actual = store.opportunity_snapshots(selection['cycle_id'])
    assert len(actual) == 3
    assert actual[0]['opportunity_score'] == ranking.top_n[0].opportunity_score
    assert actual[0]['evidence_state']['rule']['rule_engine_version'] == 'STOCK_RULE_ENGINE_V1'


@pytest.mark.asyncio
async def test_explicit_cycle_uses_canonical_rows_and_reads_back_snapshots(monkeypatch):
    from app import global_opportunity_cycle as cycle
    service, rows, pairs, store = setup(monkeypatch, 3)
    class Clock:
        @staticmethod
        def now(*a): return NOW
    monkeypatch.setattr(cycle, 'datetime', Clock)
    monkeypatch.setattr(cycle, 'GlobalOpportunityOrchestrator', lambda *a, **k: service)
    source = AsyncMock()
    source.active_global_equities.return_value = rows
    source.sector_benchmark_contexts.return_value = {}
    original = RecommendationEngineV1.evaluate
    def evaluate(self, s, previous=None):
        assert any(r['snapshot_id'] == s['snapshot_id'] for r in store.opportunity_snapshots(s['cycle_id']))
        return original(self, s, previous)
    monkeypatch.setattr(RecommendationEngineV1, 'evaluate', evaluate)
    result = await cycle.run_global_opportunity_cycle(service.repository, source, candidate_ids=[UUID(int=1)], top_n=2)
    assert result['universe_count'] == 1 and result['controlled_candidate_set']
    assert len(store.recommendation_history()) == 1
    source.active_global_equities.assert_awaited_once()


@pytest.mark.asyncio
async def test_bounded_previous_review_does_not_change_scanner_shortlist(monkeypatch):
    service, rows, pairs, store = setup(monkeypatch, 3)
    result = await service.run(rows, as_of=NOW, shortlist_limit=1, review_ids=[UUID(int=3)])
    assert result.shortlist_count == 1 and result.deep_evaluated_count == 2
    assert {e.global_instrument_id for e in result.evaluated_entries} == {UUID(int=1), UUID(int=3)}


def observation(day, price, retrieved=None):
    return MarketPriceObservation(instrument_id=UUID(int=1), observed_at=NOW+timedelta(days=day),
        retrieved_at=NOW+timedelta(days=retrieved if retrieved is not None else day),
        price=Decimal(str(price)), currency='INR', provider='YAHOO_FINANCE', source_url='https://example.test')


def test_backtest_point_in_time_returns_and_missing_future():
    r = RecommendationEngineV1().evaluate(snapshot()) | {'recommendation_id': str(uuid4())}
    prices = {UUID(int=1): [observation(0, 100), observation(7, 110), observation(30, 90), observation(3, 80)]}
    result = evaluate_backtest([r], prices, start=NOW, end=NOW, horizon='SHORT_TERM', now=NOW+timedelta(days=40))
    assert result['metrics']['1W']['average_return'] == pytest.approx(10)
    assert result['metrics']['1M']['average_return'] == pytest.approx(-10)
    assert result['metrics']['1M']['max_adverse_excursion'] == pytest.approx(-20)
    assert result['metrics']['1Y']['average_return'] is None
    assert result['samples']['1Y'][0]['status'] == 'FUTURE_PRICE_UNAVAILABLE'
    prices[UUID(int=1)][0] = observation(0, 100, retrieved=1)
    assert evaluate_backtest([r], prices, start=NOW, end=NOW, horizon='SHORT_TERM', now=NOW+timedelta(days=40))['samples']['1W'][0]['status'] == 'ENTRY_PRICE_UNAVAILABLE'


@pytest.mark.parametrize('key', ['publishedAt', 'publicAvailabilityAt', 'discoveredAt', 'computedAt', 'retrieved_at', 'calculated_at'])
def test_temporal_guards(key):
    assert not evidence_available({'nested': [{key: (NOW+timedelta(days=1)).isoformat()}]}, NOW)
    assert evidence_available({'nested': [{key: NOW.isoformat()}]}, NOW)


def test_backtest_excludes_future_evidence_and_future_recommendation():
    r = RecommendationEngineV1().evaluate(snapshot()) | {'recommendation_id': str(uuid4())}
    r['evidence_snapshot']['computedAt'] = (NOW+timedelta(days=1)).isoformat()
    result = evaluate_backtest([r], {}, start=NOW, end=NOW, horizon='SHORT_TERM', now=NOW+timedelta(days=40))
    assert result['recommendation_count'] == 0 and result['excluded_unavailable_evidence'] == 1
    r['generated_at'] = (NOW+timedelta(days=1)).isoformat()
    assert evaluate_backtest([r], {}, start=NOW, end=NOW, horizon='SHORT_TERM', now=NOW+timedelta(days=40))['recommendation_count'] == 0


def test_backtest_daily_closes_are_persisted_outcome_evidence(monkeypatch):
    from app import recommendation_backtesting as backtesting
    from app.models import DailyMarketBar
    from datetime import datetime
    store = SqliteResearchPersistence()
    publish(store, [snapshot()])
    for day, close in [(-1, 100), (7, 110)]:
        store.upsert_daily_market_bar(DailyMarketBar(global_instrument_id=UUID(int=1),
            trading_date=(NOW+timedelta(days=day)).date(), close=Decimal(close), currency='INR',
            provider='NSE', source_mode='REAL', source_url='https://example.test/bars',
            retrieved_at=NOW+timedelta(days=day, hours=12)))
    class Clock(datetime):
        @classmethod
        def now(cls, *a): return NOW+timedelta(days=40)
    monkeypatch.setattr(backtesting, 'datetime', Clock)
    result = backtesting.run_backtest(store, start=NOW, end=NOW, horizon='SHORT_TERM')
    assert result['metrics']['1W']['average_return'] == pytest.approx(10)
    assert store.backtests()[0] == result


@pytest.mark.asyncio
async def test_dashboard_read_has_no_compute_or_providers(monkeypatch):
    from app import main
    store = SqliteResearchPersistence()
    publish(store, [snapshot()])
    monkeypatch.setattr(main.repository, '_persistence', store)
    def forbidden(*a, **kw): raise AssertionError('Provider or computation in GET')
    monkeypatch.setattr(RecommendationEngineV1, 'evaluate', forbidden)
    monkeypatch.setattr(main.portfolio_orchestrator, 'active_global_equities', forbidden)
    monkeypatch.setattr(main.stock_rule_engine_service, 'analyze', forbidden)
    import httpx
    monkeypatch.setattr(httpx.AsyncClient, 'request', forbidden)
    assert len((await main.opportunity_radar())['top_short_term']) == 1
    assert len(await main.opportunity_history(UUID(int=1))) == 1
    assert await main.backtest_runs() == []
