"""Inspect an original SBI PDF for *review-only* scheme-code/ISIN candidates.

Unlike the TER workbook, a different, authentic issuer document must
explicitly print the NSDL scheme code AND ISIN in the same unambiguous
scope. This module can inspect caller-supplied original PDF bytes, but it
DOES NOT authenticate their source, approve any identity, fetch URLs,
construct a ReviewedSbiSchemeIdentity, or write financial facts.

No name-based matches, adjacent-page joins, multi-pair page guessing,
or document URL inference are allowed. Candidate status cannot be
interpreted as permission to ingest TER into production.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from itertools import islice
import re
from urllib.parse import urlsplit

from pypdf import PdfReader

MAX_IDENTITY_PDF_BYTES = 8 * 1024 * 1024
MAX_IDENTITY_PDF_PAGES = 100
MAX_PAGE_TEXT_CHARS = 100_000

# Strict, entire-line labels only; no positional pairing by fund name.
_CODE_LINE = re.compile(r"(?im)^\s*NSDL\s+Scheme\s+Code\s*[:\-]\s*([A-Za-z0-9][A-Za-z0-9/._ -]{0,99})\s*$")
_ISIN_LINE = re.compile(r"(?im)^\s*(?:ISIN|ISIN\s+Number)\s*[:\-]\s*([A-Za-z0-9]{12})\s*$")
_ISIN_FORMAT = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
_ALLOWED_HOSTS = frozenset({"www.sbimf.com", "sbimf.com"})


@dataclass(frozen=True)
class SbiSchemeIdentityCandidate:
    scheme_code: str
    isin: str
    document_url: str
    raw_pdf_sha256: str
    page_number: int
    status: str = "DOCUMENT_REVIEW_ONLY_SOURCE_NOT_AUTHENTICATED"
    eligible_for_fact_persistence: bool = False


@dataclass(frozen=True)
class SbiSchemeIdentityDocumentInspection:
    document_url: str
    raw_pdf_sha256: str
    pages_scanned: int
    candidates: tuple[SbiSchemeIdentityCandidate, ...]
    status: str = "DOCUMENT_REVIEW_ONLY"
    identity_authenticated: bool = False
    eligible_for_fact_persistence: bool = False


def _validate_source_url(source_url: str):
    try:
        value = urlsplit(source_url)
        if (value.scheme != "https" or value.hostname not in _ALLOWED_HOSTS
                or value.username is not None or value.password is not None
                or value.port not in (None, 443) or value.query or value.fragment
                or "\\" in source_url or any(c.isspace() for c in source_url)
                or not value.path.lower().endswith(".pdf")):
            raise ValueError("SBI_IDENTITY_DOCUMENT_URL_NOT_ISSUER_PDF")
    except (TypeError, AttributeError, ValueError) as exc:
        raise ValueError("SBI_IDENTITY_DOCUMENT_URL_NOT_ISSUER_PDF") from exc


def inspect_labelled_identity_pages(page_texts, *, source_url: str, raw_pdf_sha256: str):
    """Check explicit labels on one page. Ambiguous pages fail closed."""
    _validate_source_url(source_url)
    if not re.fullmatch(r"[0-9a-f]{64}", raw_pdf_sha256):
        raise ValueError("SBI_IDENTITY_ORIGINAL_PDF_HASH_INVALID")
    # Do not materialize an unbounded caller-supplied iterator.\n    texts = list(islice(page_texts, MAX_IDENTITY_PDF_PAGES + 1))
    if not texts or len(texts) > MAX_IDENTITY_PDF_PAGES:
        raise ValueError("SBI_IDENTITY_DOCUMENT_PAGE_LIMIT")
    candidates = []
    seen_codes = set()
    seen_isins = set()
    for number, text in enumerate(texts, start=1):
        if not isinstance(text, str) or len(text) > MAX_PAGE_TEXT_CHARS:
            raise ValueError("SBI_IDENTITY_PAGE_TEXT_UNSUPPORTED")
        codes = [match.strip() for match in _CODE_LINE.findall(text)]
        isins = [match.upper() for match in _ISIN_LINE.findall(text)]
        # A page with one labelled identifier but not the other is not
        # evidence of a cross-document association. More than one on a
        # page is ambiguous, even if the identifiers are repeated.
        if not codes and not isins:
            continue
        if len(codes) != 1 or len(isins) != 1:
            continue
        code, isin = codes[0], isins[0]
        if not code or not _ISIN_FORMAT.fullmatch(isin):
            continue
        if code in seen_codes or isin in seen_isins:
            raise ValueError("SBI_IDENTITY_DOCUMENT_DUPLICATE_OR_CONFLICTING_IDENTITY")
        seen_codes.add(code)
        seen_isins.add(isin)
        candidates.append(SbiSchemeIdentityCandidate(
            scheme_code=code, isin=isin, document_url=source_url,
            raw_pdf_sha256=raw_pdf_sha256, page_number=number,
        ))
    return tuple(candidates)


def inspect_sbi_identity_pdf(original_pdf_bytes: bytes, *, source_url: str):
    """Bounded offline inspection of original PDF bytes; no source approval."""
    _validate_source_url(source_url)
    if (not isinstance(original_pdf_bytes, bytes)
            or not 0 < len(original_pdf_bytes) <= MAX_IDENTITY_PDF_BYTES
            or not original_pdf_bytes.startswith(b"%PDF-")):
        raise ValueError("SBI_IDENTITY_PDF_BYTES_INVALID_OR_OVERSIZED")
    digest = sha256(original_pdf_bytes).hexdigest()
    try:
        pdf = PdfReader(BytesIO(original_pdf_bytes), strict=True)
        if not 1 <= len(pdf.pages) <= MAX_IDENTITY_PDF_PAGES:
            raise ValueError("SBI_IDENTITY_DOCUMENT_PAGE_LIMIT")
        texts = []
        for page in pdf.pages:
            text = page.extract_text() or ""
            if len(text) > MAX_PAGE_TEXT_CHARS:
                raise ValueError("SBI_IDENTITY_PAGE_TEXT_UNSUPPORTED")
            texts.append(text)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("SBI_IDENTITY_PDF_PARSE_FAILED") from exc
    candidates = inspect_labelled_identity_pages(
        texts, source_url=source_url, raw_pdf_sha256=digest
    )
    return SbiSchemeIdentityDocumentInspection(
        document_url=source_url, raw_pdf_sha256=digest,
        pages_scanned=len(texts), candidates=candidates,
    )
