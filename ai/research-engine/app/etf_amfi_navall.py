"""AMFI NAVAll.txt: exact-ISIN fund-house / AMFI scheme-code evidence for ETFs.

Source (verified by a host capture on 2026-11-05): https://portal.amfiindia.com/spages/NAVAll.txt, UTF-8, CRLF,
semicolon separated, header
``Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Plan;Option;Net Asset Value;Date``.
Lines without ``;`` are either a category heading (``Open Ended Schemes(<type> - <category>)``) or a fund-house heading;
whitespace-only lines separate them. Category headings are the only reliable ETF marker: ``... ETF ... Fund of Fund``
schemes live under FoF categories and must not match.

Identity is by exact ISIN only (never by name). An ISIN that appears on more than one ETF row, or a scheme code that
repeats, is ambiguous and yields no evidence. The file's NAV is intentionally NOT turned into NAV evidence here:
NSE remains the NAV source; this module supplies fund house and the AMFI scheme code (the join key for AMFI TER data).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from app.etf_evidence import (
    EtfAcquisitionAttempt, EtfAcquisitionOutcome, EtfAuthority, EtfFact, EtfMetric, EtfProvenance, evidence_id,
)

AMFI_NAVALL_URL = "https://portal.amfiindia.com/spages/NAVAll.txt"
EXPECTED_HEADER = ("Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Plan;Option;"
                   "Net Asset Value;Date")
_CATEGORY = re.compile(r"^(Open|Close|Interval)[A-Za-z ]*Schemes?\((.+)\)$")
_ETF_CATEGORY = re.compile(r"^(Exchange Traded Funds \(ETFs\)\s*-\s*.+|Other Scheme\s*-\s*(Gold ETF|Other\s+ETFs))$")
_ISIN = re.compile(r"^IN[A-Z0-9]{9}[0-9]$")
_CODE = re.compile(r"^\d{4,8}$")


class AmfiNavAllError(ValueError):
    pass


@dataclass(frozen=True)
class AmfiEtfScheme:
    scheme_code: str
    isins: tuple[str, ...]
    scheme_name: str
    fund_house: str
    category: str
    nav_text: str
    nav_date: date | None


@dataclass
class AmfiNavAllIndex:
    by_isin: dict[str, AmfiEtfScheme]
    ambiguous_isins: set[str]
    row_count: int
    etf_row_count: int


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("﻿", "")).strip()


def parse_amfi_navall(text: str) -> AmfiNavAllIndex:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if not lines or lines[0].strip().lstrip("﻿") != EXPECTED_HEADER:
        raise AmfiNavAllError("AMFI_NAVALL_HEADER_MISMATCH")
    category = fund_house = None
    schemes: list[AmfiEtfScheme] = []
    rows = 0
    for number, raw in enumerate(lines[1:], start=2):
        line = raw.strip()
        if not line:
            continue
        if ";" not in line:
            match = _CATEGORY.match(_clean(line))
            if match:
                category, fund_house = _clean(match.group(2)), None
            else:
                fund_house = _clean(line)
            continue
        parts = [p.strip() for p in line.split(";")]
        if len(parts) != 8:
            raise AmfiNavAllError(f"AMFI_NAVALL_MALFORMED_ROW:{number}")
        rows += 1
        if category is None or not _ETF_CATEGORY.match(category):
            continue
        code, isin1, isin2, name, _plan, _option, nav, nav_date = parts
        if fund_house is None or not _CODE.match(code) or not name:
            raise AmfiNavAllError(f"AMFI_NAVALL_MALFORMED_ETF_ROW:{number}")
        isins = tuple(v for v in (isin1, isin2) if v and v != "-")
        if not isins or any(not _ISIN.match(v) for v in isins):
            raise AmfiNavAllError(f"AMFI_NAVALL_BAD_ISIN:{number}")
        try:
            parsed_date = datetime.strptime(nav_date, "%d-%b-%Y").date()
        except ValueError:
            parsed_date = None
        schemes.append(AmfiEtfScheme(code, isins, name, fund_house, category, nav, parsed_date))
    by_isin: dict[str, AmfiEtfScheme] = {}
    ambiguous: set[str] = set()
    code_owners: dict[str, set[tuple[str, ...]]] = {}
    for scheme in schemes:
        code_owners.setdefault(scheme.scheme_code, set()).add(scheme.isins)
        for isin in scheme.isins:
            if isin in by_isin and by_isin[isin] != scheme:
                ambiguous.add(isin)
            by_isin[isin] = scheme
    for scheme in schemes:  # a repeated scheme code with different ISINs poisons both ISIN sets
        if len(code_owners[scheme.scheme_code]) > 1:
            ambiguous.update(scheme.isins)
    for isin in ambiguous:
        by_isin.pop(isin, None)
    return AmfiNavAllIndex(by_isin, ambiguous, rows, len(schemes))


def fund_identity_for(index: AmfiNavAllIndex, *, isin: str, instrument_id, retrieved_at: datetime,
                      published_at: datetime | None = None):
    """Return (evidence, attempt) for one canonical ETF; evidence is [] unless exactly one AMFI ETF row has this ISIN."""
    isin = re.sub(r"\s+", "", str(isin or "")).upper()
    provider = "AMFI"
    if isin in index.ambiguous_isins:
        outcome, reason, scheme = EtfAcquisitionOutcome.TECHNICAL_FAILURE, "AMFI_ISIN_AMBIGUOUS", None
    else:
        scheme = index.by_isin.get(isin)
        outcome, reason = ((EtfAcquisitionOutcome.SUCCESS_WITH_DATA, "EXACT_ISIN_MATCH") if scheme else
                           (EtfAcquisitionOutcome.SUCCESS_EMPTY, "ISIN_NOT_IN_AMFI_ETF_CATEGORIES"))
    evidence = []
    if scheme is not None:
        provenance = EtfProvenance(
            provider=provider, source_type="AMFI_NAVALL", source_identity=f"AMFI:{scheme.scheme_code}",
            source_url=AMFI_NAVALL_URL, authority=EtfAuthority.OTHER_AUTHORITATIVE, published_at=published_at,
            retrieved_at=retrieved_at,
            source_metadata={"amfi_scheme_code": scheme.scheme_code, "amfi_category": scheme.category,
                             "amfi_scheme_name": scheme.scheme_name, "matched_isin": isin})
        evidence.append(EtfFact(instrument_id=instrument_id, metric=EtfMetric.AMC_FUND_HOUSE,
                                value=scheme.fund_house, as_of_date=scheme.nav_date, provenance=provenance))
    attempt = EtfAcquisitionAttempt(
        instrument_id=instrument_id, metric=EtfMetric.AMC_FUND_HOUSE.value, provider=provider,
        attempted_at=retrieved_at, outcome=outcome, reason=reason, evidence_ids=[evidence_id(e) for e in evidence])
    return evidence, attempt
