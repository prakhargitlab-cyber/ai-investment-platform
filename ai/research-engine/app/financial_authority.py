"""Financial authority coverage, independent of availability and freshness."""
from collections.abc import Sequence

from app.fact_precedence import FinancialFact, FactSourceTier, SUPPORTED_FINANCIAL_SOURCE_TIERS
from app.models import CompanyResearchProfile, SourceMode


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
    # Quarterly readiness requires comparable quarters in one reporting basis.
    # A solitary official result cannot prove that history is complete.
    if not any(group[0] == "QUARTERLY" and len(periods) >= 2 for group, periods in available.items()):
        return True
    for group, periods in available.items():
        for period in sorted(periods, reverse=True)[:4]:
            # Keep the repository's income-statement completeness bar. Other
            # statements use explicit observed metrics, never invented values.
            required = {"revenue", "pat"} if group[0] != "AS_AT" else periods[period]
            if not required <= official.get(group, {}).get(period, set()):
                return True
    return False
