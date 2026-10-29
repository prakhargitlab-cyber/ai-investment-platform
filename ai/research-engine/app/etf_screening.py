"""Versioned research presentation, null-aware screening and entry evidence.

Overview prioritizes peer rank, then existing score, then canonical ID. It is
a research queue across objectives, not a claim of interchangeable exposures.
"""
from decimal import Decimal, InvalidOperation


def research_overview(ranked):
    ordered = sorted((c for c in ranked if c.get('recommendation') in
                      {'OPPORTUNITY', 'STRONG_OPPORTUNITY'} and c.get('rank') is not None),
                     key=lambda c: (c['rank'], -(c.get('rule_engine_result') or {}).get('overall_score', 0),
                                    c['global_instrument_id']))
    return [{**c, 'overview_rank': n} for n, c in enumerate(ordered[:10], 1)]


def enrich_candidate(candidate, *, now):
    factors = (candidate.get('rule_engine_result') or {}).get('factor_results', [])
    candidate['positive_signals'] = [f['factor'] for f in factors
                                   if f.get('normalized_score') is not None and f['normalized_score'] >= 60]
    candidate['negative_signals'] = [g['reason'] for g in candidate.get('risk_gates', []) if g['triggered']]
    candidate['entry_assessment'] = {
        'classification': 'INSUFFICIENT_EVIDENCE', 'price_band': None,
        'conditions': ['No validated current entry-price methodology is available; research score alone is not an entry signal.'],
        'calculated_at': now.isoformat(), 'methodology': 'ENTRY_EVIDENCE_V1'}
    inputs = candidate.get('entry_inputs', {})
    needed = ('premium_percent', 'momentum_percent', 'return_1m_percent', 'spread_percent', 'volume')
    if all(inputs.get(k) is not None for k in needed) and inputs.get('current'):
        p, m, r, spread, volume = (Decimal(str(inputs[k])) for k in needed)
        if p > 1 or r > 10:
            classification = 'EXTENDED_AVOID_CHASING'
        elif abs(p) <= Decimal('0.5') and m >= 0 and 0 <= r <= 5 and spread <= Decimal('0.5') and volume >= 1000:
            classification = 'FAVORABLE_ENTRY_ZONE'
        else:
            classification = 'NEUTRAL_WAIT'
        candidate['entry_assessment'].update(classification=classification,
            conditions=[f'Same-date premium {p}%; 50/200 momentum {m}%; 21-session price return {r}%; observed spread {spread}%; session volume {volume}.',
                        'Favorable: absolute premium <=0.5%, momentum >=0%, return 0–5%, spread <=0.5%, volume >=1000. Extended: premium >1% or return >10%. Otherwise wait.'])


TEXT_FILTERS = {'category', 'benchmark', 'fund_house', 'distribution_type', 'replication_method',
                'domicile', 'recommendation', 'peer_group', 'disposition', 'investment_eligibility'}
METRIC_UNITS = {'expense_ratio': 'PERCENT_PA', 'aum': 'INR', 'fund_age': 'YEARS',
                'trading_volume': 'UNITS', 'traded_value': 'INR', 'bid_ask_spread': 'PERCENT',
                'tracking_difference': 'PERCENT', 'tracking_error': 'PERCENT',
                'volatility': 'PERCENT', 'drawdown': 'PERCENT', 'concentration': 'PERCENT',
                'data_completeness': 'FRACTION'}


def screening_evidence(store, key, selected, candidate, *, now):
    from datetime import date
    from app.etf_evidence import EtfMetric, etf_freshness
    from app.etf_metrics import compute_etf_deterministic_metrics, EtfMetricId
    from app.etf_comparison import holdings_coverage
    from app.etf_readiness import _DERIVED_METRIC_MAX_AGE_DAYS
    metrics, fields = {}, {}
    def metric(name, value, unit, valid=True):
        metrics[name] = {'value': str(value) if value is not None else None, 'unit': unit,
                         'quality': 'VALID' if valid and value is not None else 'UNAVAILABLE'}
    for name, enum in [('expense_ratio', 'EXPENSE_RATIO'), ('aum', 'AUM'), ('trading_volume', 'TRADING_VOLUME'),
                       ('traded_value', 'TRADED_VALUE'), ('bid_ask_spread', 'BID_ASK_SPREAD'),
                       ('tracking_error', 'TRACKING_ERROR'), ('tracking_difference', 'TRACKING_DIFFERENCE')]:
        fact = selected.get(enum)
        unit = METRIC_UNITS[name]
        aliases = {'PERCENT_PA': {'percent', 'PERCENT_PA', 'PERCENT'}, 'PERCENT': {'percent', 'PERCENT'},
                   'UNITS': {'UNITS', 'units', 'shares', 'SHARES'}}.get(unit, {unit})
        valid = fact is not None and fact.unit in aliases and etf_freshness(fact, EtfMetric(enum), now=now) == 'FRESH'
        metric(name, fact.value if valid else None, unit, valid)
    for name, enum in [('fund_house', 'AMC_FUND_HOUSE'), ('domicile', 'DOMICILE'),
                       ('distribution_type', 'DISTRIBUTION_POLICY'), ('replication_method', 'REPLICATION_METHOD')]:
        fact = selected.get(enum)
        fields[name] = str(fact.value) if fact and etf_freshness(fact, EtfMetric(enum), now=now) == 'FRESH' else 'UNKNOWN'
    inception = selected.get('FUND_INCEPTION_DATE')
    age = Decimal((now.date()-date.fromisoformat(inception.value)).days)/Decimal('365.25') if inception else None
    metric('fund_age', age, 'YEARS', age is not None and age >= 0)
    bars = store.load_daily_market_bars({key})
    calculated = compute_etf_deterministic_metrics(key, bars)
    price, nav, spread, volume = (selected.get(k) for k in ('MARKET_PRICE', 'NAV', 'BID_ASK_SPREAD', 'TRADING_VOLUME'))
    current = bool(price and nav and nav.nav > 0 and price.unit == nav.currency and price.as_of_date == nav.nav_date
                   and etf_freshness(price, EtfMetric.MARKET_PRICE, now=now) == 'FRESH'
                   and calculated.as_of_date == price.as_of_date
                   and metrics['bid_ask_spread']['quality'] == metrics['trading_volume']['quality'] == 'VALID')
    entry_inputs = {'current': current,
        'premium_percent': str((price.value/nav.nav-1)*100) if current else None,
        'momentum_percent': str(calculated.get(EtfMetricId.MOMENTUM_50_200).value) if calculated.get(EtfMetricId.MOMENTUM_50_200).value is not None else None,
        'return_1m_percent': str(calculated.get(EtfMetricId.RETURN_1M).value) if calculated.get(EtfMetricId.RETURN_1M).value is not None else None,
        'spread_percent': metrics['bid_ask_spread']['value'], 'volume': metrics['trading_volume']['value']}
    for name, enum in [('volatility', EtfMetricId.ANNUALIZED_VOLATILITY), ('drawdown', EtfMetricId.MAX_DRAWDOWN)]:
        value = calculated.get(enum)
        metric(name, value.value if value else None, 'PERCENT', value is not None and not value.insufficient_history
               and 0 <= (now.date()-value.as_of_date).days <= _DERIVED_METRIC_MAX_AGE_DAYS)
    snapshots = store.etf_holdings(key)
    coverage = holdings_coverage(snapshots[0] if snapshots else None, now=now)
    metric('concentration', coverage.get('top10_concentration'), 'PERCENT', coverage['status'] == 'VALID')
    metric('data_completeness', (candidate.get('rule_engine_result') or {}).get('data_completeness'), 'FRACTION')
    return {**fields, 'screening_metrics': metrics, 'entry_inputs': entry_inputs, 'history_observations': len(bars),
            'holdings_coverage': str(coverage['coverage']) if coverage['coverage'] is not None else None}


def screen_page(payload, *, filters=None, search='', sort='symbol', descending=False, page=1, page_size=25):
    if page < 1 or not 1 <= page_size <= 100:
        raise ValueError('INVALID_PAGINATION')
    if sort not in {'symbol', 'score', 'rank', 'category', 'recommendation'}:
        raise ValueError('INVALID_SORT')
    filters = filters or {}
    for key, value in filters.items():
        if key in TEXT_FILTERS:
            if not isinstance(value, str):
                raise ValueError('INVALID_TEXT_FILTER')
        elif key in METRIC_UNITS:
            if not isinstance(value, dict) or value.get('unit') != METRIC_UNITS[key]:
                raise ValueError('INVALID_FILTER_UNIT')
            for bound in ('min', 'max'):
                if bound in value:
                    try:
                        number = Decimal(str(value[bound]))
                    except InvalidOperation:
                        raise ValueError('INVALID_FILTER_NUMBER') from None
                    if not number.is_finite():
                        raise ValueError('INVALID_FILTER_NUMBER')
            if 'min' in value and 'max' in value and Decimal(str(value['min'])) > Decimal(str(value['max'])):
                raise ValueError('INVALID_FILTER_RANGE')
        else:
            raise ValueError('UNKNOWN_FILTER')

    def matches(c):
        if search.casefold() not in ' '.join(str(c.get(k) or '') for k in ('symbol', 'fund_name', 'benchmark')).casefold():
            return False
        for key, expected in filters.items():
            if key in TEXT_FILTERS:
                if str(c.get(key) or '').casefold() != expected.casefold():
                    return False
            else:
                observation = c.get('screening_metrics', {}).get(key) or {}
                value = observation.get('value')
                if value is None or observation.get('unit') != expected['unit'] or observation.get('quality') != 'VALID':
                    return False
                number = Decimal(str(value))
                if not number.is_finite() or ('min' in expected and number < Decimal(str(expected['min']))) or (
                        'max' in expected and number > Decimal(str(expected['max']))):
                    return False
        return True
    rows = [c for c in payload.get('candidates', []) if matches(c)]
    def value(c):
        return (c.get('rule_engine_result') or {}).get('overall_score') if sort == 'score' else c.get(sort)
    known = [c for c in rows if value(c) is not None]
    missing = [c for c in rows if value(c) is None]
    known.sort(key=lambda c: str(c.get('global_instrument_id') or c.get('symbol')))
    known.sort(key=value, reverse=descending)
    rows = known + sorted(missing, key=lambda c: str(c.get('global_instrument_id') or c.get('symbol')))
    start = (page-1)*page_size
    return {'cycle_id': payload['cycle_id'], 'items': rows[start:start+page_size], 'total': len(rows),
            'page': page, 'page_size': page_size, 'pages': (len(rows)+page_size-1)//page_size}
