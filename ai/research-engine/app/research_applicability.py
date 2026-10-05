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


# Mirrors stock_rule_engine.StockRuleEngine._shareholding()'s own two
# scoring paths exactly, so readiness can never report SHAREHOLDING's
# mandatory PROMOTER_INSTITUTIONAL_PUBLIC_CATEGORIES input as covered for
# ownership evidence the rule engine cannot actually turn into a metric
# (the generic root cause behind a candidate going
# "deep_requirement_satisfied SHAREHOLDING state=READY" and then
# "RULE_AREA_UNSCORABLE blocking=['SHAREHOLDING']" for the exact same
# evidence -- not specific to any one instrument).
#
# Deliberately NOT the same question as shareholding_input_coverage()
# above, which stays single-snapshot and is still the correct, unchanged
# contract for: (a) per-snapshot field-level coverage inside
# shareholding_field_merge() (pledge/period bookkeeping, authority
# claiming), and (b) shareholding_period_groups()'s own
# qualifying_official/official_xbrl snapshot *selection* ranking. Neither
# of those is "can the rule engine score this" -- conflating them would
# either lose per-field provenance in (a) or change which snapshot is
# picked as authoritative in (b). This function answers only the
# multi-period scorability question, and only the SHAREHOLDING mandatory
# input is gated by it (see app/research_readiness_runtime.py's
# _append_shareholding).
SHAREHOLDING_TREND_SCORABLE_CATEGORIES = ("PROMOTER", "FII_FPI", "DII")


def shareholding_scorable_ownership_coverage(snapshots) -> bool:
    """True iff stock_rule_engine._shareholding() can produce at least one
    ownership metric from `snapshots` -- the exact one-best-snapshot-per-
    period, newest-last sequence it actually consumes (i.e.
    repository.shareholding_for(instrument_id, limit=N), the same shape
    StockRuleEngineInput.shareholding carries).

    Two paths, mirrored verbatim from _shareholding():
      1. Single-period: the latest (newest) period has a PROMOTER category
         value. ZERO IS VALID -- only the category's presence is checked,
         never its percentage, exactly like shareholding_input_coverage()
         above; a legitimate 0% promoter holding is never "missing".
      2. Trend: the two most recent DISTINCT periods both carry a value
         for the SAME trend-supported category (PROMOTER, FII_FPI or
         DII) -- matching _shareholding()'s own
         (PROMOTER, FII_FPI, DII) trend-metric loop exactly. A single
         period containing only FII_FPI/DII/PUBLIC_RETAIL (no PROMOTER,
         no second compatible period) satisfies neither path and is
         correctly reported NOT scorable, never silently treated as
         ready-but-unscorable.

    No synthetic snapshot is created or inspected; this only reads the
    category sets already present on real, persisted snapshots, and never
    weakens period/date/quarter validation (that filtering already
    happened upstream in shareholding_period_groups()/shareholding_for()
    before this function ever sees a snapshot).
    """
    if not snapshots:
        return False
    ordered = sorted(snapshots, key=lambda item: item.period_end)
    latest_categories = {str(value.category) for value in ordered[-1].values}
    if "PROMOTER" in latest_categories:
        return True
    if len(ordered) < 2:
        return False
    previous_categories = {str(value.category) for value in ordered[-2].values}
    return bool(
        latest_categories & previous_categories
        & set(SHAREHOLDING_TREND_SCORABLE_CATEGORIES)
    )


def shareholding_source_authority_rank(snapshot) -> int:
    """Slice 2 source authority contract, lower rank = higher authority:

    0. NSE structured / XBRL feed -- PRIMARY SOURCE OF TRUTH.
    2. Yahoo MCP (approved external tool) -- only for fields NSE is missing.
    3. Official NSE shareholding/result/announcement PDF -- last resort.
    4. Anything else (never expected for a real snapshot here).

    Mirrors the qualifying_official/official_xbrl checks shareholding_for()
    already uses, so ranking here can never disagree with the single-best
    selection that function performs.
    """
    provider = snapshot.source_provider.upper()
    is_nse = provider == "NSE"
    is_official_xbrl = is_nse and (
        snapshot.source_type == "NSE_SHAREHOLDING_XBRL"
        or any((value.source_locator or "").startswith("nse-xbrl:") for value in snapshot.values)
    )
    if is_official_xbrl:
        return 0
    if provider == "YAHOO_FINANCE_MCP":
        return 2
    if is_nse:
        # Official NSE PDF/announcement fallback: authoritative issuer, but
        # not the structured feed -- the contract places it below Yahoo.
        return 3
    return 4


def shareholding_field_merge(snapshots):
    """Authority-aware, field-level union of shareholding coverage across
    every real snapshot sharing one period.

    Each returned (snapshot, covered_input_ids) pair keeps that snapshot's
    own identity/provenance untouched -- no synthetic merged snapshot is
    ever manufactured. A lower-authority snapshot's covered_input_ids never
    includes a field a higher-authority snapshot already claimed for this
    same period, so lower-priority evidence can enrich a genuine gap but
    can never be read as overwriting/competing with higher-priority
    evidence for the same logical field. A snapshot that contributes
    nothing new (every one of its fields is already covered by a
    higher-authority source) is omitted entirely.

    ZERO IS VALID: shareholding_input_coverage() only checks for the
    presence of a category's ShareholdingSnapshotValue, never its
    percentage, so a legitimate 0% value (e.g. promoter_pledge_percent=0)
    is treated as present/covered here exactly like any other value.
    """
    ranked = sorted(snapshots, key=shareholding_source_authority_rank)
    claimed: set[str] = set()
    contributions = []
    for snapshot in ranked:
        covered = shareholding_input_coverage(snapshot) - claimed
        if not covered:
            continue
        claimed.update(covered)
        contributions.append((snapshot, covered))
    return contributions
