"""Financial authority coverage, independent of availability and freshness."""
from collections.abc import Sequence
from datetime import date

from app.fact_precedence import FinancialFact, FinancialFactKey, FactSourceTier, SUPPORTED_FINANCIAL_SOURCE_TIERS
from app.models import CompanyResearchProfile, SourceMode

ANNUAL_CAGR_MIN_PERIODS = 3  # matches stock_rule_engine._series_cagr
QUARTERLY_MIN_PERIODS = 4  # TTM history targets; distinct from source authority.


def annual_income_history_gaps(facts: Sequence[FinancialFact]) -> frozenset[FinancialFactKey]:
    """Acquisition targets, not invented facts, anchored to a reported fiscal end."""
    from app.normalization import normalize_financial_amount
    from decimal import Decimal
    groups = {}
    aliases = {'total_revenue': 'revenue', 'net_income': 'pat', 'net_profit': 'pat'}
    for fact in facts:
        metric = aliases.get(fact.key.metric, fact.key.metric)
        if (fact.source_mode != SourceMode.REAL or fact.source_tier not in SUPPORTED_FINANCIAL_SOURCE_TIERS
                or fact.key.period_type != 'ANNUAL' or metric not in {'revenue', 'pat'}
                or fact.key.reporting_basis not in {'STANDALONE', 'CONSOLIDATED'} or fact.value.value is None):
            continue
        try:
            period = date.fromisoformat(fact.key.period_end or '')
        except ValueError:
            continue
        unit = normalize_financial_amount(Decimal(0), fact.value.unit)[1]
        if not unit or len(unit) != 3:
            continue
        group = (fact.key.instrument_id, fact.key.reporting_basis, unit, period.month, period.day)
        groups.setdefault(group, set()).add((metric, period))
    if not groups:
        return frozenset()
    # Select one coherent current basis, like the existing scorer. A complete
    # consolidated series must not trigger acquisition of unused standalone data.
    group = max(groups, key=lambda g: (max(p for _, p in groups[g]), g[1] == 'CONSOLIDATED', len(groups[g])))
    present = groups[group]
    latest = max(period for _, period in present)
    missing = set()
    for offset in range(ANNUAL_CAGR_MIN_PERIODS):
        try:
            period = latest.replace(year=latest.year - offset)
        except ValueError:  # leap-day fiscal end
            period = latest.replace(year=latest.year - offset, day=28)
        for metric in ('revenue', 'pat'):
            if (metric, period) not in present:
                missing.add(FinancialFactKey(group[0], metric, period.isoformat(), 'ANNUAL', group[1]))
    return frozenset(missing)


def _quarter_starts_from(period_end: date) -> list[date]:
    """The four quarter-end dates that precede (and include) ``period_end``
    on the same fiscal calendar.  For a March-31 fiscal year-end the quarter
    ends are 31-Mar, 30-Jun, 30-Sep, 31-Dec; for any other year-end the same
    3-month cadence is applied relative to that end.  The returned list is
    ordered most-recent first.
    """
    ends = []
    for offset in range(QUARTERLY_MIN_PERIODS):
        months_back = offset * 3
        if period_end.month <= months_back:
            year = period_end.year - 1
            month = period_end.month + 12 - months_back
        else:
            year = period_end.year
            month = period_end.month - months_back
        # Quarter-end day: clamp to the last day of the resulting month to
        # handle 31-day months correctly (e.g. 31-Mar -> 30-Jun -> 30-Sep -> 31-Dec).
        month = ((month - 1) % 12) + 1
        if month == 2:
            day = 29 if (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)) else 28
        elif month in {4, 6, 9, 11}:
            day = 30
        else:
            day = 31
        ends.append(date(year, month, day))
    return ends


def quarterly_income_history_gaps(facts: Sequence[FinancialFact]) -> frozenset[FinancialFactKey]:
    """Acquisition targets for the last ``QUARTERLY_MIN_PERIODS`` completed
    quarters on the instrument's fiscal calendar, anchored to the latest
    *reported* quarter period-end.

    Mirrors :func:`annual_income_history_gaps`: selects one coherent basis
    (reporting basis + unit) from the persisted facts, derives the four
    quarter-end dates on that fiscal calendar, and reports only the
    (revenue/pat, quarter) pairs that are not yet present -- never invents
    future or unpublished periods.

    Unlike the annual variant, quarterly period-ends differ in month/day
    (e.g. 31-Mar, 30-Jun, 30-Sep, 31-Dec for a March year-end), so the
    fiscal calendar is anchored on the single latest quarter-end rather than
    a shared month/day pattern.
    """
    from app.normalization import normalize_financial_amount
    from decimal import Decimal
    groups = {}
    aliases = {'total_revenue': 'revenue', 'net_income': 'pat', 'net_profit': 'pat'}
    for fact in facts:
        metric = aliases.get(fact.key.metric, fact.key.metric)
        if (fact.source_mode != SourceMode.REAL or fact.source_tier not in SUPPORTED_FINANCIAL_SOURCE_TIERS
                or fact.key.period_type != 'QUARTERLY' or metric not in {'revenue', 'pat'}
                or fact.key.reporting_basis not in {'STANDALONE', 'CONSOLIDATED'} or fact.value.value is None):
            continue
        try:
            period = date.fromisoformat(fact.key.period_end or '')
        except ValueError:
            continue
        unit = normalize_financial_amount(Decimal(0), fact.value.unit)[1]
        if not unit or len(unit) != 3:
            continue
        # Quarterly period-ends differ in month/day, so group by basis+unit
        # only; the fiscal calendar is anchored on the latest period-end.
        group = (fact.key.instrument_id, fact.key.reporting_basis, unit)
        groups.setdefault(group, set()).add((metric, period))
    if not groups:
        return frozenset()
    # Select one coherent current basis, like the annual scorer: the group
    # whose latest period is most recent, preferring CONSOLIDATED, then larger.
    group = max(groups, key=lambda g: (max(p for _, p in groups[g]), g[1] == 'CONSOLIDATED', len(groups[g])))
    present = groups[group]
    latest = max(period for _, period in present)
    required_periods = _quarter_starts_from(latest)
    missing = set()
    for period in required_periods:
        for metric in ('revenue', 'pat'):
            if (metric, period) not in present:
                missing.add(FinancialFactKey(group[0], metric, period.isoformat(), 'QUARTERLY', group[1]))
    return frozenset(missing)


def authoritative_financial_upgrade_required(
    profile: CompanyResearchProfile,
    persisted_facts: Sequence[FinancialFact],
    readiness=None,
) -> bool:
    # The canonical profile adapter admits NSE mappings only after VERIFIED
    # validation. Never derive this identity from ticker, name, or other feeds.
    if (profile.country.upper() not in {"IN", "IND", "INDIA"}
            or profile.exchange.upper() not in {"NSE", "XNSE"}
            or not profile.provider_instrument_ids.get("NSE")):
        return False
    if readiness is not None and readiness.applicability == "NOT_APPLICABLE":
        return False
    if not any(f.key.instrument_id == profile.instrument_id and f.source_mode == SourceMode.REAL
               and f.source_tier in SUPPORTED_FINANCIAL_SOURCE_TIERS and f.value.value is not None
               for f in persisted_facts):
        return True
    # Complete income statements do not imply complete growth/quality inputs
    # (for example, EBIT needed for ROCE). The repository applies authority
    # cooldowns before this coverage check; readiness remains the authority
    # for which required inputs are still missing.
    if (readiness is not None
            and readiness.requirement_id in {"BUSINESS_QUALITY_FACTS", "GROWTH_FACTS"}
            and readiness.missing_input_ids):
        return True
    if annual_income_history_gaps(persisted_facts):
        return True
    available = {}
    official = {}
    aliases = {"total_revenue": "revenue", "net_income": "pat", "net_profit": "pat"}
    for fact in persisted_facts:
        key = fact.key
        if (key.instrument_id != profile.instrument_id or fact.source_mode != SourceMode.REAL
                or fact.source_tier not in SUPPORTED_FINANCIAL_SOURCE_TIERS
                or key.period_type not in {"QUARTERLY", "ANNUAL", "AS_AT"}
                or not key.period_end or fact.value.value is None):
            continue
        group = (key.period_type, key.reporting_basis)
        metric = aliases.get(key.metric, key.metric)
        available.setdefault(group, {}).setdefault(key.period_end, set()).add(metric)
        if fact.source_tier == FactSourceTier.OFFICIAL_NSE and fact.source_provider == "NSE":
            official.setdefault(group, {}).setdefault(key.period_end, set()).add(metric)
    # Authority upgrades compare existing evidence with official coverage.
    # Two periods support the limited quarterly assessment; TTM/YoY history
    # gaps belong to the requirement-specific acquisition plan.
    quarter_periods = {}
    for group, periods in available.items():
        if group[0] != "QUARTERLY":
            continue
        quarter_periods.setdefault(group[1], set()).update(periods.keys())
    for basis, period_ends in quarter_periods.items():
        if len(period_ends) < 2:
            return True
    for group, periods in available.items():
        for period in sorted(periods, reverse=True)[:4]:
            # Keep the repository's income-statement completeness bar. Other
            # statements use explicit observed metrics, never invented values.
            required = {"revenue", "pat"} if group[0] != "AS_AT" else periods[period]
            if not required <= official.get(group, {}).get(period, set()):
                return True
    return False
