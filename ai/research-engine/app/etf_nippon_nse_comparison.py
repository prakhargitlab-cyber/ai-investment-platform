"""Offline cross-source identity comparison for two Nippon ETF factsheet sections.

NSE CSV data supplied by the operator is NOT authenticated by this tool;
the local bytes and exact candidate rows are reported for independent review.
No approval, identity promotion, persistence, or network request occurs here.
"""
from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from hashlib import sha256
from io import StringIO
import re

from app.etf_nippon_factsheet_review import HEADINGS, ReviewOnlyEtfMetrics

EXPECTED_ISINS = {"NIFTYBEES": "INF204KB14I2", "JUNIORBEES": "INF732E01045"}
MAX_EXCHANGE_CSV_BYTES = 10 * 1024 * 1024
SYMBOL_COLUMNS = ("SYMBOL", "SYMBOL NAME", "TRADING SYMBOL", "NSE SYMBOL", "TICKER")
ISIN_COLUMNS = ("ISIN", "ISIN NUMBER", "ISIN NO", "ISIN CODE")
NAME_COLUMNS = ("NAME", "NAME OF COMPANY", "SECURITY NAME", "NAME OF SECURITY", "SCHEME NAME")


def _column(headers: list[str], allowed: tuple[str, ...]) -> str:
    found = [h for h in headers if h.strip().upper() in allowed]
    if len(found) != 1:
        raise ValueError("NSE_IDENTITY_CSV_COLUMN_MISSING_OR_AMBIGUOUS")
    return found[0]


def _name(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


@dataclass(frozen=True)
class NseSchemeIdentityComparison:
    ticker: str
    expected_isin: str
    factsheet_scheme_heading: str
    nse_scheme_name: str | None
    nse_symbol: str | None
    nse_isin: str | None
    nse_row_number: int | None
    nse_csv_sha256: str
    status: str  # MATCH_EXACT_NAME_REVIEW_REQUIRED | NAME_DIFFERS_REVIEW_REQUIRED | ...
    evidence_authenticated: bool = False
    identity_verified_for_scoring: bool = False


def compare_nse_csv_to_factsheet_sections(
    nse_csv_bytes: bytes, metrics: tuple[ReviewOnlyEtfMetrics, ...],
) -> tuple[NseSchemeIdentityComparison, ...]:
    if not isinstance(nse_csv_bytes, bytes) or not 1 <= len(nse_csv_bytes) <= MAX_EXCHANGE_CSV_BYTES:
        raise ValueError("NSE_IDENTITY_CSV_SIZE_INVALID")
    try:
        text = nse_csv_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("NSE_IDENTITY_CSV_ENCODING_INVALID") from exc
    try:
        reader = csv.DictReader(StringIO(text, newline=""))
        headers = reader.fieldnames or []
        if len(set(headers)) != len(headers):
            raise ValueError("NSE_IDENTITY_CSV_COLUMN_MISSING_OR_AMBIGUOUS")
        symbol_col = _column(headers, SYMBOL_COLUMNS)
        isin_col = _column(headers, ISIN_COLUMNS)
        name_col = _column(headers, NAME_COLUMNS)
        rows = list(reader)
        if len(rows) > 100_000 or any(None in row for row in rows):
            raise ValueError("NSE_IDENTITY_CSV_MALFORMED")
    except csv.Error as exc:
        raise ValueError("NSE_IDENTITY_CSV_MALFORMED") from exc
    if not 1 <= len(metrics) <= 2 or len({metric.ticker for metric in metrics}) != len(metrics):
        raise ValueError("NSE_SCHEME_COMPARISON_SCOPE_INVALID")
    digest = sha256(nse_csv_bytes).hexdigest()
    result = []
    for metric in metrics:
        if (metric.ticker not in EXPECTED_ISINS
                or metric.scheme_heading != HEADINGS[metric.ticker]
                or metric.status != "DOCUMENT_REVIEW_ONLY" or metric.identity_verified):
            raise ValueError("NSE_SCHEME_COMPARISON_SCOPE_INVALID")
        isin = EXPECTED_ISINS[metric.ticker]
        matching = [
            (index, row) for index, row in enumerate(rows, start=2)
            if (row[symbol_col] or "").strip().upper() == metric.ticker
               or (row[isin_col] or "").strip().upper() == isin
        ]
        if len(matching) != 1:
            status = "EXCHANGE_ROW_MISSING" if not matching else "EXCHANGE_ROW_AMBIGUOUS"
            chosen = None
        else:
            chosen = matching[0]
            row = chosen[1]
            if (row[symbol_col] or "").strip().upper() != metric.ticker or (
                row[isin_col] or "").strip().upper() != isin:
                status = "EXCHANGE_SYMBOL_ISIN_CONFLICT"
            elif _name(row[name_col] or "") == _name(metric.scheme_heading):
                status = "MATCH_EXACT_NAME_REVIEW_REQUIRED"
            else:
                status = "NAME_DIFFERS_REVIEW_REQUIRED"
        result.append(NseSchemeIdentityComparison(
            ticker=metric.ticker, expected_isin=isin, factsheet_scheme_heading=metric.scheme_heading,
            nse_scheme_name=chosen[1][name_col] if chosen else None,
            nse_symbol=chosen[1][symbol_col] if chosen else None,
            nse_isin=chosen[1][isin_col] if chosen else None,
            nse_row_number=chosen[0] if chosen else None,
            nse_csv_sha256=digest, status=status,
        ))
    return tuple(result)


def review_report(nse_csv_bytes: bytes, metrics: tuple[ReviewOnlyEtfMetrics, ...]) -> dict:
    comparisons = compare_nse_csv_to_factsheet_sections(nse_csv_bytes, metrics)
    return {
        "status": "IDENTITY_CROSS_SOURCE_REVIEW_ONLY",
        "notice": "Local NSE CSV source authenticity and scheme-name equivalence require independent review.",
        "nse_csv_sha256": sha256(nse_csv_bytes).hexdigest(),
        "evidence_authenticated": False,
        "identity_verified_for_scoring": False,
        "schemes": [asdict(item) for item in comparisons],
    }
