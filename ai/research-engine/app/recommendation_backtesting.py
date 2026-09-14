"""Recorded-recommendation cohort backtesting; no retrospective evidence reconstruction.

Entry is the first persisted price known at evaluation T. An immutable recommendation
must itself have existed by T. This conservative boundary also excludes V12 features
computed/discovered after T; current exposure/event tables are never queried.
"""
from datetime import datetime, timedelta, timezone, time
from statistics import mean, median
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo
from app.models import MarketPriceObservation
from app.technical_features import normalize_price_history
from app.news_intelligence import EventImpactFeature, latest_known_features

HORIZONS = {'1W': 7, '1M': 30, '3M': 91, '6M': 182, '1Y': 365}


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


def metrics(samples):
    returns = [s['return_pct'] for s in samples if s['return_pct'] is not None]
    adverse = [s['max_adverse_excursion_pct'] for s in samples if s['max_adverse_excursion_pct'] is not None]
    excess = [s['benchmark_excess_return_pct'] for s in samples if s['benchmark_excess_return_pct'] is not None]
    return dict(recommendation_count=len(samples), evaluated_count=len(returns), missing_count=len(samples)-len(returns),
        hit_rate=100 * sum(v > 0 for v in returns)/len(returns) if returns else None,
        average_return=mean(returns) if returns else None, median_return=median(returns) if returns else None,
        best_return=max(returns) if returns else None, worst_return=min(returns) if returns else None,
        max_adverse_excursion=min(adverse) if adverse else None,
        benchmark_excess_return=mean(excess) if excess else None)


def evaluate_backtest(history, prices, *, start, end, horizon, now, benchmark_prices=None):
    start, end, now = utc(start), utc(end), utc(now)
    if start > end or end > now or horizon not in {'SHORT_TERM', 'LONG_TERM'}:
        raise ValueError('INVALID_BACKTEST_PARAMETERS')
    actions = {'BUY'} if horizon == 'SHORT_TERM' else {'ACCUMULATE', 'TOP_UP'}
    field = 'short_term_action' if horizon == 'SHORT_TERM' else 'long_term_action'
    cohort, excluded = [], 0
    for r in history:
        t = utc(r['generated_at'])
        if not start <= t <= end or r.get('market') != 'NSE' or r[field] not in actions:
            continue
        if not evidence_available(r.get('evidence_snapshot', {}), t):
            excluded += 1
            continue
        cohort.append(r)
    output = {h: [] for h in HORIZONS}
    for r in cohort:
        t, key = utc(r['generated_at']), UUID(r['global_instrument_id'])
        rows = prices.get(key, [])
        known = normalize_price_history(key, rows, as_of=t)
        entry = known.observations[-1] if known.observations and not known.current_conflict else None
        observed = normalize_price_history(key, rows, as_of=now, currency=entry.currency if entry else None)
        for h, days in HORIZONS.items():
            target = t + timedelta(days=days)
            future = [p for p in observed.observations if target <= utc(p.observed_at) <= target + timedelta(days=7)] if target <= now else []
            exit_price = future[0] if future else None
            value = (float(exit_price.price / entry.price) - 1) * 100 if entry and exit_price else None
            window = [float(p.price / entry.price - 1) * 100 for p in observed.observations
                      if entry and exit_price and t < utc(p.observed_at) <= utc(exit_price.observed_at)]
            excess = None
            if benchmark_prices and value is not None:
                bkey = benchmark_prices[0].instrument_id
                before = normalize_price_history(bkey, benchmark_prices, as_of=t)
                after = normalize_price_history(bkey, benchmark_prices, as_of=now)
                matching = [p for p in after.observations if utc(p.observed_at).date() == utc(exit_price.observed_at).date()]
                if before.observations and not before.current_conflict and matching:
                    excess = value - (float(matching[0].price / before.observations[-1].price) - 1) * 100
            output[h].append(dict(recommendation_id=r['recommendation_id'], global_instrument_id=str(key),
                symbol=r.get('symbol'), company_name=r.get('company_name'),
                evaluation_date=t.isoformat(), return_pct=value,
                max_adverse_excursion_pct=min([0, *window]) if window else None,
                benchmark_excess_return_pct=excess,
                status='AVAILABLE' if value is not None else 'ENTRY_PRICE_UNAVAILABLE' if not entry else 'FUTURE_PRICE_UNAVAILABLE'))
    examples = sorted([s for s in output['1M' if horizon == 'SHORT_TERM' else '1Y'] if s['return_pct'] is not None], key=lambda s: s['return_pct'])
    return dict(backtest_id=str(uuid4()), generated_at=now.isoformat(), engine_version='BACKTESTING_V1',
        start=start.isoformat(), end=end.isoformat(), market='NSE', horizon=horizon,
        recommendation_count=len(cohort), excluded_unavailable_evidence=excluded,
        methodology='Recorded recommendations; calendar horizons, first available close within 7 days; unadjusted price returns; close-based MAE.',
        metrics={h: metrics(v) for h, v in output.items()}, samples=output,
        winners=[s for s in reversed(examples) if s['return_pct'] > 0][:4],
        losers=[s for s in examples if s['return_pct'] <= 0][:4])


def run_backtest(persistence, *, start, end, horizon, benchmark_id=None):
    history = persistence.recommendation_history()
    keys = {UUID(r['global_instrument_id']) for r in history}
    rows = persistence.load_market_price_observations(keys) if keys else []
    prices = {key: [] for key in keys}
    for row in rows:
        prices[row.instrument_id].append(row)
    # Preserve each bar's retrieval clock: a later correction cannot become an entry at T.
    # Daily and quote conflicts are still resolved by the existing history normalizer.
    for bar in persistence.load_daily_market_bars(keys) if keys else []:
        if bar.close is None or bar.close <= 0 or bar.source_mode != 'REAL':
            continue
        stamp = datetime.combine(bar.trading_date, time(15, 30), ZoneInfo('Asia/Kolkata')).astimezone(timezone.utc)
        prices[bar.global_instrument_id].append(MarketPriceObservation(
            instrument_id=bar.global_instrument_id, observed_at=stamp, retrieved_at=bar.retrieved_at,
            price=bar.close, currency=bar.currency, provider=bar.provider, source_url=bar.source_url))
    benchmark = persistence.load_market_price_observations({benchmark_id}) if benchmark_id else None
    result = evaluate_backtest(history, prices, start=start, end=end, horizon=horizon,
        now=datetime.now(timezone.utc), benchmark_prices=benchmark)
    return persistence.save_backtest(result)
