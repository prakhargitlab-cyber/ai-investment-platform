"""Point-in-time evaluation of immutable, persisted Radar recommendations.

The recommendation's persisted investment horizon and direction are independent
inputs to the evaluation. A SHORT_TERM recommendation is evaluated only at
7/30/91/182 calendar days; a LONG_TERM recommendation starts at 365 days and
may also be evaluated at 730/1095 days. Nothing in this module reruns today's
recommendation engine or infers either dimension from later price movement.

Every sample carries exactly one status:

  EVALUATED                 the due date arrived and both reference prices exist.
  NOT_MATURED               the due date is later than the run's as-of time.
  MISSING_MARKET_DATA       the due date arrived, but a reference price is absent.
  OUTSIDE_EVALUATION_WINDOW the immutable recommendation does not match the
                            selected recommendation date, market, type, or a
                            supported directional action.
  INSUFFICIENT_EVIDENCE     its persisted evidence was unavailable when issued.
  BACKEND_FAILURE           an unexpected row-local evaluation failure.
"""
from collections import Counter
from datetime import datetime, timedelta, timezone, time
from statistics import mean, median
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from app.models import MarketPriceObservation
from app.news_intelligence import EventImpactFeature, latest_known_features
from app.technical_features import normalize_price_history


SHORT_TERM_HORIZONS = {'1W': 7, '1M': 30, '3M': 91, '6M': 182}
LONG_TERM_HORIZONS = {'1Y': 365, '2Y': 730, '3Y': 1095}
HORIZONS_BY_RECOMMENDATION_TYPE = {
    'SHORT_TERM': SHORT_TERM_HORIZONS,
    'LONG_TERM': LONG_TERM_HORIZONS,
}
# Compatibility/export convenience. Evaluation always uses the type-specific
# mapping above, never this combined mapping.
HORIZONS = {**SHORT_TERM_HORIZONS, **LONG_TERM_HORIZONS}

BUY_ACTIONS = frozenset({'BUY', 'STRONG_BUY', 'ACCUMULATE', 'TOP_UP'})
SELL_ACTIONS = frozenset({'SELL', 'AVOID', 'PARTIAL_EXIT', 'EXIT', 'EXIT_REVIEW', 'REDUCE'})
STATUSES = ('EVALUATED', 'NOT_MATURED', 'MISSING_MARKET_DATA', 'OUTSIDE_EVALUATION_WINDOW',
            'INSUFFICIENT_EVIDENCE', 'BACKEND_FAILURE')


def utc(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00')) if isinstance(value, str) else value
    if result.tzinfo is None:
        raise ValueError('AWARE_DATE_REQUIRED')
    return result.astimezone(timezone.utc)


def evidence_available(value, at):
    """Conservative recursive guard for persisted availability/provenance timestamps."""
    if isinstance(value, dict):
        if 'news_features' in value:
            try:
                features = [EventImpactFeature.model_validate(f) for f in value['news_features']]
                if len(latest_known_features(features, at)) != len(features):
                    return False
            except (ValueError, TypeError):
                return False
        for key, item in value.items():
            normalized = key.replace('_', '').lower()
            if normalized in {'publishedat', 'publicationtime', 'publicavailabilityat', 'publicavailableat',
                              'discoveredat', 'computedat', 'calculatedat', 'retrievedat', 'asof', 'inputasof'} and item:
                try:
                    if utc(item) > at:
                        return False
                except (ValueError, TypeError, AttributeError):
                    return False
            if not evidence_available(item, at):
                return False
    elif isinstance(value, list):
        return all(evidence_available(item, at) for item in value)
    return True


def canonical_direction(action):
    """Map a persisted action to direction without consulting price outcomes."""
    normalized = str(action or '').strip().upper()
    if normalized in BUY_ACTIONS:
        return 'BUY'
    if normalized in SELL_ACTIONS:
        return 'SELL'
    return None


def _persisted_action(recommendation, recommendation_type):
    """Read action from the explicit V14 record or a legacy V13 horizon field."""
    explicit_type = recommendation.get('horizon') or recommendation.get('recommendation_type')
    if explicit_type:
        return (recommendation.get('action') or recommendation.get('initial_action')
                or recommendation.get('canonical_action') or recommendation.get('public_action'))
    field = 'short_term_action' if recommendation_type == 'SHORT_TERM' else 'long_term_action'
    return recommendation.get(field)


def _recommendation_id(recommendation):
    return str(recommendation.get('recommendation_id') or recommendation.get('suggestion_id'))


def _positive_price(value):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _persisted_reference_price(recommendation):
    """Return V14's immutable entry reference, never a mutable/legacy projection."""
    is_original_radar = bool(
        recommendation.get('horizon') or recommendation.get('recommendation_type')
        or recommendation.get('persistence_source') == 'GLOBAL_STOCK_SUGGESTION')
    return (_positive_price(recommendation.get('price_at_recommendation'))
            if is_original_radar else None)


def _sample(recommendation, *, status, action=None, direction=None, recommendation_type=None,
            global_instrument_id=None, evaluation_date=None, required_evaluation_date=None,
            return_pct=None, signal_return_pct=None, success=None, entry_price=None, exit_price=None,
            max_adverse_excursion_pct=None, benchmark_excess_return_pct=None):
    original_type = (recommendation.get('horizon') or recommendation.get('recommendation_type')
                     or recommendation_type)
    return dict(
        recommendation_id=_recommendation_id(recommendation),
        global_instrument_id=(str(global_instrument_id) if global_instrument_id is not None
                              else recommendation['global_instrument_id']),
        symbol=recommendation.get('symbol'), company_name=recommendation.get('company_name'),
        action=action, direction=direction, recommendation_type=original_type,
        tested_recommendation_type=recommendation_type,
        status=status, evaluation_date=evaluation_date,
        required_evaluation_date=required_evaluation_date,
        entry_price=entry_price, exit_price=exit_price,
        # return_pct is the auditable underlying stock move. signal_return_pct
        # applies the persisted direction and is what accuracy aggregates use.
        return_pct=return_pct, signal_return_pct=signal_return_pct, success=success,
        max_adverse_excursion_pct=max_adverse_excursion_pct,
        benchmark_excess_return_pct=benchmark_excess_return_pct,
    )


def metrics(samples):
    by_status = Counter(sample['status'] for sample in samples)
    evaluated = [sample for sample in samples if sample['status'] == 'EVALUATED']
    signal_returns = [sample['signal_return_pct'] for sample in evaluated]
    market_returns = [sample['return_pct'] for sample in evaluated]
    adverse = [sample['max_adverse_excursion_pct'] for sample in evaluated
               if sample['max_adverse_excursion_pct'] is not None]
    excess = [sample['benchmark_excess_return_pct'] for sample in evaluated
              if sample['benchmark_excess_return_pct'] is not None]
    success_rate = (100 * sum(sample['success'] is True for sample in evaluated) / len(evaluated)
                    if evaluated else None)
    return dict(
        recommendation_count=len(samples),
        evaluated_count=by_status.get('EVALUATED', 0),
        not_matured_count=by_status.get('NOT_MATURED', 0),
        missing_market_data_count=by_status.get('MISSING_MARKET_DATA', 0),
        outside_evaluation_window_count=by_status.get('OUTSIDE_EVALUATION_WINDOW', 0),
        insufficient_evidence_count=by_status.get('INSUFFICIENT_EVIDENCE', 0),
        backend_failure_count=by_status.get('BACKEND_FAILURE', 0),
        # Backward-compatible aliases retained for persisted/API consumers.
        missing_count=by_status.get('MISSING_MARKET_DATA', 0),
        hit_rate=success_rate,
        success_rate=success_rate,
        # Direction-adjusted returns allow BUY and SELL signals within the same
        # recommendation class to share a meaningful performance aggregate.
        average_return=mean(signal_returns) if signal_returns else None,
        median_return=median(signal_returns) if signal_returns else None,
        best_return=max(signal_returns) if signal_returns else None,
        worst_return=min(signal_returns) if signal_returns else None,
        average_market_return=mean(market_returns) if market_returns else None,
        max_adverse_excursion=min(adverse) if adverse else None,
        benchmark_excess_return=mean(excess) if excess else None,
    )


def evaluate_backtest(history, prices, *, start, end, horizon, now, benchmark_prices=None):
    start, end, now = utc(start), utc(end), utc(now)
    if start > end or end > now or horizon not in HORIZONS_BY_RECOMMENDATION_TYPE:
        raise ValueError('INVALID_BACKTEST_PARAMETERS')
    evaluation_horizons = HORIZONS_BY_RECOMMENDATION_TYPE[horizon]

    # Keep the complete submitted history accounted for while allowing only
    # matching original recommendation dates/types/actions into accuracy.
    cohort, outside_window, insufficient_evidence = [], [], []
    for recommendation in history:
        generated_at = utc(recommendation['generated_at'])
        explicit_type = recommendation.get('horizon') or recommendation.get('recommendation_type')
        action = _persisted_action(recommendation, horizon)
        direction = canonical_direction(action)
        context = (recommendation, action, direction)
        if (not start <= generated_at <= end or recommendation.get('market') != 'NSE'
                or (explicit_type is not None and explicit_type != horizon)
                or direction is None):
            outside_window.append(context)
            continue
        if not evidence_available(recommendation.get('evidence_snapshot', {}), generated_at):
            insufficient_evidence.append(context)
            continue
        cohort.append(context)

    output = {bucket: [] for bucket in evaluation_horizons}
    for recommendation, action, direction in outside_window:
        for bucket in evaluation_horizons:
            output[bucket].append(_sample(
                recommendation, status='OUTSIDE_EVALUATION_WINDOW', action=action,
                direction=direction, recommendation_type=horizon))
    for recommendation, action, direction in insufficient_evidence:
        generated_at = utc(recommendation['generated_at'])
        for bucket, days in evaluation_horizons.items():
            target = generated_at + timedelta(days=days)
            status = 'NOT_MATURED' if target > now else 'INSUFFICIENT_EVIDENCE'
            output[bucket].append(_sample(
                recommendation, status=status, action=action, direction=direction,
                recommendation_type=horizon, evaluation_date=generated_at.isoformat(),
                required_evaluation_date=target.isoformat()))

    for recommendation, action, direction in cohort:
        generated_at = utc(recommendation['generated_at'])
        instrument_id = UUID(recommendation['global_instrument_id'])
        rows = prices.get(instrument_id, [])
        matured_horizons = {}
        for bucket, days in evaluation_horizons.items():
            target = generated_at + timedelta(days=days)
            if target > now:
                output[bucket].append(_sample(
                    recommendation, status='NOT_MATURED', action=action,
                    direction=direction, recommendation_type=horizon,
                    global_instrument_id=instrument_id,
                    evaluation_date=generated_at.isoformat(),
                    required_evaluation_date=target.isoformat(),
                    entry_price=_persisted_reference_price(recommendation)))
            else:
                matured_horizons[bucket] = target
        if not matured_horizons:
            continue
        try:
            known = normalize_price_history(instrument_id, rows, as_of=generated_at)
            known_entry = (known.observations[-1]
                           if known.observations and not known.current_conflict else None)
            # V14's immutable suggestion price is the original Radar reference
            # price. Legacy V13 rows bundled both horizons and historically used
            # the last point-in-time market observation as their entry.
            persisted_entry = _persisted_reference_price(recommendation)
            entry_price = persisted_entry or (_positive_price(known_entry.price) if known_entry else None)
            observed = normalize_price_history(
                instrument_id, rows, as_of=now,
                currency=known_entry.currency if known_entry else None)
        except Exception:
            for bucket, target in matured_horizons.items():
                output[bucket].append(_sample(
                    recommendation, status='BACKEND_FAILURE', action=action,
                    direction=direction, recommendation_type=horizon,
                    global_instrument_id=instrument_id,
                    evaluation_date=generated_at.isoformat(),
                    required_evaluation_date=target.isoformat()))
            continue

        direction_sign = 1 if direction == 'BUY' else -1
        for bucket, target in matured_horizons.items():
            try:
                future = [price for price in observed.observations
                          if target <= utc(price.observed_at) <= target + timedelta(days=7)]
                exit_observation = future[0] if future else None
                exit_price = _positive_price(exit_observation.price) if exit_observation else None
                if entry_price is None or exit_price is None:
                    output[bucket].append(_sample(
                        recommendation, status='MISSING_MARKET_DATA', action=action,
                        direction=direction, recommendation_type=horizon,
                        global_instrument_id=instrument_id,
                        evaluation_date=generated_at.isoformat(),
                        required_evaluation_date=target.isoformat(),
                        entry_price=entry_price, exit_price=exit_price))
                    continue

                market_return = (exit_price / entry_price - 1) * 100
                signal_return = direction_sign * market_return
                directional_window = [
                    direction_sign * (_positive_price(price.price) / entry_price - 1) * 100
                    for price in observed.observations
                    if (_positive_price(price.price) is not None
                        and generated_at < utc(price.observed_at) <= utc(exit_observation.observed_at))
                ]
                excess = None
                if benchmark_prices:
                    benchmark_id = benchmark_prices[0].instrument_id
                    before = normalize_price_history(benchmark_id, benchmark_prices, as_of=generated_at)
                    after = normalize_price_history(benchmark_id, benchmark_prices, as_of=now)
                    matching = [price for price in after.observations
                                if utc(price.observed_at).date() == utc(exit_observation.observed_at).date()]
                    if before.observations and not before.current_conflict and matching:
                        benchmark_return = (
                            float(matching[0].price / before.observations[-1].price) - 1) * 100
                        excess = direction_sign * (market_return - benchmark_return)
                output[bucket].append(_sample(
                    recommendation, status='EVALUATED', action=action, direction=direction,
                    recommendation_type=horizon, global_instrument_id=instrument_id,
                    evaluation_date=generated_at.isoformat(),
                    required_evaluation_date=target.isoformat(), entry_price=entry_price,
                    exit_price=exit_price, return_pct=market_return,
                    signal_return_pct=signal_return, success=signal_return > 0,
                    max_adverse_excursion_pct=(min([0, *directional_window])
                                               if directional_window else None),
                    benchmark_excess_return_pct=excess))
            except Exception:
                output[bucket].append(_sample(
                    recommendation, status='BACKEND_FAILURE', action=action,
                    direction=direction, recommendation_type=horizon,
                    global_instrument_id=instrument_id,
                    evaluation_date=generated_at.isoformat(),
                    required_evaluation_date=target.isoformat(), entry_price=entry_price))

    example_horizon = '1M' if horizon == 'SHORT_TERM' else '1Y'
    examples = sorted(
        [sample for sample in output[example_horizon] if sample['status'] == 'EVALUATED'],
        key=lambda sample: sample['signal_return_pct'])
    return dict(
        backtest_id=str(uuid4()), generated_at=now.isoformat(), engine_version='BACKTESTING_V3',
        start=start.isoformat(), end=end.isoformat(), as_of=now.isoformat(), market='NSE',
        horizon=horizon, recommendation_type=horizon,
        available_horizons=list(evaluation_horizons), example_horizon=example_horizon,
        recommendation_count=len(cohort) + len(outside_window) + len(insufficient_evidence),
        selected_recommendation_count=len(cohort) + len(insufficient_evidence),
        excluded_unavailable_evidence=len(insufficient_evidence),
        methodology=(
            'Immutable original Radar recommendations; selection period applies to recommendation date; '
            'type-specific calendar horizons; persisted entry price (or last point-in-time close); first '
            'available close within 7 days of evaluation due date; direction-adjusted success and aggregate '
            'return; raw stock return retained per sample; close-based adverse excursion.'),
        metrics={bucket: metrics(samples) for bucket, samples in output.items()}, samples=output,
        winners=[sample for sample in reversed(examples) if sample['success']][:4],
        losers=[sample for sample in examples if not sample['success']][:4],
    )


def run_backtest(persistence, *, start, end, horizon, benchmark_id=None, as_of=None):
    # V14 stores the original Radar type/action/date/price immutably. Use it in
    # preference to V13's bundled legacy recommendation payloads.
    radar_history = getattr(persistence, 'radar_recommendation_history', None)
    history = radar_history() if radar_history is not None else []
    if not history:
        history = persistence.recommendation_history()
    keys = {UUID(recommendation['global_instrument_id']) for recommendation in history}
    rows = persistence.load_market_price_observations(keys) if keys else []
    prices = {key: [] for key in keys}
    for row in rows:
        prices[row.instrument_id].append(row)
    # Preserve each bar's retrieval clock: a later correction cannot become an
    # entry at recommendation time.
    for bar in persistence.load_daily_market_bars(keys) if keys else []:
        if bar.close is None or bar.close <= 0 or bar.source_mode != 'REAL':
            continue
        stamp = datetime.combine(
            bar.trading_date, time(15, 30), ZoneInfo('Asia/Kolkata')).astimezone(timezone.utc)
        prices[bar.global_instrument_id].append(MarketPriceObservation(
            instrument_id=bar.global_instrument_id, observed_at=stamp,
            retrieved_at=bar.retrieved_at, price=bar.close, currency=bar.currency,
            provider=bar.provider, source_url=bar.source_url))
    benchmark = persistence.load_market_price_observations({benchmark_id}) if benchmark_id else None
    result = evaluate_backtest(
        history, prices, start=start, end=end, horizon=horizon,
        now=utc(as_of) if as_of else datetime.now(timezone.utc), benchmark_prices=benchmark)
    return persistence.save_backtest(result)
