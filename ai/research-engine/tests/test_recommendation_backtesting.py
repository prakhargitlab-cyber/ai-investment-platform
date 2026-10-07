"""Focused correctness tests for app.recommendation_backtesting.

Exercises evaluate_backtest() directly against synthetic history/price
fixtures (no database/API layer involved) to prove the six-way status
reconciliation required of the backtesting correctness fix:
EVALUATED / NOT_MATURED / MISSING_MARKET_DATA / OUTSIDE_EVALUATION_WINDOW /
INSUFFICIENT_EVIDENCE / BACKEND_FAILURE.
"""
from uuid import uuid4

import pytest

from app import recommendation_backtesting as backtesting
from app.models import MarketPriceObservation

INSTRUMENT_A = uuid4()
INSTRUMENT_B = uuid4()
INSTRUMENT_C = uuid4()
INSTRUMENT_D = uuid4()


def _rec(instrument_id, generated_at, *, short_action='BUY', long_action='HOLD',
         market='NSE', evidence=None, rec_id=None):
    return dict(recommendation_id=str(rec_id or uuid4()), global_instrument_id=str(instrument_id),
        generated_at=generated_at, symbol='TST', company_name='Test Co', market=market,
        short_term_action=short_action, long_term_action=long_action,
        evidence_snapshot=evidence if evidence is not None else {})


def _original_rec(instrument_id, generated_at, *, horizon='SHORT_TERM', action='BUY',
                  market='NSE', evidence=None, entry_price=100.0, rec_id=None):
    """Shape returned from the immutable V14 Radar suggestion table."""
    recommendation_id = str(rec_id or uuid4())
    return dict(recommendation_id=recommendation_id, suggestion_id=recommendation_id,
        global_instrument_id=str(instrument_id), generated_at=generated_at,
        symbol='TST', company_name='Test Co', market=market, horizon=horizon,
        recommendation_type=horizon, action=action, initial_action=action,
        price_at_recommendation=entry_price,
        evidence_snapshot=evidence if evidence is not None else {})


def _price(instrument_id, observed_at, price, *, retrieved_at=None):
    return MarketPriceObservation(instrument_id=instrument_id, observed_at=observed_at,
        price=price, currency='INR', provider='TEST', source_url='https://test.invalid',
        retrieved_at=retrieved_at or observed_at)


def _run(history, prices, *, now='2024-02-10T00:00:00Z', start='2024-01-01T00:00:00Z',
          end='2024-01-02T00:00:00Z', horizon='SHORT_TERM', benchmark_prices=None):
    return backtesting.evaluate_backtest(history, prices, start=start, end=end, horizon=horizon,
        now=now, benchmark_prices=benchmark_prices)


def test_matured_with_data_is_evaluated():
    history = [_rec(INSTRUMENT_A, '2024-01-01T00:00:00Z')]
    prices = {INSTRUMENT_A: [
        _price(INSTRUMENT_A, '2023-12-31T00:00:00Z', 100),
        _price(INSTRUMENT_A, '2024-01-09T00:00:00Z', 110),   # 1W target=Jan8, window Jan8-15
        _price(INSTRUMENT_A, '2024-02-01T00:00:00Z', 120),   # 1M target=Jan31, window Jan31-Feb7
    ]}
    result = _run(history, prices)
    assert result['samples']['1W'][0]['status'] == 'EVALUATED'
    assert result['samples']['1W'][0]['return_pct'] == pytest.approx(10.0)
    assert result['samples']['1M'][0]['status'] == 'EVALUATED'
    assert result['samples']['1M'][0]['return_pct'] == pytest.approx(20.0)


def test_future_required_date_is_not_matured():
    history = [_rec(INSTRUMENT_A, '2024-01-01T00:00:00Z')]
    prices = {INSTRUMENT_A: [_price(INSTRUMENT_A, '2023-12-31T00:00:00Z', 100)]}
    # now = 2024-02-10: 1W/1M targets have passed, but the remaining
    # SHORT_TERM targets are still in the future. A short-term run has no 1Y.
    result = _run(history, prices)
    for h in ('3M', '6M'):
        sample = result['samples'][h][0]
        assert sample['status'] == 'NOT_MATURED', h
        assert sample['return_pct'] is None
        assert sample['required_evaluation_date'] is not None
    # Never conflated with a market-data gap.
    assert result['metrics']['3M']['missing_market_data_count'] == 0
    assert result['metrics']['3M']['not_matured_count'] == 1
    assert '1Y' not in result['samples']


def test_matured_but_missing_price_data_is_missing_market_data():
    history = [_rec(INSTRUMENT_B, '2024-01-01T00:00:00Z')]
    prices = {INSTRUMENT_B: []}   # no price history at all
    result = _run(history, prices)
    sample = result['samples']['1W'][0]
    assert sample['status'] == 'MISSING_MARKET_DATA'
    assert sample['return_pct'] is None
    assert result['metrics']['1W']['not_matured_count'] == 0


def test_genuine_insufficient_evidence_stays_insufficient_evidence():
    # computed_at AFTER the recommendation's own generation time -> evidence
    # was not yet available when the recommendation was made.
    evidence = {'rule': {'computed_at': '2024-01-02T00:00:00Z'}}
    history = [_rec(INSTRUMENT_C, '2024-01-01T00:00:00Z', evidence=evidence)]
    result = _run(history, {INSTRUMENT_C: []})
    for h in ('1W', '1M'):
        assert result['samples'][h][0]['status'] == 'INSUFFICIENT_EVIDENCE'
    # Maturity takes precedence: no future horizon is mislabeled as an
    # evidence failure before its evaluation date exists.
    for h in ('3M', '6M'):
        assert result['samples'][h][0]['status'] == 'NOT_MATURED'
    assert result['metrics']['1W']['insufficient_evidence_count'] == 1
    assert result['metrics']['1W']['missing_market_data_count'] == 0


def test_unexpected_backend_failure_is_distinguishable(monkeypatch):
    def _boom(instrument_id, observations, *, as_of, currency=None, trusted_providers=None):
        raise RuntimeError('synthetic failure')
    monkeypatch.setattr(backtesting, 'normalize_price_history', _boom)
    history = [_rec(INSTRUMENT_D, '2024-01-01T00:00:00Z')]
    result = _run(history, {INSTRUMENT_D: [_price(INSTRUMENT_D, '2023-12-31T00:00:00Z', 100)]})
    for h in ('1W', '1M'):
        sample = result['samples'][h][0]
        assert sample['status'] == 'BACKEND_FAILURE', h
    for h in ('3M', '6M'):
        assert result['samples'][h][0]['status'] == 'NOT_MATURED'
    # Never disguised as insufficient evidence or missing data.
    assert result['metrics']['1W']['insufficient_evidence_count'] == 0
    assert result['metrics']['1W']['missing_market_data_count'] == 0
    assert result['metrics']['1W']['backend_failure_count'] == 1


def test_summary_counts_reconcile_exactly_with_rows():
    history = [
        _rec(INSTRUMENT_A, '2024-01-01T00:00:00Z'),                                  # -> cohort
        _rec(INSTRUMENT_B, '2024-01-01T00:00:00Z', short_action='HOLD'),             # -> outside window (action)
        _rec(INSTRUMENT_C, '2024-01-01T00:00:00Z', market='BSE'),                    # -> outside window (market)
        _rec(INSTRUMENT_D, '2024-01-01T00:00:00Z', evidence={'rule': {'computed_at': '2024-01-02T00:00:00Z'}}),  # -> insufficient evidence
    ]
    prices = {INSTRUMENT_A: [_price(INSTRUMENT_A, '2023-12-31T00:00:00Z', 100),
                              _price(INSTRUMENT_A, '2024-01-09T00:00:00Z', 110)],
              INSTRUMENT_B: [], INSTRUMENT_C: [], INSTRUMENT_D: []}
    result = _run(history, prices)
    assert result['recommendation_count'] == len(history)
    for h in backtesting.SHORT_TERM_HORIZONS:
        m = result['metrics'][h]
        total = (m['evaluated_count'] + m['not_matured_count'] + m['missing_market_data_count']
                 + m['outside_evaluation_window_count'] + m['insufficient_evidence_count'] + m['backend_failure_count'])
        assert total == len(history) == m['recommendation_count'] == len(result['samples'][h])
    assert result['metrics']['1W']['outside_evaluation_window_count'] == 2
    assert result['metrics']['1W']['insufficient_evidence_count'] == 1


def test_aggregate_performance_uses_only_evaluated_rows():
    history = [_rec(INSTRUMENT_A, '2024-01-01T00:00:00Z'), _rec(INSTRUMENT_B, '2024-01-01T00:00:00Z')]
    prices = {
        INSTRUMENT_A: [_price(INSTRUMENT_A, '2023-12-31T00:00:00Z', 100),
                       _price(INSTRUMENT_A, '2024-01-09T00:00:00Z', 110)],
        INSTRUMENT_B: [],   # genuinely missing -- must not pollute the average
    }
    result = _run(history, prices)
    m = result['metrics']['1W']
    assert m['evaluated_count'] == 1
    assert m['missing_market_data_count'] == 1
    assert m['average_return'] == pytest.approx(10.0)
    assert m['hit_rate'] == pytest.approx(100.0)


def test_zero_evaluated_rows_do_not_produce_misleading_zero_percent():
    history = [_rec(INSTRUMENT_B, '2024-01-01T00:00:00Z')]
    result = _run(history, {INSTRUMENT_B: []})
    m = result['metrics']['1W']
    assert m['evaluated_count'] == 0
    assert m['average_return'] is None
    assert m['median_return'] is None
    assert m['hit_rate'] is None
    assert m['best_return'] is None
    assert m['worst_return'] is None


def test_existing_outside_window_and_invalid_parameters_behavior_unchanged():
    history = [_rec(INSTRUMENT_A, '2024-01-01T00:00:00Z', short_action='HOLD')]
    result = _run(history, {INSTRUMENT_A: []})
    assert result['samples']['1W'][0]['status'] == 'OUTSIDE_EVALUATION_WINDOW'
    assert result['metrics']['1W']['evaluated_count'] == 0

    with pytest.raises(ValueError):
        _run(history, {INSTRUMENT_A: []}, start='2024-02-01T00:00:00Z', end='2024-01-01T00:00:00Z')
    with pytest.raises(ValueError):
        _run(history, {INSTRUMENT_A: []}, end='2099-01-01T00:00:00Z')
    with pytest.raises(ValueError):
        _run(history, {INSTRUMENT_A: []}, horizon='NOT_A_HORIZON')


def test_as_of_pins_evaluation_clock_deterministically():
    # Without as_of, run_backtest's own default is "real now" -- here we just
    # confirm evaluate_backtest's `now` parameter (which as_of feeds) is what
    # actually drives maturity, by picking an earlier as_of than the fixture
    # above and seeing 1M flip from EVALUATED to NOT_MATURED.
    history = [_rec(INSTRUMENT_A, '2024-01-01T00:00:00Z')]
    prices = {INSTRUMENT_A: [_price(INSTRUMENT_A, '2023-12-31T00:00:00Z', 100),
                              _price(INSTRUMENT_A, '2024-01-09T00:00:00Z', 110)]}
    early = _run(history, prices, now='2024-01-05T00:00:00Z')
    assert early['samples']['1W'][0]['status'] == 'NOT_MATURED'
    late = _run(history, prices, now='2024-02-10T00:00:00Z')
    assert late['samples']['1W'][0]['status'] == 'EVALUATED'


def test_short_term_exposes_only_7_30_91_182_day_horizons():
    result = _run([_original_rec(INSTRUMENT_A, '2024-01-01T00:00:00Z')],
                  {INSTRUMENT_A: []})
    assert result['available_horizons'] == ['1W', '1M', '3M', '6M']
    assert set(result['samples']) == {'1W', '1M', '3M', '6M'}
    assert '1Y' not in result['metrics']


def test_long_term_starts_at_365_days_and_exposes_no_short_term_buckets():
    result = _run(
        [_original_rec(INSTRUMENT_A, '2024-01-01T00:00:00Z', horizon='LONG_TERM')],
        {INSTRUMENT_A: []}, horizon='LONG_TERM')
    assert result['available_horizons'] == ['1Y', '2Y', '3Y']
    assert set(result['samples']) == {'1Y', '2Y', '3Y'}
    assert not set(result['samples']).intersection({'1W', '1M', '3M', '6M'})


def test_long_term_younger_than_365_days_is_not_matured():
    recommendation = _original_rec(
        INSTRUMENT_A, '2024-01-01T00:00:00Z', horizon='LONG_TERM',
        evidence={'rule': {'computed_at': '2024-01-02T00:00:00Z'}})
    result = _run([recommendation], {INSTRUMENT_A: []}, horizon='LONG_TERM',
                  now='2024-12-30T00:00:00Z')
    sample = result['samples']['1Y'][0]
    assert sample['status'] == 'NOT_MATURED'
    assert result['metrics']['1Y']['not_matured_count'] == 1
    assert result['metrics']['1Y']['evaluated_count'] == 0
    assert result['metrics']['1Y']['success_rate'] is None


def test_long_term_at_365_days_can_be_evaluated():
    recommendation = _original_rec(
        INSTRUMENT_A, '2024-01-01T00:00:00Z', horizon='LONG_TERM', entry_price=100)
    prices = {INSTRUMENT_A: [
        _price(INSTRUMENT_A, '2024-12-31T00:00:00Z', 125),
    ]}
    result = _run([recommendation], prices, horizon='LONG_TERM',
                  now='2024-12-31T23:59:59Z')
    sample = result['samples']['1Y'][0]
    assert sample['status'] == 'EVALUATED'
    assert sample['return_pct'] == pytest.approx(25.0)
    assert sample['signal_return_pct'] == pytest.approx(25.0)


def test_two_and_three_year_long_term_horizons_evaluate_when_data_exists():
    recommendation = _original_rec(
        INSTRUMENT_A, '2024-01-01T00:00:00Z', horizon='LONG_TERM', entry_price=100)
    prices = {INSTRUMENT_A: [
        _price(INSTRUMENT_A, '2024-12-31T00:00:00Z', 110),
        _price(INSTRUMENT_A, '2025-12-31T00:00:00Z', 140),
        _price(INSTRUMENT_A, '2026-12-31T00:00:00Z', 180),
    ]}
    result = _run([recommendation], prices, horizon='LONG_TERM',
                  now='2027-01-01T00:00:00Z')
    assert result['samples']['2Y'][0]['status'] == 'EVALUATED'
    assert result['samples']['2Y'][0]['signal_return_pct'] == pytest.approx(40.0)
    assert result['samples']['3Y'][0]['status'] == 'EVALUATED'
    assert result['samples']['3Y'][0]['signal_return_pct'] == pytest.approx(80.0)


def test_short_and_long_accuracy_never_mix():
    short = _original_rec(INSTRUMENT_A, '2024-01-01T00:00:00Z', horizon='SHORT_TERM')
    long = _original_rec(INSTRUMENT_B, '2024-01-01T00:00:00Z', horizon='LONG_TERM')
    prices = {
        INSTRUMENT_A: [_price(INSTRUMENT_A, '2024-01-08T00:00:00Z', 110)],
        INSTRUMENT_B: [_price(INSTRUMENT_B, '2024-12-31T00:00:00Z', 130)],
    }
    short_result = _run([short, long], prices, now='2025-01-02T00:00:00Z')
    assert short_result['metrics']['1W']['evaluated_count'] == 1
    assert short_result['metrics']['1W']['outside_evaluation_window_count'] == 1
    assert short_result['selected_recommendation_count'] == 1
    excluded_long = next(sample for sample in short_result['samples']['1W']
                         if sample['recommendation_id'] == long['recommendation_id'])
    assert excluded_long['recommendation_type'] == 'LONG_TERM'
    assert excluded_long['tested_recommendation_type'] == 'SHORT_TERM'
    assert excluded_long['action'] == 'BUY'

    long_result = _run([short, long], prices, horizon='LONG_TERM',
                       now='2025-01-02T00:00:00Z')
    assert long_result['metrics']['1Y']['evaluated_count'] == 1
    assert long_result['metrics']['1Y']['outside_evaluation_window_count'] == 1
    assert long_result['selected_recommendation_count'] == 1


def test_buy_and_sell_direction_semantics_are_independent_of_horizon():
    buy = _original_rec(INSTRUMENT_A, '2024-01-01T00:00:00Z', action='BUY')
    sell = _original_rec(INSTRUMENT_B, '2024-01-01T00:00:00Z', action='SELL')
    prices = {
        INSTRUMENT_A: [_price(INSTRUMENT_A, '2024-01-08T00:00:00Z', 110)],
        INSTRUMENT_B: [_price(INSTRUMENT_B, '2024-01-08T00:00:00Z', 90)],
    }
    result = _run([buy, sell], prices, now='2024-01-10T00:00:00Z')
    samples = result['samples']['1W']
    assert [sample['direction'] for sample in samples] == ['BUY', 'SELL']
    assert samples[0]['return_pct'] == pytest.approx(10.0)
    assert samples[1]['return_pct'] == pytest.approx(-10.0)
    assert [sample['signal_return_pct'] for sample in samples] == pytest.approx([10.0, 10.0])
    assert result['metrics']['1W']['success_rate'] == pytest.approx(100.0)
    assert result['metrics']['1W']['average_return'] == pytest.approx(10.0)

    long_sell = _original_rec(
        INSTRUMENT_C, '2024-01-01T00:00:00Z', horizon='LONG_TERM', action='AVOID')
    long_result = _run(
        [long_sell],
        {INSTRUMENT_C: [_price(INSTRUMENT_C, '2024-12-31T00:00:00Z', 80)]},
        horizon='LONG_TERM', now='2025-01-02T00:00:00Z')
    assert long_result['samples']['1Y'][0]['direction'] == 'SELL'
    assert long_result['samples']['1Y'][0]['signal_return_pct'] == pytest.approx(20.0)
    assert long_result['metrics']['1Y']['success_rate'] == pytest.approx(100.0)


def test_selection_period_filters_recommendation_date_not_evaluation_date():
    before_period = _original_rec(INSTRUMENT_A, '2023-12-01T00:00:00Z')
    within_period = _original_rec(INSTRUMENT_B, '2024-01-01T00:00:00Z')
    prices = {
        INSTRUMENT_A: [_price(INSTRUMENT_A, '2024-01-01T00:00:00Z', 110)],
        # Evaluation is after the selected end date, but the original
        # recommendation is inside it and must still be evaluated.
        INSTRUMENT_B: [_price(INSTRUMENT_B, '2024-01-08T00:00:00Z', 110)],
    }
    result = _run([before_period, within_period], prices,
                  start='2024-01-01T00:00:00Z', end='2024-01-02T00:00:00Z',
                  now='2024-01-10T00:00:00Z')
    by_id = {sample['recommendation_id']: sample for sample in result['samples']['1W']}
    assert by_id[before_period['recommendation_id']]['status'] == 'OUTSIDE_EVALUATION_WINDOW'
    assert by_id[within_period['recommendation_id']]['status'] == 'EVALUATED'


def test_run_backtest_uses_immutable_original_radar_type_action_date_and_price():
    from app.persistence import SqliteResearchPersistence

    store = SqliteResearchPersistence(':memory:')
    instrument_id = str(INSTRUMENT_A)

    def scan(scan_id, completed_at):
        return dict(scan_id=scan_id, started_at=completed_at, completed_at=completed_at,
            status='COMPLETED', market='NSE', exchange='NSE', universe_count=1,
            shortlist_count=1, evaluated_count=1, rank_eligible_count=1,
            suppressed_count=0, engine_version='TEST_ENGINE', controlled=False,
            failure_reason_code=None, created_at=completed_at)

    original_card = dict(global_instrument_id=instrument_id, horizon='LONG_TERM',
        public_action='BUY', symbol='TST', company_name='Test Co', current_price=100.0,
        price_at_recommendation=100.0, fingerprint='original-buy',
        evidence_snapshot={'persisted': True})
    store.persist_global_suggestion_lifecycle(
        scan('scan-original', '2024-01-01T00:00:00+00:00'), [original_card])

    # A later lifecycle SELL changes current state/history, never the original.
    later_sell = {**original_card, 'public_action': 'SELL', 'current_price': 80.0,
                  'price_at_recommendation': 80.0, 'fingerprint': 'later-sell'}
    store.persist_global_suggestion_lifecycle(
        scan('scan-later', '2024-02-01T00:00:00+00:00'), [later_sell])

    originals = store.radar_recommendation_history()
    assert len(originals) == 1
    assert originals[0]['horizon'] == 'LONG_TERM'
    assert originals[0]['action'] == 'BUY'
    assert originals[0]['generated_at'] == '2024-01-01T00:00:00+00:00'
    assert originals[0]['price_at_recommendation'] == pytest.approx(100.0)

    result = backtesting.run_backtest(
        store, start='2024-01-01T00:00:00Z', end='2024-01-01T23:59:59Z',
        horizon='LONG_TERM', as_of='2024-12-30T23:59:59Z')
    sample = result['samples']['1Y'][0]
    assert sample['status'] == 'NOT_MATURED'
    assert sample['action'] == 'BUY'
    assert sample['entry_price'] == pytest.approx(100.0)
