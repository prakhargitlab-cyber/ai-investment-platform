"""Finite evidence requirements for the existing financial rules, not download quotas.

Periods are anchored to published issuer statements. UNKNOWN-basis secondary
facts remain useful to their callers but never cover an official statement gap.
"""
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import calendar

from app.fact_precedence import FinancialFact, FactSourceTier
from app.models import SourceMode
from app.normalization import normalize_financial_amount

ANNUAL_DEFAULT = 3
ANNUAL_MAXIMUM = 5
QUARTERLY_MAXIMUM = 8
ALIASES = {
    "total_revenue": "revenue", "net_income": "pat", "net_profit": "pat",
    "profit_before_tax": "pbt", "finance_cost": "finance_costs", "interest_expense": "finance_costs",
    "equity": "total_equity", "cash_and_equivalents": "cash_and_cash_equivalents",
    "operating_cash_flow": "cash_flow_from_operating_activities",
    "total_debt": "debt_or_borrowings", "borrowings": "debt_or_borrowings",
}


def period_date(value):
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


def shift_months(end: date, months: int) -> date:
    year, month0 = divmod(end.year * 12 + end.month - 1 + months, 12)
    month = month0 + 1
    last = calendar.monthrange(year, month)[1]
    day = last if end.day == calendar.monthrange(end.year, end.month)[1] else min(end.day, last)
    return date(year, month, day)


def official_amount(fact):
    if (fact.source_mode != SourceMode.REAL or fact.source_provider != "NSE"
            or fact.source_tier != FactSourceTier.OFFICIAL_NSE
            or fact.key.reporting_basis not in {"CONSOLIDATED", "STANDALONE"}):
        return None
    try:
        value, unit = normalize_financial_amount(Decimal(str(fact.value.value)), fact.value.unit)
        if value is not None and value.is_finite():
            return value, unit
    except (InvalidOperation, ValueError, TypeError):
        pass
    return None


@dataclass(frozen=True)
class HistoryDemand:
    calculation: str
    frequency: str
    periods: int

    def validate(self):
        # These are existing scorer operations. No implicit extension for a
        # missing ratio, an unsupported metric, or a provider's default window.
        allowed = {"ANNUAL": {"REVENUE_CAGR", "EARNINGS_CAGR", "GROWTH_CONSISTENCY"},
                   "QUARTERLY": {"TTM", "REVENUE_YOY", "EARNINGS_YOY", "EPS_YOY", "GROWTH_CONSISTENCY"}}
        maximum = ANNUAL_MAXIMUM if self.frequency == "ANNUAL" else QUARTERLY_MAXIMUM
        if self.calculation not in allowed.get(self.frequency, ()) or not 1 <= self.periods <= maximum:
            raise ValueError("UNJUSTIFIED_OR_UNBOUNDED_FINANCIAL_HISTORY")


@dataclass(frozen=True)
class EvidenceNeed:
    requirement_id: str
    input_id: str
    metric: str
    frequency: str
    period_end: str
    reporting_basis: str
    unit: str
    priority: int = 1
    authority: str = "NSE"
    acquisition_cost: str = "STRUCTURED_BEFORE_PDF"
    # Each tuple is a complete alternative set of deterministic inputs.
    alternatives: tuple[tuple[str, ...], ...] = ()

    def covered(self, facts) -> bool:
        metrics = set()
        for fact in facts:
            amount = official_amount(fact)
            if (amount is None or fact.key.period_type != self.frequency
                    or fact.key.reporting_basis != self.reporting_basis
                    or period_date(fact.key.period_end) != period_date(self.period_end)):
                continue
            unit = amount[1]
            if unit == self.unit or (self.unit.endswith(" per share") and unit == self.unit.replace(" per share", "/share")):
                metrics.add(ALIASES.get(fact.key.metric, fact.key.metric))
        return any(set(option) <= metrics for option in (self.alternatives or ((self.metric,),)))


@dataclass(frozen=True)
class FinancialEvidencePlan:
    needs: tuple[EvidenceNeed, ...]
    annual_periods: tuple[str, ...]
    quarterly_periods: tuple[str, ...]
    reporting_basis: str
    unresolved: tuple[str, ...] = ()
    extensions: tuple[HistoryDemand, ...] = ()

    def missing(self, facts) -> tuple[EvidenceNeed, ...]:
        return tuple(need for need in self.needs if not need.covered(facts))

    def accepts_period(self, fact) -> bool:
        end = period_date(fact.key.period_end)
        periods = self.quarterly_periods if fact.key.period_type == "QUARTERLY" else self.annual_periods
        return (fact.key.reporting_basis == self.reporting_basis
                and (not periods or end is not None and end.isoformat() in periods))


def build_financial_evidence_plan(profile, facts, missing, *, as_of=None, history_demands=(),
                                  published_periods=(), preferred_basis=None):
    """Translate unresolved *existing* readiness inputs into statement keys.

    Three annual points support the current CAGR rule; four quarters support
    comparable/TTM history and the fifth is the latest quarter's exact YoY
    comparison. An ROCE-only request asks for one matching annual denominator,
    not three/five unrelated reports. Older generated dates are targets only;
    discovery must still prove a published filing contains them.
    """
    as_of = as_of or datetime.now(timezone.utc)
    cutoff = as_of.date() if isinstance(as_of, datetime) else as_of
    valid = [f for f in facts if f.source_mode == SourceMode.REAL
             and period_date(f.key.period_end) is not None and period_date(f.key.period_end) <= cutoff
             and (f.value.published_at is None or f.value.published_at.date() <= cutoff)]
    bases = {f.key.reporting_basis for f in valid if official_amount(f) is not None}
    basis = "CONSOLIDATED" if "CONSOLIDATED" in bases else "STANDALONE" if "STANDALONE" in bases else (preferred_basis or "CONSOLIDATED")
    # Calendar identity is distinct from authority: a reported Yahoo fiscal
    # end can anchor discovery, without supplying a single official fact.
    def latest(kind):
        preferred = [period_date(f.key.period_end) for f in valid
                     if f.key.period_type == kind and f.key.reporting_basis == basis]
        other = [period_date(f.key.period_end) for f in valid if f.key.period_type == kind]
        return max(preferred or other, default=None)
    annual, quarter = latest("ANNUAL"), latest("QUARTERLY")
    reported = [period_date(p) for p in published_periods if period_date(p) and period_date(p) <= cutoff]
    if annual:
        # The fiscal end comes from a statement; the index only advances it to
        # an actually published period on that same issuer calendar.
        annual = max([annual] + [p for p in reported if (p.month, p.day) == (annual.month, annual.day)])
    if quarter:
        quarter = max([quarter] + [p for p in reported if (p.month-quarter.month) % 3 == 0])
    annual_count, quarter_count = ANNUAL_DEFAULT, 4
    inputs = {item for values in missing.values() for item in values}
    if inputs & {"QUARTERLY_YOY_QOQ_TRENDS", "COMPARABLE_QUARTERS", "QUARTERLY_EPS"}:
        quarter_count = 5  # latest Q, three TTM constituents, latest Q one year ago
    for demand in history_demands:
        demand.validate()
        if demand.frequency == "ANNUAL":
            annual_count = max(annual_count, demand.periods)
        else:
            quarter_count = max(quarter_count, demand.periods)
    annual_periods = tuple(shift_months(annual, -12 * i).isoformat() for i in range(annual_count)) if annual else ()
    quarter_periods = tuple(shift_months(quarter, -3 * i).isoformat() for i in range(quarter_count)) if quarter else ()
    needs, unresolved = [], set()
    currency = getattr(profile, "currency", None) or "INR"

    def add(requirement, input_id, metrics, frequency, periods, priority=1):
        if not periods:
            unresolved.add("PUBLISHED_" + frequency + "_ANCHOR_REQUIRED")
        for end in periods:
            for metric in metrics:
                alternatives = ((("ebit",), ("pbt", "finance_costs")) if metric == "ebit" else
                                (("debt_or_borrowings",), ("current_borrowings", "noncurrent_borrowings"))
                                if metric == "debt_or_borrowings" else ())
                needs.append(EvidenceNeed(requirement, input_id, metric, frequency, end, basis,
                                          currency + " per share" if metric == "eps" else currency,
                                          priority, alternatives=alternatives))

    for requirement, input_ids in sorted(missing.items()):
        for input_id in sorted(input_ids):
            if input_id in {"ANNUAL_CAGR_INPUTS", "REVENUE_HISTORY", "EARNINGS_HISTORY", "PROFITABILITY_HISTORY"}:
                metrics = ("revenue",) if input_id == "REVENUE_HISTORY" else ("pat",) if input_id in {"EARNINGS_HISTORY", "PROFITABILITY_HISTORY"} else ("revenue", "pat")
                periods = annual_periods if input_id == "ANNUAL_CAGR_INPUTS" else annual_periods[:2]
                add(requirement, input_id, metrics, "ANNUAL", periods)
            elif input_id in {"QUARTERLY_YOY_QOQ_TRENDS", "COMPARABLE_QUARTERS", "LATEST_QUARTERLY_RESULT", "QUARTERLY_REVENUE", "QUARTERLY_PAT", "QUARTERLY_EPS"}:
                metrics = {"QUARTERLY_REVENUE": ("revenue",), "QUARTERLY_PAT": ("pat",), "QUARTERLY_EPS": ("eps",)}.get(input_id, ("revenue", "pat"))
                periods = (quarter_periods[:1] if input_id == "LATEST_QUARTERLY_RESULT" else
                           (quarter_periods[0], quarter_periods[4]) if input_id == "QUARTERLY_EPS" and len(quarter_periods) >= 5
                           else quarter_periods)
                add(requirement, input_id, metrics, "QUARTERLY", periods)
            elif input_id in {"ROCE", "ROE"}:
                add(requirement, input_id, ("ebit",) if input_id == "ROCE" else ("pat",), "ANNUAL", annual_periods[:1])
                add(requirement, input_id, ("total_assets", "current_liabilities") if input_id == "ROCE" else ("total_equity",), "AS_AT", annual_periods[:1])
            elif input_id == "MARGINS":
                add(requirement, input_id, ("pat", "revenue"), "ANNUAL", annual_periods[:1])
            elif input_id == "CASH_CONVERSION_OR_FCF_QUALITY":
                add(requirement, input_id, ("cash_flow_from_operating_activities",), "ANNUAL", annual_periods[:1])
            elif input_id in {"DEBT", "EQUITY", "CASH", "LIQUIDITY_CURRENT_RATIO_INPUTS"}:
                metrics = {"DEBT": ("debt_or_borrowings",), "EQUITY": ("total_equity",), "CASH": ("cash_and_cash_equivalents",), "LIQUIDITY_CURRENT_RATIO_INPUTS": ("current_assets", "current_liabilities")}[input_id]
                add(requirement, input_id, metrics, "AS_AT", annual_periods[:1])
            elif input_id == "INTEREST_COVERAGE_INPUTS":
                add(requirement, input_id, ("ebitda", "finance_costs"), "ANNUAL", annual_periods[:1], 2)
            elif input_id in {"QUARTERLY_EBITDA_OR_OPERATING_PROFIT", "QUARTERLY_MARGINS"}:
                # The scorer uses operating income for quarterly margin trend;
                # PAT/EBITDA are not relabelled as operating income.
                add(requirement, input_id, ("operating_income", "revenue"), "QUARTERLY", quarter_periods[:2], 2)
            else:
                unresolved.add("UNSUPPORTED_INPUT:" + input_id)
    return FinancialEvidencePlan(tuple(dict.fromkeys(needs)), annual_periods, quarter_periods,
                                 basis, tuple(sorted(unresolved)), tuple(history_demands))


def potential_coverage(row, missing, *, fiscal_end=None):
    """Metadata is a cost estimate, never proof that facts exist in a filing."""
    try:
        end = datetime.strptime(row["qe_Date"], "%d-%b-%Y").date()
    except (ValueError, TypeError, KeyError):
        return ()
    basis = str(row.get("consolidated", "")).upper()
    annual = fiscal_end is not None and (end.month, end.day) == (fiscal_end.month, fiscal_end.day)
    covered = []
    for need in missing:
        target = period_date(need.period_end)
        if basis != need.reporting_basis or target is None or target > end:
            continue
        # Integrated statements may include previous-year comparatives. Do
        # not assume these are present; reassess actual keys after each parse.
        if need.frequency == "QUARTERLY":
            relevant = target in {end, shift_months(end, -3), shift_months(end, -12)}
        else:
            relevant = annual and target in {end, shift_months(end, -12)}
        if relevant:
            covered.append(need)
    return tuple(covered)


def filing_rank(row, missing, *, fiscal_end=None):
    coverage = potential_coverage(row, missing, fiscal_end=fiscal_end)
    if not coverage:
        return None
    end = datetime.strptime(row["qe_Date"], "%d-%b-%Y").date()
    exact = sum(period_date(n.period_end) == end for n in coverage)
    cost = 1_500_000
    text = str(row.get("xbrlFileSize") or "").upper().split()
    if len(text) == 2 and text[1] in {"BYTES", "KB", "MB"}:
        try:
            cost = max(1, int(float(text[0]) * {"BYTES": 1, "KB": 1024, "MB": 1024 ** 2}[text[1]]))
        except (ValueError, OverflowError):
            pass
    # Authority and basis are eligibility gates; prioritize high coverage and
    # exact matches ahead of recency. Publication breaks otherwise equal ties.
    publication = datetime.strptime(row["broadcast_Date"], "%d-%b-%Y %H:%M:%S")
    return (sum(3 - n.priority for n in coverage), exact, -cost, publication, row["xbrl"])
