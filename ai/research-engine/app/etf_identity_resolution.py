"""Conservative ETF identity reconciliation across NSE canonical ids, AMFI NAVAll and the AMFI scheme master.

Principles (user decision, 2026-11-05): no fuzzy matching; a name alone never identifies an ETF. An exact
(whitespace/case-normalised) name may select an AMFI scheme only when ALL of these hold: the AMC identity is
independently verified, the category is a compatible ETF category (never fund-of-funds), exactly one AMFI ETF scheme
carries that name, that scheme maps to exactly one canonical ETF, and no identifier conflicts across sources.
Anything else is REJECTED with an explicit reason; a rejection never degrades to a guess.

AMFI "Launch Date" is the NFO open date; it is never used as listing or inception.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from app.etf_amfi_navall import AmfiNavAllIndex
from app.etf_amfi_scheme_master import AmfiMasterEtfScheme, AmfiSchemeMasterIndex, is_etf_category, norm_space


class IdentityConfidence(StrEnum):
    VERIFIED_ISIN_TWO_SOURCES = "VERIFIED_ISIN_TWO_SOURCES"   # ISIN+code+category+AMC agree in NAVAll and master
    VERIFIED_EXACT_NAME = "VERIFIED_EXACT_NAME"               # ISIN route plus all exact-name conditions
    REJECTED = "REJECTED"


class IdentityRejection(StrEnum):
    ISIN_NOT_IN_NAVALL = "ISIN_NOT_IN_NAVALL"
    ISIN_NOT_IN_SCHEME_MASTER = "ISIN_NOT_IN_SCHEME_MASTER"
    ISIN_AMBIGUOUS = "ISIN_AMBIGUOUS"
    SCHEME_CODE_CONFLICT = "SCHEME_CODE_CONFLICT"
    CATEGORY_CONFLICT = "CATEGORY_CONFLICT"
    AMC_UNVERIFIED = "AMC_UNVERIFIED"
    AMC_CONFLICT = "AMC_CONFLICT"
    NOT_ETF_CATEGORY = "NOT_ETF_CATEGORY"
    FUND_OF_FUND = "FUND_OF_FUND"
    NAME_NO_MATCH = "NAME_NO_MATCH"
    NAME_AMBIGUOUS = "NAME_AMBIGUOUS"
    NO_CANONICAL_ETF = "NO_CANONICAL_ETF"
    CANONICAL_AMBIGUOUS = "CANONICAL_AMBIGUOUS"
    CANONICAL_ISIN_CONFLICT = "CANONICAL_ISIN_CONFLICT"


@dataclass(frozen=True)
class IdentityDecision:
    confidence: IdentityConfidence
    rejection: IdentityRejection | None = None
    scheme_code: str | None = None
    isin: str | None = None
    amc: str | None = None
    category: str | None = None
    instrument_id: object | None = None

    @property
    def accepted(self) -> bool:
        return self.confidence is not IdentityConfidence.REJECTED


def _reject(reason: IdentityRejection, **kw) -> IdentityDecision:
    return IdentityDecision(IdentityConfidence.REJECTED, reason, **kw)


def build_amc_pairing(navall: AmfiNavAllIndex, master: AmfiSchemeMasterIndex) -> dict[str, str]:
    """Data-driven NAVAll fund-house -> master AMC pairing. The two files name an AMC differently
    ("SBI Mutual Fund" vs "SBI Funds Management Limited"), so equality is never assumed. A pair is verified only when
    it is one-to-one over every ETF scheme that both files carry with the same code and ISIN; any fund house seen
    with more than one AMC (or vice versa) is dropped."""
    pairs: dict[str, set[str]] = {}
    reverse: dict[str, set[str]] = {}
    for isin, n in navall.by_isin.items():
        m = master.by_isin.get(isin)
        if m is None or m.scheme_code != n.scheme_code:
            continue
        pairs.setdefault(n.fund_house, set()).add(m.amc)
        reverse.setdefault(m.amc, set()).add(n.fund_house)
    return {fh: next(iter(a)) for fh, a in pairs.items()
            if len(a) == 1 and len(reverse[next(iter(a))]) == 1}


def reconcile_isin(isin: str, navall: AmfiNavAllIndex, master: AmfiSchemeMasterIndex,
                   pairing: Mapping[str, str]) -> IdentityDecision:
    if isin in navall.ambiguous_isins or isin in master.ambiguous_isins:
        return _reject(IdentityRejection.ISIN_AMBIGUOUS, isin=isin)
    n = navall.by_isin.get(isin)
    if n is None:
        return _reject(IdentityRejection.ISIN_NOT_IN_NAVALL, isin=isin)
    m = master.by_isin.get(isin)
    if m is None:
        return _reject(IdentityRejection.ISIN_NOT_IN_SCHEME_MASTER, isin=isin, scheme_code=n.scheme_code)
    if m.scheme_code != n.scheme_code:
        return _reject(IdentityRejection.SCHEME_CODE_CONFLICT, isin=isin)
    if not is_etf_category(m.category) or not is_etf_category(n.category):
        return _reject(IdentityRejection.NOT_ETF_CATEGORY, isin=isin, scheme_code=m.scheme_code)
    if norm_space(n.category).casefold() != norm_space(m.category).casefold():
        return _reject(IdentityRejection.CATEGORY_CONFLICT, isin=isin, scheme_code=m.scheme_code)
    paired = pairing.get(n.fund_house)
    if paired is None:
        return _reject(IdentityRejection.AMC_UNVERIFIED, isin=isin, scheme_code=m.scheme_code)
    if paired != m.amc:
        return _reject(IdentityRejection.AMC_CONFLICT, isin=isin, scheme_code=m.scheme_code)
    return IdentityDecision(IdentityConfidence.VERIFIED_ISIN_TWO_SOURCES, None, m.scheme_code, isin, m.amc,
                            m.category)


def resolve_exact_name(name: str, *, amc: str, navall: AmfiNavAllIndex, master: AmfiSchemeMasterIndex,
                       pairing: Mapping[str, str], canonical_by_isin: Mapping[str, object],
                       canonical_duplicate_isins: frozenset[str] = frozenset()) -> IdentityDecision:
    """Exact normalised-name resolution for a disclosure that carries no ISIN (e.g. the SBI TER workbook).

    ``amc`` is the AMC legal name as published in the scheme master and must be an AMC whose pairing with a NAVAll
    fund house is independently verified. ``canonical_by_isin`` maps ISIN -> canonical ETF id; ISINs listed in
    ``canonical_duplicate_isins`` (several canonical rows) are ambiguous."""
    key = norm_space(name).casefold()
    if "fund of fund" in key or " fof" in f" {key}" or "fof " in key:
        return _reject(IdentityRejection.FUND_OF_FUND)
    if amc not in set(pairing.values()):
        return _reject(IdentityRejection.AMC_UNVERIFIED)
    matches = [s for s in master.by_code.values()
               if s.amc == amc and s.scheme_name.casefold() == key]
    if (amc, key) in master.ambiguous_names:
        return _reject(IdentityRejection.NAME_AMBIGUOUS)
    if not matches:
        return _reject(IdentityRejection.NAME_NO_MATCH)
    if len(matches) > 1:
        return _reject(IdentityRejection.NAME_AMBIGUOUS)
    m = matches[0]
    if not is_etf_category(m.category):
        return _reject(IdentityRejection.NOT_ETF_CATEGORY, scheme_code=m.scheme_code)
    if len(m.isins) != 1:
        return _reject(IdentityRejection.ISIN_AMBIGUOUS, scheme_code=m.scheme_code)
    isin = m.isins[0]
    base = reconcile_isin(isin, navall, master, pairing)
    if not base.accepted:
        return base
    if isin in canonical_duplicate_isins:
        return _reject(IdentityRejection.CANONICAL_AMBIGUOUS, scheme_code=m.scheme_code, isin=isin)
    instrument = canonical_by_isin.get(isin)
    if instrument is None:
        return _reject(IdentityRejection.NO_CANONICAL_ETF, scheme_code=m.scheme_code, isin=isin)
    return IdentityDecision(IdentityConfidence.VERIFIED_EXACT_NAME, None, m.scheme_code, isin, m.amc,
                            m.category, instrument)


def build_name_based_code_resolver(staged_names: Mapping[str, str], *, amc: str, navall: AmfiNavAllIndex,
                                   master: AmfiSchemeMasterIndex, canonical_by_isin: Mapping[str, object],
                                   canonical_duplicate_isins: frozenset[str] = frozenset(),
                                   on_decision: Callable[[str, IdentityDecision], None] | None = None
                                   ) -> Callable[[str], object | None]:
    """Adapter hook: NSDL scheme code -> canonical id, resolved ONLY via resolve_exact_name. Every NSDL code is
    resolved from its own workbook name; two workbook codes that resolve to the same instrument are both rejected
    (one disclosure line must not be attributed to an ETF twice)."""
    pairing = build_amc_pairing(navall, master)
    decisions = {code: resolve_exact_name(name, amc=amc, navall=navall, master=master, pairing=pairing,
                                          canonical_by_isin=canonical_by_isin,
                                          canonical_duplicate_isins=canonical_duplicate_isins)
                 for code, name in staged_names.items()}
    claimed: dict[object, list[str]] = {}
    for code, d in decisions.items():
        if d.accepted:
            claimed.setdefault(d.instrument_id, []).append(code)
    for instrument, codes in claimed.items():
        if len(codes) > 1:
            for code in codes:
                decisions[code] = _reject(IdentityRejection.CANONICAL_AMBIGUOUS, scheme_code=decisions[code].scheme_code)

    def resolve(code: str):
        d = decisions.get(code)
        if d is None:
            return None
        if on_decision:
            on_decision(code, d)
        return d.instrument_id if d.accepted else None
    resolve.decisions = decisions  # type: ignore[attr-defined]
    return resolve
