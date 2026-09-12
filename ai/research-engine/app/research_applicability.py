"""Deterministic business applicability; no company names or provider outcomes."""
from dataclasses import dataclass, field
import re
from typing import Mapping


@dataclass(frozen=True)
class RequirementApplicability:
    state: str = "APPLICABLE"
    reason: str | None = None
    classification: str | None = None
    source: str | None = None
    excluded_inputs: Mapping[str, str] = field(default_factory=dict)


def classify_requirements(sector: str | None, industry: str | None, source: str | None):
    """Unknown/broad classifications never justify excluding a requirement.

    The order/capacity area concerns production and contracted delivery. General
    financial-company guidance remains available to growth/news/governance.
    Software backlog remains applicable even without industrial plant capacity.
    """
    text = re.sub(r"[^a-z0-9]+", " ", industry.casefold()).strip() if industry else ""
    classification = f"{sector or 'unknown'} / {industry or 'unknown'}"
    common = dict(classification=classification, source=source)
    result = {}
    if not text:
        result["ORDER_BOOK_CAPEX_GUIDANCE"] = RequirementApplicability(
            "UNKNOWN", "BUSINESS_CLASSIFICATION_UNAVAILABLE", **common)
        return result
    financial = any(term in text for term in (
        "stock exchanges", "financial exchanges", "financial data", "banks",
        "banking", "insurance", "asset management", "capital markets",
        "investment banking", "financial market infrastructure",
    ))
    if financial:
        result["ORDER_BOOK_CAPEX_GUIDANCE"] = RequirementApplicability(
            "NOT_APPLICABLE", "DOMAIN_NOT_APPLICABLE:NON_PRODUCTION_FINANCIAL_BUSINESS", **common)
        result["BUSINESS_QUALITY_FACTS"] = RequirementApplicability(
            "PARTIALLY_APPLICABLE", "INDUSTRIAL_ROCE_NOT_APPLICABLE", **common,
            excluded_inputs={"ROCE": "INDUSTRIAL_CAPITAL_EMPLOYED_NOT_APPLICABLE"})
    elif "software" in text:
        result["ORDER_BOOK_CAPEX_GUIDANCE"] = RequirementApplicability(
            "PARTIALLY_APPLICABLE", "SOFTWARE_BACKLOG_APPLIES_WITHOUT_INDUSTRIAL_CAPACITY", **common,
            excluded_inputs={"CAPACITY_OR_CAPEX_OR_COMMISSIONING": "INDUSTRIAL_PRODUCTION_CAPACITY_NOT_APPLICABLE"})
    else:
        # Manufacturing, EPC, defence and utilities retain all applicable targets.
        result["ORDER_BOOK_CAPEX_GUIDANCE"] = RequirementApplicability(
            "APPLICABLE", "PRODUCTION_OR_CONTRACTED_DELIVERY_REQUIREMENTS_RETAINED", **common)
    return result
