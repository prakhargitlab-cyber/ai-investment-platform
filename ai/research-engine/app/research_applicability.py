"""Deterministic business applicability; never inferred from missing evidence."""
from dataclasses import dataclass, field
import re
from typing import Mapping

# Canonical classification terms that mark an issuer as a financial company
# (banking / credit / lending / NBFC / housing finance / mortgage / leasing /
# microfinance / finance company). Shared with the rule-engine ROCE suppression
# (_is_financial) so applicability and scoring agree on what is "financial".
FINANCIAL_CLASSIFICATION_TERMS = (
    "stock exchanges", "financial exchanges", "financial data", "financial",
    "banks", "banking", "insurance", "asset management", "capital markets",
    "investment banking", "financial market infrastructure",
    "credit service", "credit services", "lending", "non-bank financial", "nbfc",
    "leasing", "housing finance", "mortgage", "mortgages",
    "finance company", "finance companies", "microfinance",
)

CONCEPT_INPUTS = {
    "ORDER_BOOK": "ORDER_BOOK_OR_MAJOR_CONTRACT",
    "CAPEX": "CAPACITY_OR_CAPEX_OR_COMMISSIONING",
    "GUIDANCE": "MANAGEMENT_GUIDANCE",
}
CONCEPT_CATEGORIES = {
    "ORDER_BOOK": frozenset({"ORDERS_BACKLOG", "CONTRACTS"}),
    "CAPEX": frozenset({"CAPEX", "NEW_FACILITIES"}),
    "GUIDANCE": frozenset({"GUIDANCE"}),
}

@dataclass(frozen=True)
class RequirementApplicability:
    state: str = "APPLICABLE"
    reason: str | None = None
    classification: str | None = None
    source: str | None = None
    excluded_inputs: Mapping[str, str] = field(default_factory=dict)
    concepts: Mapping[str, "RequirementApplicability"] = field(default_factory=dict)


def classify_requirements(sector: str | None, industry: str | None, source: str | None, *, asset_type: str | None = None):
    text = re.sub(r"[^a-z0-9]+", " ", industry.casefold()).strip() if industry and source else ""
    if text in {"unknown", "unclassified", "not available", "other", "n a", "na"}:
        text = ""
    common = dict(classification=f"{sector or 'unknown'} / {industry or 'unknown'}", source=source)
    if asset_type and asset_type.upper() in {"ETF", "MUTUAL_FUND", "INDEX", "BOND"}:
        decisions = {key: RequirementApplicability("NOT_APPLICABLE", "NOT_APPLICABLE_ASSET_TYPE", **common)
            for key in CONCEPT_INPUTS}
        return {"ORDER_BOOK_CAPEX_GUIDANCE": RequirementApplicability("NOT_APPLICABLE",
            "NOT_APPLICABLE_ASSET_TYPE", **common, concepts=decisions,
            excluded_inputs={value: "NOT_APPLICABLE_ASSET_TYPE" for value in CONCEPT_INPUTS.values()})}
    financial = any(term in text for term in FINANCIAL_CLASSIFICATION_TERMS)
    contracted = any(term in text for term in (
        "engineering", "construction", "aerospace", "defense", "defence", "electrical equipment",
        "industrial machinery", "software", "shipbuilding",
    ))
    decisions = {
        "ORDER_BOOK": RequirementApplicability(
            "NOT_APPLICABLE" if financial else "APPLICABLE" if contracted else "UNKNOWN",
            "NOT_APPLICABLE_BUSINESS_MODEL" if financial else "APPLICABLE_BUSINESS_MODEL" if contracted else "UNKNOWN_CLASSIFICATION", **common),
        # Financial and software companies also invest in assets and publish guidance.
        # Lack of an industrial plant cannot establish that either concept is N/A.
        "CAPEX": RequirementApplicability("APPLICABLE" if text else "UNKNOWN",
            "APPLICABLE_BUSINESS_MODEL" if text else "UNKNOWN_CLASSIFICATION", **common),
        "GUIDANCE": RequirementApplicability("APPLICABLE" if text else "UNKNOWN",
            "APPLICABLE_BUSINESS_MODEL" if text else "UNKNOWN_CLASSIFICATION", **common),
    }
    excluded = {CONCEPT_INPUTS[k]: v.reason for k, v in decisions.items() if v.state == "NOT_APPLICABLE"}
    result = {"ORDER_BOOK_CAPEX_GUIDANCE": RequirementApplicability(
        "PARTIALLY_APPLICABLE" if excluded else "APPLICABLE" if text else "UNKNOWN",
        "INDEPENDENT_CONCEPT_APPLICABILITY", **common, excluded_inputs=excluded, concepts=decisions)}
    if financial:
        result["BUSINESS_QUALITY_FACTS"] = RequirementApplicability(
            "PARTIALLY_APPLICABLE", "INDUSTRIAL_ROCE_NOT_APPLICABLE", **common,
            excluded_inputs={"ROCE": "INDUSTRIAL_CAPITAL_EMPLOYED_NOT_APPLICABLE"})
    return result


def event_concept(event_type: str) -> str | None:
    if event_type in {"NEW_ORDER", "ORDER_BACKLOG_CHANGE", "MAJOR_CONTRACT", "GOVERNMENT_CONTRACT", "ORDER_CANCELLED"}:
        return "ORDER_BOOK"
    if event_type in {"CAPEX", "FACTORY_EXPANSION", "CAPACITY_EXPANSION", "NEW_FACILITY", "PROJECT_DELAY"}:
        return "CAPEX"
    if event_type.startswith("GUIDANCE_"):
        return "GUIDANCE"
    return None
