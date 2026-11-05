"""Versioned research presentation, null-aware screening and entry evidence.

Overview prioritizes peer rank, then existing score, then canonical ID. It is
a research queue across objectives, not a claim of interchangeable exposures.
"""
from decimal import Decimal, InvalidOperation


REQUIRED_RECOMMENDATION_METRICS = ('expense_ratio', 'aum', 'tracking_error',
                                   'tracking_difference', 'bid_ask_spread')


def readiness_payload(statuses=None, summary=None):
    """Preserve observed requirement failures without inventing absent checks."""
    if statuses is None or summary is None:
        return {'observed': False, 'status': 'UNASSESSED', 'summary': None, 'requirements': None}
    import dataclasses
    import json
    return json.loads(json.dumps({
        'observed': True, 'status': 'READY' if summary.ready_for_analysis else 'NOT_READY',
        'summary': dataclasses.asdict(summary),
        'requirements': [dataclasses.asdict(item) for item in statuses],
    }, default=lambda value: value.isoformat() if hasattr(value, 'isoformat') else str(value)))


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
    evaluated = candidate.get('disposition') == 'EVALUATED' and candidate.get('rule_engine_result') is not None
    gates = candidate.get('risk_gates')
    candidate['analysis_status'] = 'EVALUATED' if evaluated else 'NOT_EVALUATED'
    candidate['risk_assessment'] = {'status': 'EVALUATED' if evaluated else 'NOT_EVALUATED',
        'triggered_gates': [g['gate_id'] for g in (gates or []) if g.get('triggered')] if evaluated else None,
        'reason': None if evaluated else candidate.get('reason', 'ANALYSIS_UNAVAILABLE')}
    candidate['negative_signals'] = [g['reason'] for g in (gates or []) if g.get('triggered')] if evaluated else None
    if not evaluated:
        candidate['risk_gates'] = None
    readiness = candidate.get('research_readiness') or readiness_payload()
    candidate['research_readiness'] = readiness
    candidate['entry_assessment'] = {
        'classification': 'INSUFFICIENT_EVIDENCE', 'price_band': None,
        'conditions': ['Entry assessment requires verified same-session price, volume, spread and NAV, usable history, and mandatory readiness. A research score alone is not an entry signal.'],
        'calculated_at': now.isoformat(), 'methodology': 'ENTRY_EVIDENCE_V1'}
    inputs = candidate.get('entry_inputs', {})
    needed = ('premium_percent', 'momentum_percent', 'return_1m_percent', 'spread_percent', 'volume')
    mandatory_ready = (readiness.get('summary') or {}).get('ready_for_analysis') is True
    if (evaluated and mandatory_ready and inputs.get('verified') is True
            and all(inputs.get(k) is not None for k in needed) and inputs.get('current')):
        try:
            p, m, r, spread, volume = (Decimal(str(inputs[k])) for k in needed)
            valid_numbers = all(v.is_finite() for v in (p, m, r, spread, volume)) and spread >= 0 and volume > 0
        except (InvalidOperation, ValueError, TypeError):
            valid_numbers = False
        if not valid_numbers:
            inputs = {**inputs, 'current': False, 'verified': False}
            candidate['entry_inputs'] = inputs
        else:
            _classify_entry(candidate, p, m, r, spread, volume)
    missing = [name for name in REQUIRED_RECOMMENDATION_METRICS
               if (candidate.get('screening_metrics') or {}).get(name, {}).get('quality') != 'VALID'
               or (candidate.get('screening_metrics') or {}).get(name, {}).get('value') is None]
    candidate['evidence_completeness'] = {
        'status': 'COMPLETE' if not missing else 'UNAVAILABLE' if len(missing) == 5 else 'PARTIAL',
        'total': 5, 'available': 5-len(missing), 'required_metrics': list(REQUIRED_RECOMMENDATION_METRICS),
        'available_metrics': [name for name in REQUIRED_RECOMMENDATION_METRICS if name not in missing],
        'missing_metrics': missing}
    unavailable = [f['factor'] for f in factors if f.get('status') == 'UNAVAILABLE']
    partial = [f['factor'] for f in factors if f.get('status') == 'SCORED'
               and f.get('confidence') is not None and f['confidence'] < 1]
    candidate['score_basis'] = {
        'status': 'UNAVAILABLE' if not evaluated else 'PRELIMINARY' if not factors or unavailable or partial or missing else 'COMPLETE',
        'method': 'AVAILABLE_FACTOR_WEIGHT_NORMALIZATION',
        'available_weight_percent': sum(f['weight'] for f in factors if f.get('status') == 'SCORED') if evaluated and factors else None,
        'unavailable_factors': unavailable,
        'partial_factors': partial,
        'explanation': 'Research scores normalize available factor weights; missing history or fund evidence remains missing. A score is not an entry signal.'}
    requirement_gaps = [r['requirement'] for r in readiness.get('requirements') or []
                        if r.get('role') in {'MANDATORY', 'SUPPORTING'} and r.get('status') != 'READY_FRESH']
    candidate['missing_data'] = list(dict.fromkeys([*(candidate.get('missing_data') or []), *requirement_gaps, *unavailable]))
    classification = candidate['entry_assessment']['classification']
    actionable = evaluated and mandatory_ready and not missing and classification == 'FAVORABLE_ENTRY_ZONE' and candidate.get('recommendation') in {'OPPORTUNITY', 'STRONG_OPPORTUNITY'}
    candidate['actionable_recommendation'] = {
        'status': classification if evaluated and mandatory_ready and not missing else 'INSUFFICIENT_EVIDENCE',
        'is_actionable': actionable,
        'explanation': ('Verified entry conditions and the five required fund/liquidity metrics are available.'
                        if actionable else 'Research ranking alone does not establish an actionable entry; review missing evidence and entry conditions.')}


def _classify_entry(candidate, p, m, r, spread, volume):
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


def _entry_session_verified(key, price, volume, spread, nav, *, now, candidate, calculated):
    """A fund NAV may have a different issuer; market observations share a venue."""
    from urllib.parse import urlsplit
    from app.etf_evidence import EtfMetric, etf_freshness
    readiness = candidate.get('research_readiness') or {}
    if (candidate.get('disposition') != 'EVALUATED'
            or (readiness.get('summary') or {}).get('ready_for_analysis') is not True
            or any(value is None for value in (price, volume, spread, nav))):
        return False
    try:
        if (any(f.instrument_id != key for f in (price, volume, spread, nav))
                or price.unit != nav.currency or nav.currency != 'INR'
                or volume.unit not in {'UNITS', 'units', 'shares', 'SHARES'}
                or spread.unit not in {'percent', 'PERCENT'}
                or price.value <= 0 or nav.nav <= 0 or volume.value <= 0
                or volume.value != volume.value.to_integral_value() or spread.value < 0
                or len({price.as_of_date, volume.as_of_date, spread.as_of_date, nav.nav_date,
                        calculated.as_of_date}) != 1):
            return False
        for fact, metric in ((price, EtfMetric.MARKET_PRICE), (volume, EtfMetric.TRADING_VOLUME),
                             (spread, EtfMetric.BID_ASK_SPREAD), (nav, EtfMetric.NAV)):
            p = fact.provenance
            if (p.source_mode != 'REAL' or p.retrieved_at > now
                    or (p.published_at is not None and p.published_at > now)
                    or not p.source_url or etf_freshness(fact, metric, now=now) != 'FRESH'
                    or (p.source_metadata or {}).get('synthetic') is True):
                return False
        if nav.provenance.authority not in {'OFFICIAL_FUND', 'OFFICIAL_EXCHANGE'}:
            return False
        market = (price, volume, spread)
        if len({fact.provenance.provider for fact in market}) != 1:
            return False
        venues = []
        for fact in market:
            p, md = fact.provenance, fact.provenance.source_metadata or {}
            url = urlsplit(p.source_url)
            allowed_hosts = ({'nseindia.com', 'www.nseindia.com', 'nsearchives.nseindia.com', 'archives.nseindia.com'}
                             if p.provider == 'NSE' and p.authority == 'OFFICIAL_EXCHANGE' else
                             {'finance.yahoo.com', 'query1.finance.yahoo.com', 'query2.finance.yahoo.com'}
                             if p.provider in {'YAHOO_FINANCE', 'YAHOO_FINANCE_MCP'} else set())
            if (url.scheme != 'https' or url.hostname not in allowed_hosts or url.port not in {None, 443}
                    or url.username is not None or url.password is not None):
                return False
            venue = md.get('exchange') or md.get('mic')
            if not venue and p.provider == 'NSE' and p.authority == 'OFFICIAL_EXCHANGE':
                host = urlsplit(p.source_url).hostname or ''
                if host in {'nseindia.com', 'www.nseindia.com', 'nsearchives.nseindia.com', 'archives.nseindia.com'}:
                    venue = 'NSE'
            venues.append({'XNSE': 'NSE', 'NSI': 'NSE'}.get(venue, venue))
        if set(venues) != {'NSE'}:
            return False
        # Explicit security/session metadata, when supplied, must agree too.
        for field in ('security_code', 'isin', 'session_timestamp'):
            values = [(fact.provenance.source_metadata or {}).get(field) for fact in market]
            if any(v is not None for v in values) and len(set(values)) != 1:
                return False
        for container, fields in (('record', ('symbol', 'identifier', 'series')), ('session', ('timestamp',))):
            for field in fields:
                values = [((fact.provenance.source_metadata or {}).get(container) or {}).get(field) for fact in market]
                if any(value is not None for value in values) and len(set(values)) != 1:
                    return False
        return True
    except (AttributeError, InvalidOperation, TypeError, ValueError):
        return False


def screening_evidence(store, key, selected, candidate, *, now):
    from datetime import date
    from app.etf_evidence import EtfMetric, etf_freshness
    from app.etf_metrics import compute_etf_deterministic_metrics, EtfMetricId
    from app.etf_comparison import holdings_coverage
    from app.etf_readiness import _DERIVED_METRIC_MAX_AGE_DAYS
    metrics, fields = {}, {}
    def metric(name, value, unit, valid=True, reason=None):
        available = valid and value is not None
        metrics[name] = {'value': str(value) if available else None, 'unit': unit,
                         'quality': 'VALID' if available else 'UNAVAILABLE',
                         'reason': None if available else reason or 'NO_VERIFIED_CURRENT_EVIDENCE'}
    for name, enum in [('expense_ratio', 'EXPENSE_RATIO'), ('aum', 'AUM'), ('trading_volume', 'TRADING_VOLUME'),
                       ('traded_value', 'TRADED_VALUE'), ('bid_ask_spread', 'BID_ASK_SPREAD'),
                       ('tracking_error', 'TRACKING_ERROR'), ('tracking_difference', 'TRACKING_DIFFERENCE')]:
        fact = selected.get(enum)
        unit = METRIC_UNITS[name]
        aliases = {'PERCENT_PA': {'percent', 'PERCENT_PA', 'PERCENT'}, 'PERCENT': {'percent', 'PERCENT'},
                   'UNITS': {'UNITS', 'units', 'shares', 'SHARES'}}.get(unit, {unit})
        valid = (fact is not None and fact.unit in aliases and fact.provenance.source_mode == 'REAL'
                 and fact.instrument_id == key and fact.provenance.retrieved_at <= now
                 and bool(fact.provenance.source_url)
                 and (fact.provenance.published_at is None or fact.provenance.published_at <= now)
                 and etf_freshness(fact, EtfMetric(enum), now=now) == 'FRESH')
        reason = ('NO_PERSISTED_METRIC_EVIDENCE' if fact is None else
                  'INCOMPATIBLE_METRIC_UNIT' if fact.unit not in aliases else
                  'EVIDENCE_INSTRUMENT_MISMATCH' if fact.instrument_id != key else
                  'NON_REAL_EVIDENCE' if fact.provenance.source_mode != 'REAL' else
                  'FUTURE_EVIDENCE_TIMESTAMP' if fact.provenance.retrieved_at > now
                  or fact.provenance.published_at is not None and fact.provenance.published_at > now else
                  'MISSING_SOURCE_PROVENANCE' if not fact.provenance.source_url else
                  etf_freshness(fact, EtfMetric(enum), now=now))
        metric(name, fact.value if valid else None, unit, valid, reason)
    for name, enum in [('fund_house', 'AMC_FUND_HOUSE'), ('domicile', 'DOMICILE'),
                       ('distribution_type', 'DISTRIBUTION_POLICY'), ('replication_method', 'REPLICATION_METHOD')]:
        fact = selected.get(enum)
        fields[name] = str(fact.value) if fact and etf_freshness(fact, EtfMetric(enum), now=now) == 'FRESH' else 'UNKNOWN'
    inception = selected.get('FUND_INCEPTION_DATE')
    age = (Decimal((now.date()-date.fromisoformat(inception.value)).days)/Decimal('365.25')
           if inception and etf_freshness(inception, EtfMetric.FUND_INCEPTION_DATE, now=now) == 'FRESH' else None)
    metric('fund_age', age, 'YEARS', age is not None and age >= 0, 'NO_VERIFIED_CURRENT_INCEPTION_EVIDENCE')
    from app.etf_history_selection import select_etf_history_bars
    bars, history_diagnostics = select_etf_history_bars(store.load_daily_market_bars({key}), key, now=now)
    calculated = compute_etf_deterministic_metrics(key, bars)
    price, nav, spread, volume = (selected.get(k) for k in ('MARKET_PRICE', 'NAV', 'BID_ASK_SPREAD', 'TRADING_VOLUME'))
    current = _entry_session_verified(key, price, volume, spread, nav, now=now,
                                      candidate=candidate, calculated=calculated)
    entry_inputs = {'current': current, 'verified': current,
        'reason': None if current else 'ALIGNED_VERIFIED_MARKET_AND_NAV_SESSION_REQUIRED',
        'premium_percent': str((price.value/nav.nav-1)*100) if current else None,
        'momentum_percent': str(calculated.get(EtfMetricId.MOMENTUM_50_200).value) if calculated.get(EtfMetricId.MOMENTUM_50_200).value is not None else None,
        'return_1m_percent': str(calculated.get(EtfMetricId.RETURN_1M).value) if calculated.get(EtfMetricId.RETURN_1M).value is not None else None,
        'spread_percent': metrics['bid_ask_spread']['value'], 'volume': metrics['trading_volume']['value']}
    for name, enum in [('volatility', EtfMetricId.ANNUALIZED_VOLATILITY), ('drawdown', EtfMetricId.MAX_DRAWDOWN)]:
        value = calculated.get(enum)
        metric(name, value.value if value else None, 'PERCENT', value is not None and not value.insufficient_history
               and 0 <= (now.date()-value.as_of_date).days <= _DERIVED_METRIC_MAX_AGE_DAYS,
               value.reason if value and value.insufficient_history else 'STALE_OR_UNAVAILABLE_PRICE_HISTORY')
    snapshots = store.etf_holdings(key)
    coverage = holdings_coverage(snapshots[0] if snapshots else None, now=now)
    metric('concentration', coverage.get('top10_concentration'), 'PERCENT', coverage['status'] == 'VALID',
           coverage.get('reason') or 'INCOMPLETE_VERIFIED_HOLDINGS')
    metric('data_completeness', (candidate.get('rule_engine_result') or {}).get('data_completeness'), 'FRACTION')
    return {**fields, 'screening_metrics': metrics, 'entry_inputs': entry_inputs, 'history_observations': len(bars),
            'history_quality': history_diagnostics,
            'holdings_coverage': str(coverage['coverage']) if coverage['coverage'] is not None else None}


def recommendation_evidence_gaps(candidates):
    """Diagnostic counts for research opportunities, not an alternate buy gate.

    Each missing count is independently assessed against the source-backed
    screening metrics. The categories overlap: one ETF may lack all of them.
    The authoritative scoring and peer-ranking decisions remain unchanged.
    """
    selected = [c for c in candidates if c.get('recommendation') in
                ('OPPORTUNITY', 'STRONG_OPPORTUNITY') and c.get('rank') is not None]
    required = REQUIRED_RECOMMENDATION_METRICS
    missing = {}
    for metric in required:
        missing[metric] = sum(
            (c.get('screening_metrics') or {}).get(metric, {}).get('quality') != 'VALID'
            or (c.get('screening_metrics') or {}).get(metric, {}).get('value') is None
            for c in selected)
    entry_ready = sum((c.get('entry_assessment') or {}).get('classification') in
                      ('FAVORABLE_ENTRY_ZONE', 'NEUTRAL_WAIT', 'EXTENDED_AVOID_CHASING')
                      for c in selected)
    return {
        'screening_opportunities': len(selected),
        'missing_verified_metrics': missing,
        'entry_assessed': entry_ready,
        'entry_favorable': sum((c.get('entry_assessment') or {}).get('classification') == 'FAVORABLE_ENTRY_ZONE' for c in selected),
        'entry_insufficient_evidence': len(selected) - entry_ready,
    }


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
