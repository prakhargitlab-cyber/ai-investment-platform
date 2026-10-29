"""ETF-only classification and applicability contracts; no execution or I/O.

Future acquisition, readiness and scoring must consume etf_applicability.
NAV and MARKET_PRICE are independent requirements, with no substitution rule.
No scoring weights, thresholds or evidence-availability policy is defined here.
"""
from dataclasses import dataclass
from enum import StrEnum
import re

from app.models import EtfResearchProfile, EtfSubtype


class EtfRequirement(StrEnum):
    IDENTITY = "IDENTITY"
    BENCHMARK_INDEX = "BENCHMARK_INDEX"
    NAV = "NAV"
    MARKET_PRICE = "MARKET_PRICE"
    AUM = "AUM"
    EXPENSE_RATIO = "EXPENSE_RATIO"
    TRACKING_ERROR = "TRACKING_ERROR"
    TRACKING_DIFFERENCE = "TRACKING_DIFFERENCE"
    LIQUIDITY = "LIQUIDITY"
    BID_ASK_SPREAD = "BID_ASK_SPREAD"
    TRADING_VOLUME = "TRADING_VOLUME"
    FUND_AGE = "FUND_AGE"
    AMC_FUND_HOUSE = "AMC_FUND_HOUSE"
    REPLICATION_METHOD = "REPLICATION_METHOD"
    UNDERLYING_HOLDINGS = "UNDERLYING_HOLDINGS"
    CONCENTRATION = "CONCENTRATION"
    INDEX_CONSTITUENTS = "INDEX_CONSTITUENTS"
    INDEX_VALUATION = "INDEX_VALUATION"
    INDEX_MOMENTUM = "INDEX_MOMENTUM"
    HISTORICAL_RETURNS = "HISTORICAL_RETURNS"
    VOLATILITY = "VOLATILITY"
    DRAWDOWN = "DRAWDOWN"
    CATEGORY_RELATIVE_PERFORMANCE = "CATEGORY_RELATIVE_PERFORMANCE"
    CURRENT_NEWS = "CURRENT_NEWS"


class EtfRequirementRole(StrEnum):
    MANDATORY = "MANDATORY"
    SUPPORTING = "SUPPORTING"
    CONTEXTUAL = "CONTEXTUAL"
    NOT_APPLICABLE = "NOT_APPLICABLE"


@dataclass(frozen=True)
class EtfRequirementApplicability:
    role: EtfRequirementRole
    reason: str

    @property
    def blocking(self) -> bool:
        return self.role == EtfRequirementRole.MANDATORY


def etf_applicability(
    subtype: EtfSubtype, *, is_benchmark_based: bool | None = None,
) -> dict[EtfRequirement, EtfRequirementApplicability]:
    """One conservative contract for every subtype, including OTHER.

    Unknown benchmark status retains the requirement until explicit metadata
    establishes that it is not benchmark based. Missing evidence never sets
    NOT_APPLICABLE. Supporting inputs are not mandatory evidence gates.
    """
    EtfSubtype(subtype)  # Reject accidental stock classifications.
    roles = {r: EtfRequirementApplicability(EtfRequirementRole.SUPPORTING, "SUPPORTING_ETF_EVIDENCE")
             for r in EtfRequirement}
    for requirement in (EtfRequirement.IDENTITY, EtfRequirement.NAV, EtfRequirement.MARKET_PRICE,
                        EtfRequirement.AUM, EtfRequirement.EXPENSE_RATIO, EtfRequirement.LIQUIDITY):
        roles[requirement] = EtfRequirementApplicability(EtfRequirementRole.MANDATORY, "CORE_ETF_EVIDENCE")
    for requirement in (EtfRequirement.INDEX_VALUATION, EtfRequirement.INDEX_MOMENTUM,
                        EtfRequirement.CATEGORY_RELATIVE_PERFORMANCE, EtfRequirement.CURRENT_NEWS):
        roles[requirement] = EtfRequirementApplicability(EtfRequirementRole.CONTEXTUAL, "NONBLOCKING_CONTEXT")
    roles[EtfRequirement.BENCHMARK_INDEX] = EtfRequirementApplicability(
        EtfRequirementRole.MANDATORY,
        "BENCHMARK_BASED" if is_benchmark_based is True else "BENCHMARK_STATUS_UNCONFIRMED",
    )
    if is_benchmark_based is False:
        for requirement in (EtfRequirement.BENCHMARK_INDEX, EtfRequirement.TRACKING_ERROR,
                            EtfRequirement.TRACKING_DIFFERENCE, EtfRequirement.INDEX_CONSTITUENTS,
                            EtfRequirement.INDEX_VALUATION, EtfRequirement.INDEX_MOMENTUM):
            roles[requirement] = EtfRequirementApplicability(
                EtfRequirementRole.NOT_APPLICABLE, "EXPLICITLY_NOT_BENCHMARK_BASED")
    return roles


def _normalized(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").casefold()).strip()


_CATEGORY_SUBTYPES = {_normalized(s.value): s for s in EtfSubtype}

# Broad asset-class tokens that classify_etf's asset-family branching
# already recognizes (equity/debt/gold/...). Used to tell a genuine
# broad-asset-class field from an unrelated free-text field (e.g. NSE
# market-watch's 'assets' description text) before it is trusted as
# the classification input.
_BROAD_ASSET_CLASS_TOKENS = {
    "equity", "debt", "fixed income", "bonds", "gold", "silver",
    "money market", "hybrid", "multi asset",
}
_CATEGORY_SUBTYPES.update({
    "sector thematic": EtfSubtype.EQUITY_SECTOR_THEMATIC,
    "smart beta": EtfSubtype.EQUITY_SMART_BETA,
    "fixed income": EtfSubtype.DEBT,
    "money market": EtfSubtype.MONEY_MARKET,
    "multi asset": EtfSubtype.HYBRID,
})


# Known NSE benchmark-index family names (stable, publicly documented NSE
# index taxonomy -- read from the already identity-verified
# subCatagory/mainCatagory metadata field, never from a fund's own name or
# ticker). A plain index-tracking ETF's subCatagory is typically the
# literal benchmark name itself (e.g. "Nifty 50"), not a generic strategy
# label like "equity index" -- classify_etf's exact-match strategy
# vocabulary previously had no entry for this, so such an ETF fell through
# to OTHER/unclassified even though it is an ordinary, textbook index fund.
_NSE_SECTOR_INDEX_KEYWORDS = (
    "bank", "it", "pharma", "auto", "fmcg", "energy", "metal", "realty",
    "psu", "infrastructure", "infra", "media", "financial services",
    "consumer durables", "healthcare", "oil gas",
)


def _nse_benchmark_strategy_bucket(strategy: str) -> str | None:
    """Map a normalized, already-verified NSE subCatagory benchmark-index
    name to the same strategy buckets classify_etf's exact-match vocabulary
    already understands. Returns None for anything not recognizably an NSE
    index-family name, so callers fall back to their existing behavior."""
    if not strategy or not strategy.split(" ")[0] in {"nifty", "sensex", "bse"}:
        return None
    # Match whole normalized tokens/phrases: "it" inside "quality" or
    # "digital" does not establish the IT sector.
    words = f" {strategy} "
    if any(f" {keyword} " in words for keyword in _NSE_SECTOR_INDEX_KEYWORDS):
        return "sector thematic"
    # Only established broad benchmarks may supply a missing strategy.
    # A recognized prefix alone cannot classify an unknown factor/theme.
    if strategy in {"nifty 50", "nifty next 50", "nifty 500", "sensex", "bse sensex"}:
        return "broad market index"
    return None


def classify_etf(
    profile: EtfResearchProfile, *, category: str | None = None,
    investment_strategy: str | None = None, exposure_region: str | None = None,
) -> EtfSubtype:
    """Classify explicit metadata only; identifiers/names/prices are not signals.

    Exact normalized categories avoid substring guesses (e.g. a gold-mining
    equity fund is not a physical-gold ETF). Conflicts resolve to OTHER.
    A benchmark name alone does not establish its asset class or strategy.
    """
    candidates = set()
    if profile.subtype != EtfSubtype.OTHER:
        candidates.add(EtfSubtype(profile.subtype))
    if (mapped := _CATEGORY_SUBTYPES.get(_normalized(category))) and mapped != EtfSubtype.OTHER:
        candidates.add(mapped)
    asset = _normalized(profile.asset_class)
    region = _normalized(exposure_region)
    strategy = _normalized(investment_strategy)
    # A benchmark family is weaker than an explicit verified subtype/category.
    # Keep exact strategy labels below so genuine category conflicts still fail.
    if not candidates:
        strategy = _nse_benchmark_strategy_bucket(strategy) or strategy
    international = region in {"international", "global", "overseas"}
    if asset == "equity":
        if international:
            candidates.add(EtfSubtype.INTERNATIONAL_EQUITY)
        elif strategy in {"sector", "thematic", "sector thematic", "equity sector thematic"}:
            candidates.add(EtfSubtype.EQUITY_SECTOR_THEMATIC)
        elif strategy in {"smart beta", "factor", "multi factor"}:
            candidates.add(EtfSubtype.EQUITY_SMART_BETA)
        elif strategy in {"broad market index", "equity index"}:
            candidates.add(EtfSubtype.EQUITY_INDEX)
    elif asset in {"debt", "fixed income", "bonds"}:
        if international:
            candidates.add(EtfSubtype.INTERNATIONAL_DEBT)
        elif not candidates:
            candidates.add(EtfSubtype.DEBT)
    elif asset in {"gold", "silver", "money market", "hybrid", "multi asset"}:
        candidates.add(_CATEGORY_SUBTYPES[asset])
    # An explicit asset class must also agree with categorical subtype evidence.
    families = {
        "equity": {EtfSubtype.EQUITY_INDEX, EtfSubtype.EQUITY_SECTOR_THEMATIC, EtfSubtype.EQUITY_SMART_BETA,
                   EtfSubtype.INTERNATIONAL_EQUITY},
        "commodity": {EtfSubtype.GOLD, EtfSubtype.SILVER},
        "debt": {EtfSubtype.DEBT, EtfSubtype.INTERNATIONAL_DEBT, EtfSubtype.MONEY_MARKET},
    }
    family = families.get("debt" if asset in {"fixed income", "bonds"} else asset)
    if family and not candidates <= family:
        return EtfSubtype.OTHER
    return next(iter(candidates)) if len(candidates) == 1 else EtfSubtype.OTHER
