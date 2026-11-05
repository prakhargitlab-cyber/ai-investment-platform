"""AMFI scheme master (DownloadSchemeData_Po.aspx?mf=0): second identity source for ETFs, next to NAVAll.

Verified by a host capture on 2026-11-05: CSV, 10 fields per row, header
``AMC,Code,Scheme Name,Scheme Type,Scheme Category,Scheme NAV Name,Scheme Minimum Amount,Launch Date, Closure Date,
ISIN Div Payout/ ISIN GrowthISIN Div Reinvestment`` (the last two header names are fused in the source and the
column holds the ISIN(s); a scheme with two ISINs has them concatenated without separator).

"Launch Date" / " Closure Date" are the NFO open/close dates. They are exposed ONLY as ``nfo_open_date`` /
``nfo_close_date`` and are never an exchange listing date or a fund inception date.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import date, datetime

EXPECTED_HEADER = ("AMC", "Code", "Scheme Name", "Scheme Type", "Scheme Category", "Scheme NAV Name",
                   "Scheme Minimum Amount", "Launch Date", "Closure Date",
                   "ISIN Div Payout/ ISIN GrowthISIN Div Reinvestment")
_ETF_CATEGORY = re.compile(r"^(Exchange Traded Funds \(ETFs\)\s*-\s*.+|Other Scheme\s*-\s*(Gold ETF|Other\s+ETFs))$")
_ISIN = re.compile(r"^IN[A-Z0-9]{9}[0-9]$")
_CODE = re.compile(r"^\d{4,8}$")


class AmfiSchemeMasterError(ValueError):
    pass


def norm_space(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("﻿", "")).strip()


@dataclass(frozen=True)
class AmfiMasterEtfScheme:
    scheme_code: str
    amc: str
    scheme_name: str
    nav_name: str
    category: str
    isins: tuple[str, ...]
    nfo_open_date: date | None   # AMFI "Launch Date": NOT listing, NOT inception
    nfo_close_date: date | None


@dataclass
class AmfiSchemeMasterIndex:
    by_code: dict[str, AmfiMasterEtfScheme]
    by_isin: dict[str, AmfiMasterEtfScheme]
    ambiguous_codes: set[str]
    ambiguous_isins: set[str]
    ambiguous_names: set[tuple[str, str]]  # (amc, casefolded name) of scheme codes with conflicting rows
    row_count: int
    etf_row_count: int


def _date(value: str) -> date | None:
    try:
        return datetime.strptime(value.strip(), "%d-%b-%Y").date()
    except ValueError:
        return None


def _split_isins(raw: str) -> tuple[str, ...] | None:
    raw = raw.strip().replace(" ", "")
    if not raw or len(raw) % 12:
        return None
    parts = tuple(raw[i:i + 12] for i in range(0, len(raw), 12))
    return parts if all(_ISIN.match(p) for p in parts) else None


def is_etf_category(category: str) -> bool:
    return bool(_ETF_CATEGORY.match(norm_space(category)))


def parse_amfi_scheme_master(text: str) -> AmfiSchemeMasterIndex:
    reader = csv.reader(io.StringIO(text.lstrip("﻿"), newline=""))
    header = next(reader, None)
    if header is None or tuple(h.strip() for h in header) != EXPECTED_HEADER:
        raise AmfiSchemeMasterError("AMFI_SCHEME_MASTER_UNSUPPORTED_HEADER")
    rows = etf_rows = 0
    codes: dict[str, list[AmfiMasterEtfScheme]] = {}
    for row in reader:
        if not row or not any(c.strip() for c in row):
            continue
        rows += 1
        if len(row) != len(EXPECTED_HEADER):
            continue  # malformed row: never guessed at
        if not is_etf_category(row[4]):
            continue
        etf_rows += 1
        code, isins = row[1].strip(), _split_isins(row[9])
        if not _CODE.match(code) or isins is None:
            continue
        codes.setdefault(code, []).append(AmfiMasterEtfScheme(
            code, norm_space(row[0]), norm_space(row[2]), norm_space(row[5]), norm_space(row[4]), isins,
            _date(row[7]), _date(row[8])))
    by_code, ambiguous_codes, ambiguous_names = {}, set(), set()
    for code, items in codes.items():
        if len(set(items)) > 1:
            ambiguous_codes.add(code)
            ambiguous_names.update((i.amc, i.scheme_name.casefold()) for i in items)
        else:
            by_code[code] = items[0]
    owners: dict[str, set[str]] = {}
    for s in by_code.values():
        for i in s.isins:
            owners.setdefault(i, set()).add(s.scheme_code)
    ambiguous_isins = {i for i, o in owners.items() if len(o) > 1}
    by_isin = {i: by_code[next(iter(o))] for i, o in owners.items() if i not in ambiguous_isins}
    return AmfiSchemeMasterIndex(by_code, by_isin, ambiguous_codes, ambiguous_isins, ambiguous_names, rows, etf_rows)
