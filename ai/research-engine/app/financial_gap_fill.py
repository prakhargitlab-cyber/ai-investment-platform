"""Read-only financial gap selection. Persistence retains the sole authority policy."""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace

from app.fact_precedence import FinancialFact, merge_fact
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

    def supported_inputs(self, requirement_id):
        return self.missing[requirement_id] & _INPUT_METRICS.keys()

    def accepts(self, requirement_id: str, fact: FinancialFact, currency: str) -> bool:
        """Select unresolved contributors without conflating keys, units or series."""
        key = fact.key
        if requirement_id != "BALANCE_SHEET_FACTS" and key.period_type == "AS_AT":
            return False
        if (key.reporting_basis not in {"STANDALONE", "CONSOLIDATED"}
                or key.period_type not in {"QUARTERLY", "ANNUAL", "AS_AT"}
                or not _numeric(fact.value.value)):
            return False
        try:
            period = date.fromisoformat(key.period_end or "")
        except ValueError:
            return False
        if key.period_end != period.isoformat() or period > self.readiness.generated_at.date():
            return False
        relevant = {input_id for input_id in self.supported_inputs(requirement_id)
                    if key.metric in _INPUT_METRICS[input_id]
                    and (input_id not in _QUARTER_INPUTS or key.period_type == "QUARTERLY")
                    and (input_id != "ANNUAL_CAGR_INPUTS" or key.period_type == "ANNUAL")}
        if not relevant:
            return False
        unit = normalize_financial_amount(Decimal(0), fact.value.unit)[1]
        expected_units = ({f"{currency}/share", f"{currency} per share"} if key.metric == "eps" else
                          {"%", "PERCENT", "percent", "ratio"} if key.metric in _MARGINS | {"roe", "roce"}
                          else {currency})
        if unit not in expected_units:
            return False
        existing = [item for item in self.facts if item.source_mode == SourceMode.REAL
                    and _numeric(item.value.value)]
        # Unit is not part of FinancialFactKey. Check it separately, then use
        # the existing authority policy (including zero), not a second ranking.
        for item in existing:
            if item.key == key and (normalize_financial_amount(Decimal(0), item.value.unit)[1] != unit
                                   or merge_fact(item, fact) is not fact):
                return False
        series = [item for item in existing if item.key.period_type == key.period_type]
        bases = {item.key.reporting_basis for item in series
                 if item.key.reporting_basis in {"STANDALONE", "CONSOLIDATED"}}
        if bases and key.reporting_basis not in bases:
            return False
        units = {normalize_financial_amount(Decimal(0), item.value.unit)[1] for item in series
                 if item.key.reporting_basis == key.reporting_basis and item.key.metric == key.metric}
        return not units or unit in units


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
