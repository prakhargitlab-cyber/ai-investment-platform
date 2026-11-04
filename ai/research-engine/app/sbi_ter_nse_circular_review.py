"""Review-only correlation of staged SBI TER names with NSE circular rows.

An NSE circular establishes name/ISIN/symbol, not NSDL code/ISIN.
An exact name match across two documents remains an unapproved candidate.
No network, hardcoded crosswalk, resolver construction or persistence.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from hashlib import sha256
from io import BytesIO
import re
from urllib.parse import urlsplit

from pypdf import PdfReader

from app.sbi_ter_workbook_adapter import AdapterOutcome

MAX_NSE_CIRCULAR_BYTES = 4 * 1024 * 1024
MAX_NSE_CIRCULAR_PAGES = 20
MAX_NSE_PAGE_TEXT = 200_000
_ISIN = re.compile(r"[A-Z]{2}[A-Z0-9]{9}[0-9]\\Z")
_SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9._-]*\\Z")
_ROW_START = re.compile(r"^\\s*\\d{1,2}\\s{2,}")
_DATE = re.compile(r"October\\s+30\\s*,\\s*2026", re.I)


@dataclass(frozen=True)
class NseIdentityRow:
    row_number: int
    scheme_name: str
    isin: str
    old_symbol: str
    new_symbol: str
    page_number: int


@dataclass(frozen=True)
class SbiNseIdentityReviewCandidate:
    nsdl_scheme_code: str
    scheme_name: str
    isin: str
    old_nse_symbol: str
    new_nse_symbol: str
    nse_circular_row: int
    nse_circular_page: int
    nse_pdf_sha256: str
    ter_workbook_sha256: str
    nse_effective_date: date
    status: str = "REVIEW_ONLY_EXACT_NAME_JOIN_NOT_CODE_ISIN_PROOF"
    approved_for_persistence: bool = False


@dataclass(frozen=True)
class SbiNseReconciliationReport:
    nse_pdf_sha256: str
    ter_workbook_sha256: str
    circular_reference: str
    nse_effective_date: date
    candidate_rows: tuple[SbiNseIdentityReviewCandidate, ...]
    unmatched_scheme_codes: tuple[str, ...]
    status: str = "REVIEW_ONLY_NO_DIRECT_CODE_ISIN_PROOF"
    approved_for_persistence: bool = False


def _name_key(name: str) -> str:
    # Preserve punctuation and all words. No fuzzy scheme-name equivalence.
    return " ".join(name.casefold().split())


def _nse_pdf_pages(pdf_bytes: bytes) -> tuple[str, ...]:
    if (not isinstance(pdf_bytes, bytes) or not 0 < len(pdf_bytes) <= MAX_NSE_CIRCULAR_BYTES
            or not pdf_bytes.startswith(b"%PDF-")):
        raise ValueError("NSE_CIRCULAR_BYTES_INVALID")
    try:
        reader = PdfReader(BytesIO(pdf_bytes), strict=True)
        if not 1 <= len(reader.pages) <= MAX_NSE_CIRCULAR_PAGES:
            raise ValueError("NSE_CIRCULAR_PAGE_LIMIT")
        pages = []
        for page in reader.pages:
            text = page.extract_text(extraction_mode="layout") or ""
            if len(text) > MAX_NSE_PAGE_TEXT:
                raise ValueError("NSE_CIRCULAR_PAGE_TEXT_LIMIT")
            pages.append(text)
        return tuple(pages)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("NSE_CIRCULAR_PARSE_FAILED") from exc


def extract_nse_scheme_rows(pdf_bytes: bytes, *, claimed_source_url: str) -> tuple[tuple[NseIdentityRow, ...], str]:
    """Read complete single-line table rows from actual PDF bytes.

    The claimed source URL is *not* proof of network origin.
    Multiline table rows are deliberately not reconstructed.
    """
    try:
        url = urlsplit(claimed_source_url)
        valid = (url.scheme == "https" and url.hostname == "nsearchives.nseindia.com"
                 and not url.username and not url.password and url.port in (None, 443)
                 and not url.query and not url.fragment
                 and url.path == "/content/circulars/CML77115.pdf")
    except (ValueError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise ValueError("NSE_CIRCULAR_SOURCE_URL_NOT_REVIEWED")
    pages = _nse_pdf_pages(pdf_bytes)
    combined = "\n".join(pages)
    if not re.search(r"Download Ref No:\\s*NSE/CML/\\s*77115", combined):
        raise ValueError("NSE_CIRCULAR_REFERENCE_MISMATCH")
    if not re.search(r"Change in scheme\\s+name and symbol", combined, re.I):
        raise ValueError("NSE_CIRCULAR_SUBJECT_MISMATCH")
    if not _DATE.search(combined):
        raise ValueError("NSE_CIRCULAR_EFFECTIVE_DATE_MISSING")
    rows = []
    used_numbers, used_isins, used_names, used_symbols = set(), set(), set(), set()
    for page_number, text in enumerate(pages, start=1):
        for line in text.splitlines():
            if not _ROW_START.match(line):
                continue
            fields = re.split(r"\\s{2,}", line.strip())
            if len(fields) != 6:
                raise ValueError("NSE_CIRCULAR_TABLE_ROW_LAYOUT_UNSUPPORTED")
            number, _old_name, new_name, isin, old_symbol, new_symbol = fields
            if not (number.isdecimal() and _ISIN.fullmatch(isin)
                    and _SYMBOL.fullmatch(old_symbol)
                    and (new_symbol == "-" or _SYMBOL.fullmatch(new_symbol))):
                raise ValueError("NSE_CIRCULAR_TABLE_ROW_INVALID")
            if number in used_numbers or isin in used_isins:
                raise ValueError("NSE_CIRCULAR_DUPLICATE_ROW_OR_ISIN")
            used_numbers.add(number)
            used_isins.add(isin)
            if new_name == "-" or new_symbol == "-":
                continue
            name = _name_key(new_name)
            if name in used_names or new_symbol in used_symbols:
                raise ValueError("NSE_CIRCULAR_DUPLICATE_SCHEME_OR_SYMBOL")
            used_names.add(name)
            used_symbols.add(new_symbol)
            rows.append(NseIdentityRow(
                int(number), new_name, isin, old_symbol, new_symbol, page_number))
    if not rows:
        raise ValueError("NSE_CIRCULAR_NO_COMPLETE_IDENTITY_ROWS")
    return tuple(rows), sha256(pdf_bytes).hexdigest()


def reconcile_sbi_ter_nse_circular(
    staged_rows, *, ter_workbook_sha256: str,
    nse_pdf_bytes: bytes, claimed_nse_url: str, effective_on: date,
) -> SbiNseReconciliationReport:
    """Return source-hash-bound, non-authorizing exact-name candidates."""
    if not re.fullmatch(r"[a-f0-9]{64}", ter_workbook_sha256):
        raise ValueError("SBI_TER_SOURCE_HASH_INVALID")
    if not isinstance(effective_on, date) or effective_on < date(2026, 10, 30):
        raise ValueError("NSE_CIRCULAR_NOT_YET_EFFECTIVE")
    nse_rows, nse_hash = extract_nse_scheme_rows(
        nse_pdf_bytes, claimed_source_url=claimed_nse_url)
    by_name = {_name_key(row.scheme_name): row for row in nse_rows}
    seen_codes, seen_names = set(), set()
    candidates, unmatched = [], []
    for item in staged_rows:
        if (item.outcome != AdapterOutcome.IDENTITY_UNRESOLVED_STAGED_ONLY
                or item.instrument_id is not None or item.fact is not None):
            raise ValueError("SBI_TER_CANDIDATE_REQUIRES_STAGING_ONLY")
        code, name = item.scheme_code, item.scheme_name
        if (not isinstance(code, str) or not code.strip()
                or not isinstance(name, str) or not name.strip()):
            raise ValueError("SBI_TER_STAGED_IDENTITY_INVALID")
        key = _name_key(name)
        if code in seen_codes or key in seen_names:
            raise ValueError("SBI_TER_STAGED_IDENTITY_AMBIGUOUS")
        seen_codes.add(code)
        seen_names.add(key)
        row = by_name.get(key)
        if row is None:
            unmatched.append(code)
            continue
        candidates.append(SbiNseIdentityReviewCandidate(
            nsdl_scheme_code=code, scheme_name=name, isin=row.isin,
            old_nse_symbol=row.old_symbol, new_nse_symbol=row.new_symbol,
            nse_circular_row=row.row_number, nse_circular_page=row.page_number,
            nse_pdf_sha256=nse_hash, ter_workbook_sha256=ter_workbook_sha256,
            nse_effective_date=date(2026, 10, 30)))
    return SbiNseReconciliationReport(
        nse_pdf_sha256=nse_hash, ter_workbook_sha256=ter_workbook_sha256,
        circular_reference="NSE/CML/77115", nse_effective_date=date(2026, 10, 30),
        candidate_rows=tuple(sorted(candidates, key=lambda row: row.nsdl_scheme_code)),
        unmatched_scheme_codes=tuple(sorted(unmatched)),
    )
