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

# Terms that identify regulated lenders / HFCs / banking / NBFCs which publish
# regulatory ratios (capital adequacy, gross NPA, net NPA). Shared across
# research_applicability, stock_rule_engine, and structured_research so all
# components agree on what constitutes a "financial-sector" issuer.
FINANCIAL_SECTOR_SUBTYPE_TERMS = (
    "banks", "banking", "non-bank", "nbfc", "housing finance", "mortgage finance",
    "microfinance", "credit service", "credit services", "lending", "finance company",
    "micro finance", "nbf", "cooperative bank", "urban financial",
)

# Terms that identify asset management companies -- they are financial but
# do NOT publish lender-style regulatory ratios.
ASSET_MANAGEMENT_TERMS = ("asset management", "investment management")

# Canonical financial-sector ratio metrics that the StockRuleEngine
# `_balance_sheet` financial branch queries via `_latest_fact`. Each maps
# a durable `FinancialFact` metric name to the text labels the official NSE
# result parser already uses.
FINANCIAL_SECTOR_RATIO_METRICS = (
    ("capital_adequacy", ("capital adequacy", "capital adequacy ratio", "crar")),
    ("gross_npa", ("gross npa", "gnpa")),
    ("net_npa", ("net npa", "nnpa")),
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
    result["BALANCE_SHEET_FACTS"] = balance_sheet_applicability(industry, sector)
    return result


def event_concept(event_type: str) -> str | None:
    if event_type in {"NEW_ORDER", "ORDER_BACKLOG_CHANGE", "MAJOR_CONTRACT", "GOVERNMENT_CONTRACT", "ORDER_CANCELLED"}:
        return "ORDER_BOOK"
    if event_type in {"CAPEX", "FACTORY_EXPANSION", "CAPACITY_EXPANSION", "NEW_FACILITY", "PROJECT_DELAY"}:
        return "CAPEX"
    if event_type.startswith("GUIDANCE_"):
        return "GUIDANCE"
    return None


def classify_financial_subtype(industry: str | None, sector: str | None = None) -> str:
    """Classify a financial issuer's subtype for balance-sheet scoring.

    Returns one of:
    - "FINANCIAL_SECTOR" for regulated lenders/HFCs/banks/NBFCs that publish
      capital adequacy, gross NPA, net NPA ratios.
    - "ASSET_MANAGEMENT" for investment/asset management companies that do not.
    - "CORPORATE" for non-financial issuers.

    This shared contract ensures consistency across research_applicability,
    stock_rule_engine, and structured_research.
    """
    text = (sector or "").casefold() + " " + (industry or "")
    # Normalize: keep only alphanumeric and spaces (remove hyphens, dashes, etc.)
    text = re.sub(r"[^a-z0-9\s]+", " ", text.casefold()).strip()

    # Check for asset management first (more specific)
    if any(re.sub(r"[^a-z0-9\s]+", " ", term).strip() in text for term in ASSET_MANAGEMENT_TERMS):
        return "ASSET_MANAGEMENT"

    # Check for regulated lender/HFC/banking terms
    # Normalize search terms the same way as text (remove hyphens, etc.)
    if (any(re.sub(r"[^a-z0-9\s]+", " ", term).strip() in text for term in FINANCIAL_SECTOR_SUBTYPE_TERMS)
            or re.search(r"\bhfc\b", text)):
        return "FINANCIAL_SECTOR"

    # Default to corporate for non-financial
    return "CORPORATE"


def is_regulated_lender_or_hfc(industry: str | None, sector: str | None = None) -> bool:
    """Return True if the issuer is a regulated lender/HFC/banking/NBFC entity.

    Combined with BALANCE_SHEET_FACTS readiness, this determines whether
    capital_adequacy/gross_npa/net_npa ratios are applicable scoring metrics.
    """
    return classify_financial_subtype(industry, sector) == "FINANCIAL_SECTOR"


LENDER_BALANCE_SHEET_INPUT = "FINANCIAL_SECTOR_CAPITAL_OR_ASSET_QUALITY"
LENDER_BALANCE_SHEET_METRICS = {
    "CAPITAL_ADEQUACY": ("capital_adequacy", "capital_adequacy_ratio"),
    "GROSS_NPA": ("gross_npa", "gross_npa_ratio"),
    "NET_NPA": ("net_npa", "net_npa_ratio"),
}
CORPORATE_BALANCE_SHEET_INPUTS = (
    "DEBT", "EQUITY", "CASH", "INTEREST_COVERAGE_INPUTS", "LIQUIDITY_CURRENT_RATIO_INPUTS",
)


def balance_sheet_applicability(industry=None, sector=None):
    lender = is_regulated_lender_or_hfc(industry, sector)
    excluded = CORPORATE_BALANCE_SHEET_INPUTS if lender else (LENDER_BALANCE_SHEET_INPUT,)
    return RequirementApplicability(
        "PARTIALLY_APPLICABLE", "BALANCE_SHEET_SUBTYPE_INPUTS",
        classification=classify_financial_subtype(industry, sector),
        excluded_inputs={key: "NOT_APPLICABLE_BUSINESS_MODEL" for key in excluded},
    )


def shareholding_input_coverage(snapshot):
    """Use the same mandatory ownership semantics for readiness and recovery."""
    categories = {str(value.category) for value in snapshot.values}
    covered = {"LATEST_VALID_SHAREHOLDING_PERIOD"}
    if categories & {"PROMOTER", "FII_FPI", "DII", "PUBLIC_RETAIL"}:
        covered.add("PROMOTER_INSTITUTIONAL_PUBLIC_CATEGORIES")
    if "PROMOTER_PLEDGE" in categories:
        covered.add("PROMOTER_PLEDGE")
    return covered
