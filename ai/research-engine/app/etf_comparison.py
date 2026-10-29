"""Evidence-only holdings overlap and benchmark tracking calculations."""
from decimal import Decimal
from statistics import stdev
from math import sqrt
import re

from app.etf_evidence import EtfAuthority, EtfMetric, etf_freshness


def holdings_coverage(snapshot, *, now, minimum=Decimal('95')):
    if snapshot is None or etf_freshness(snapshot, EtfMetric.UNDERLYING_HOLDINGS, now=now) != 'FRESH':
        return {'status': 'INSUFFICIENT_DATA', 'reason': 'MISSING_OR_STALE_HOLDINGS', 'coverage': None, 'weights': {}}
    if snapshot.provenance.authority == EtfAuthority.SECONDARY:
        return {'status': 'INSUFFICIENT_DATA', 'reason': 'UNVERIFIED_HOLDINGS', 'coverage': None, 'weights': {}}
    weights = {}
    for holding in snapshot.holdings:
        isin = holding.constituent_isin
        weight = holding.weight_percentage
        if not isin or not re.fullmatch(r'[A-Z]{2}[A-Z0-9]{9}[0-9]', isin) or weight is None:
            continue
        if isin in weights or not weight.is_finite() or not 0 <= weight <= 100:
            return {'status': 'INSUFFICIENT_DATA', 'reason': 'INVALID_OR_DUPLICATE_HOLDING', 'coverage': None, 'weights': {}}
        weights[isin] = weight
    coverage = sum(weights.values(), Decimal(0))
    valid = minimum <= coverage <= Decimal('100')
    return {'status': 'VALID' if valid else 'INSUFFICIENT_DATA', 'reason': None if valid else 'INCOMPLETE_HOLDINGS',
            'coverage': coverage, 'weights': weights, 'as_of': snapshot.as_of_date.isoformat(),
            'top10_concentration': sum(sorted(weights.values(), reverse=True)[:10]) if valid else None}


def holdings_overlap(left, right, *, now, max_date_gap_days=7):
    a, b = holdings_coverage(left, now=now), holdings_coverage(right, now=now)
    valid = a['status'] == b['status'] == 'VALID'
    aligned = valid and abs((left.as_of_date-right.as_of_date).days) <= max_date_gap_days
    value = sum((min(w, b['weights'][isin]) for isin, w in a['weights'].items()
                 if isin in b['weights']), Decimal(0)) if aligned else None
    return {'overlap_percent': value, 'status': 'VALID' if aligned else 'INSUFFICIENT_DATA',
            'left_coverage': a['coverage'], 'right_coverage': b['coverage'],
            'left_as_of': a.get('as_of'), 'right_as_of': b.get('as_of'),
            'formula': 'SUM_MIN_COMMON_ISIN_WEIGHT_PERCENT',
            'reason': None if aligned else 'MISSING_INCOMPLETE_OR_MISALIGNED_HOLDINGS'}


def tracking_metrics(fund, benchmark, *, expected_dates, minimum_coverage=Decimal('0.95'),
                     return_convention='PRICE_RETURN', benchmark_id=None, currency=None):
    """Dates must describe a trusted common session calendar, prices same currency.

    Daily simple returns; sample stdev of daily active returns * sqrt(252).
    Difference is cumulative fund minus benchmark return over the same endpoints.
    A missing date breaks the return pair; no forward fill or gap compression.
    """
    dates = sorted(set(expected_dates))
    pairs = []
    for start, end in zip(dates, dates[1:]):
        values = [fund.get(start), fund.get(end), benchmark.get(start), benchmark.get(end)]
        if all(v is not None and Decimal(v).is_finite() and v > 0 for v in values):
            pairs.append((fund[end]/fund[start]-1)-(benchmark[end]/benchmark[start]-1))
    coverage = Decimal(len(pairs))/Decimal(len(dates)-1) if len(dates) > 1 else Decimal(0)
    valid = len(pairs) >= 63 and coverage >= minimum_coverage and benchmark_id and currency
    endpoints = dates and all(series.get(dates[0], 0) > 0 and series.get(dates[-1], 0) > 0 for series in (fund, benchmark))
    return {'tracking_error_percent_pa': Decimal(str(stdev(map(float, pairs))*sqrt(252)*100)) if valid else None,
            'tracking_difference_percent': ((fund[dates[-1]]/fund[dates[0]])-(benchmark[dates[-1]]/benchmark[dates[0]]))*100 if valid and endpoints else None,
            'coverage': coverage, 'observations': len(pairs), 'benchmark_id': benchmark_id,
            'currency': currency, 'return_convention': return_convention,
            'status': 'VALID' if valid and endpoints else 'INSUFFICIENT_DATA'}
