"""Read-only financial gap selection. Persistence retains the sole authority policy."""
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace

from app.fact_precedence import FinancialFact, merge_fact
from app.financial_authority import annual_income_history_gaps, quarterly_income_history_gaps
from app.models import SourceMode
from app.normalization import normalize_financial_amount
from app.research_readiness import ResearchReadinessService, ResearchRequirementRegistry
from app.research_readiness_runtime import RepositoryResearchReadinessAdapter


FINANCIAL_GAP_REQUIREMENTS = frozenset({
    "QUARTERLY_FINANCIALS", "GROWTH_FACTS", "BUSINESS_QUALITY_FACTS", "BALANCE_SHEET_FACTS",
})
_REVENUE = {"revenue", "total_revenue"}
_EARNINGS = {"pat", "net_income", "net_profit", "eps"}
_MARGINS = {"operating_margin", "profit_margin", "ebitda_margin"}
_CASH_FLOW = {"operating_cash_flow", "free_cash_flow"}
# Only metrics supported by the existing Yahoo normalized financial wire model.
# These are input contributors, not assertions that one fact completes a series.
_INPUT_METRICS = {
    "REVENUE_HISTORY": _REVENUE,
    "EARNINGS_HISTORY": _EARNINGS,
    "QUARTERLY_YOY_QOQ_TRENDS": _REVENUE | _EARNINGS,
    "ANNUAL_CAGR_INPUTS": _REVENUE | _EARNINGS,
    "PROFITABILITY_HISTORY": _EARNINGS - {"eps"} | {"roe", "roce", "operating_margin", "profit_margin", "operating_cash_flow"},
    "ROE": {"roe"}, "ROCE": {"roce"}, "MARGINS": _MARGINS,
    "CASH_CONVERSION_OR_FCF_QUALITY": _CASH_FLOW,
    "DEBT": {"total_debt", "debt_or_borrowings"}, "EQUITY": {"equity"},
    "CASH": {"total_cash", "cash_and_equivalents"},
    "INTEREST_COVERAGE_INPUTS": {"interest_expense", "finance_cost", "ebit", "operating_profit"},
    "LIQUIDITY_CURRENT_RATIO_INPUTS": {"current_assets", "current_liabilities"},
    "LATEST_QUARTERLY_RESULT": _REVENUE | _EARNINGS,
    "COMPARABLE_QUARTERS": _REVENUE | _EARNINGS,
    "QUARTERLY_REVENUE": _REVENUE, "QUARTERLY_PAT": _EARNINGS - {"eps"},
    "QUARTERLY_EPS": {"eps"},
    "QUARTERLY_EBITDA_OR_OPERATING_PROFIT": {"ebitda", "operating_profit", "operating_income"},
    "QUARTERLY_MARGINS": _MARGINS,
}
_QUARTER_INPUTS = {key for key in _INPUT_METRICS if key.startswith("QUARTERLY_")} | {
    "LATEST_QUARTERLY_RESULT", "COMPARABLE_QUARTERS",
}


def required_quarterly_periods(facts: Sequence[FinancialFact], as_of=None) -> tuple[str, ...]:
    """Compute the last ``QUARTERLY_MIN_PERIODS`` completed quarterly
    period-end dates on the instrument's fiscal calendar, anchored to the
    latest *reported* quarter.

    Returns period-end ISO strings ordered most-recent first.  An unpublished
    expected period is never included -- the calculation starts from the most
    recently persisted quarter-end and walks backwards by three months, so a
    not-yet-reported latest quarter cannot be silently treated as completed.
    """
    from app.financial_authority import _quarter_starts_from, QUARTERLY_MIN_PERIODS
    from datetime import date as _date
    aliases = {'total_revenue': 'revenue', 'net_income': 'pat', 'net_profit': 'pat'}
    latest = None
    for fact in facts:
        metric = aliases.get(fact.key.metric, fact.key.metric)
        if (fact.source_mode != SourceMode.REAL or metric not in {'revenue', 'pat'}
                or fact.key.period_type != 'QUARTERLY'
                or fact.key.reporting_basis not in {'STANDALONE', 'CONSOLIDATED'}
                or fact.value.value is None):
            continue
        try:
            period = _date.fromisoformat(fact.key.period_end or '')
        except ValueError:
            continue
        if latest is None or period > latest:
            latest = period
    if latest is None:
        return ()
    return tuple(p.isoformat() for p in _quarter_starts_from(latest)[:QUARTERLY_MIN_PERIODS])


def _numeric(value) -> bool:
    try:
        return value is not None and Decimal(str(value)).is_finite()
    except (InvalidOperation, ValueError, TypeError):
        return False


@dataclass(frozen=True)
class FinancialGapState:
    readiness: object
    facts: tuple[FinancialFact, ...]
    missing: dict[str, frozenset[str]]

    @property
    def missing_annual_fact_keys(self):
        return annual_income_history_gaps(self.facts) if 'ANNUAL_CAGR_INPUTS' in self.missing.get('GROWTH_FACTS', ()) else frozenset()

    @property
    def missing_quarterly_fact_keys(self):
        quarter_inputs = {'QUARTERLY_YOY_QOQ_TRENDS', 'COMPARABLE_QUARTERS',
                          'LATEST_QUARTERLY_RESULT', 'QUARTERLY_REVENUE', 'QUARTERLY_PAT',
                          'QUARTERLY_EPS', 'QUARTERLY_EBITDA_OR_OPERATING_PROFIT', 'QUARTERLY_MARGINS'}
        quarterly_missing = self.missing.get('QUARTERLY_FINANCIALS', ())
        growth_missing = self.missing.get('GROWTH_FACTS', ())
        if not (quarter_inputs & quarterly_missing) and not ('QUARTERLY_YOY_QOQ_TRENDS' in growth_missing):
            return frozenset()
        return quarterly_income_history_gaps(self.facts)

    def supported_inputs(self, requirement_id):
        return self.missing[requirement_id] & _INPUT_METRICS.keys()

    def accepts(self, requirement_id: str, fact: FinancialFact, currency: str,
                diagnostics: list[str] | None = None) -> bool:
        """Select unresolved contributors without conflating keys, units or series.

        ``diagnostics``, when provided, receives one short, sanitized reason
        code (never the fact's own metric/value/period) for a rejection, so a
        caller can tell "the provider genuinely returned nothing usable" apart
        from "something here was rejected and here is which check" without
        logging or persisting raw financial figures. Optional and additive:
        omitting it leaves this method's behavior and return value identical
        to before this diagnostic capability existed.
        """
        def reject(code: str) -> bool:
            if diagnostics is not None:
                diagnostics.append(code)
            return False

        key = fact.key
        if requirement_id != "BALANCE_SHEET_FACTS" and key.period_type == "AS_AT":
            return reject("AS_AT_PERIOD_NOT_SUPPORTED_FOR_REQUIREMENT")
        # "UNKNOWN" is a deliberate, isolated third reporting_basis category
        # (see app/structured_market.py's _normalize_statement_facts and
        # ai/yahoo-finance-mcp/yahoo_mcp_server/contracts.py's RawFact
        # default) -- Yahoo's statement-derived facts (revenue/PAT/EPS/etc,
        # exactly what GROWTH_FACTS needs) never carry a standalone/
        # consolidated distinction, by design, and are tagged UNKNOWN rather
        # than guessing one. Rejecting UNKNOWN outright here blanket-rejected
        # every such fact (confirmed: 76/76 GROWTH_FACTS facts rejected with
        # this exact code for one real NSE instrument). UNKNOWN is never
        # merged with STANDALONE/CONSOLIDATED: the `bases` check below still
        # excludes it from real-basis series membership, and the identity/
        # unit-consistency checks below compare reporting_basis for exact
        # equality, so UNKNOWN facts only ever group with other UNKNOWN
        # facts, never with NSE's STANDALONE/CONSOLIDATED filings.
        if (key.reporting_basis not in {"STANDALONE", "CONSOLIDATED", "UNKNOWN"}
                or key.period_type not in {"QUARTERLY", "ANNUAL", "AS_AT"}
                or not _numeric(fact.value.value)):
            return reject("INVALID_BASIS_PERIOD_TYPE_OR_NON_NUMERIC_VALUE")
        try:
            period = date.fromisoformat(key.period_end or "")
        except ValueError:
            return reject("UNPARSEABLE_PERIOD_END")
        if key.period_end != period.isoformat() or period > self.readiness.generated_at.date():
            return reject("MALFORMED_OR_FUTURE_PERIOD_END")
        relevant = {input_id for input_id in self.supported_inputs(requirement_id)
                    if key.metric in _INPUT_METRICS[input_id]
                    and (input_id not in _QUARTER_INPUTS or key.period_type == "QUARTERLY")
                    and (input_id != "ANNUAL_CAGR_INPUTS" or key.period_type == "ANNUAL")}
        if not relevant:
            return reject("METRIC_NOT_MAPPED_TO_A_MISSING_INPUT")
        unit = normalize_financial_amount(Decimal(0), fact.value.unit)[1]
        expected_units = ({f"{currency}/share", f"{currency} per share"} if key.metric == "eps" else
                          {"%", "PERCENT", "percent", "ratio"} if key.metric in _MARGINS | {"roe", "roce"}
                          else {currency})
        if unit not in expected_units:
            return reject("UNIT_DID_NOT_MATCH_EXPECTED_UNIT_FOR_METRIC")
        existing = [item for item in self.facts if item.source_mode == SourceMode.REAL
                    and _numeric(item.value.value)]
        # Unit is not part of FinancialFactKey. Check it separately, then use
        # the existing authority policy (including zero), not a second ranking.
        for item in existing:
            if item.key == key and (normalize_financial_amount(Decimal(0), item.value.unit)[1] != unit
                                   or merge_fact(item, fact) is not fact):
                return reject("SUPERSEDED_BY_OR_CONFLICTS_WITH_EXISTING_FACT")
        series = [item for item in existing if item.key.period_type == key.period_type]
        bases = {item.key.reporting_basis for item in series
                 if item.key.reporting_basis in {"STANDALONE", "CONSOLIDATED"}}
        if bases and key.reporting_basis not in bases:
            return reject("REPORTING_BASIS_INCONSISTENT_WITH_EXISTING_SERIES")
        units = {normalize_financial_amount(Decimal(0), item.value.unit)[1] for item in series
                 if item.key.reporting_basis == key.reporting_basis and item.key.metric == key.metric}
        if not (not units or unit in units):
            return reject("UNIT_INCONSISTENT_WITH_EXISTING_SERIES")
        return True


def load_financial_gap_state(repository, instrument_id, jurisdiction="INDIA") -> FinancialGapState:
    """Reuse readiness coverage, exclusions and freshness; never infer a new period."""
    registry = ResearchRequirementRegistry.default()
    snapshot = RepositoryResearchReadinessAdapter(repository).load_by_global_instrument_id(
        instrument_id, registry.requirements)
    service = ResearchReadinessService(SimpleNamespace(load_by_global_instrument_id=lambda *_: snapshot))
    readiness = service.assess(instrument_id, jurisdiction=jurisdiction, evidence_only=True)
    missing = {}
    for requirement in registry.requirements:
        if requirement.requirement_id not in FINANCIAL_GAP_REQUIREMENTS:
            continue
        row = readiness.for_requirement(requirement.requirement_id)
        policy = service.freshness_registry.get(requirement.freshness_policy_id)
        eligible = service.coverage_service.score_input_evidence(
            requirement, snapshot.evidence_for(requirement.requirement_id), policy, readiness.generated_at)
        fresh = service.coverage_service.covered_input_ids(
            requirement, tuple(item for item in eligible if item.complete and policy.is_fresh(item, readiness.generated_at)))
        missing[requirement.requirement_id] = frozenset(
            item.input_id for item in requirement.inputs
            if item.input_id not in fresh and item.input_id not in row.not_applicable_input_reasons)
    return FinancialGapState(readiness, tuple(repository.financial_facts_for(instrument_id)), missing)
