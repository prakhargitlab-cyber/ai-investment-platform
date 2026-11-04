"""SBI source inspection never converts a PDF candidate into trusted ETF facts."""
from hashlib import sha256

import pytest

from app.sbi_ter_identity_document import (
    inspect_labelled_identity_pages,
    inspect_sbi_identity_pdf,
)

SOURCE = "https://www.sbimf.com/docs/identity-example.pdf"
HASH = "a" * 64


def scan(*pages, url=SOURCE):
    return inspect_labelled_identity_pages(pages, source_url=url, raw_pdf_sha256=HASH)


def test_explicit_code_and_isin_same_page_are_review_only():
    candidates = scan("NSDL Scheme Code: 110 / ETF\nISIN: INF200A01026")
    assert len(candidates) == 1
    assert candidates[0].scheme_code == "110 / ETF"
    assert candidates[0].isin == "INF200A01026"
    assert candidates[0].page_number == 1
    assert candidates[0].raw_pdf_sha256 == HASH
    assert candidates[0].status == "DOCUMENT_REVIEW_ONLY_SOURCE_NOT_AUTHENTICATED"
    assert not candidates[0].eligible_for_fact_persistence


def test_identifiers_on_different_pages_do_not_join():
    assert scan("NSDL Scheme Code: 110 / ETF", "ISIN: INF200A01026") == ()


def test_fund_name_only_is_not_identity_evidence():
    assert scan("SBI Nifty 50 ETF\nSymbol: SETFNIF50\nISIN: INF200A01026") == ()


def test_multiple_isins_or_codes_on_one_page_are_ambiguous():
    assert scan("NSDL Scheme Code: ONE\nISIN: INF200A01026\nISIN: INF200A01034") == ()
    assert scan("NSDL Scheme Code: ONE\nNSDL Scheme Code: TWO\nISIN: INF200A01026") == ()


def test_duplicate_scheme_code_across_pages_fails():
    with pytest.raises(ValueError, match="DUPLICATE_OR_CONFLICTING"):
        scan("NSDL Scheme Code: ONE\nISIN: INF200A01026",
             "NSDL Scheme Code: ONE\nISIN: INF200A01034")


def test_duplicate_isin_across_pages_fails():
    with pytest.raises(ValueError, match="DUPLICATE_OR_CONFLICTING"):
        scan("NSDL Scheme Code: ONE\nISIN: INF200A01026",
             "NSDL Scheme Code: TWO\nISIN: INF200A01026")


@pytest.mark.parametrize("url", [
    "https://other.example/path.pdf",
    "http://www.sbimf.com/file.pdf",
    "https://www.sbimf.com/file.pdf?token=1",
    "https://www.sbimf.com@evil.example/file.pdf",
    "https://www.sbimf.com/file.xlsx",
])
def test_unapproved_source_url_rejected(url):
    with pytest.raises(ValueError, match="URL_NOT_ISSUER_PDF"):
        scan("NSDL Scheme Code: ONE\nISIN: INF200A01026", url=url)


def test_bad_hash_rejected():
    with pytest.raises(ValueError, match="ORIGINAL_PDF_HASH_INVALID"):
        inspect_labelled_identity_pages(
            ["NSDL Scheme Code: ONE\nISIN: INF200A01026"],
            source_url=SOURCE, raw_pdf_sha256="fake"
        )


def test_invalid_pdf_bytes_rejected_without_fetch():
    with pytest.raises(ValueError, match="PDF_BYTES_INVALID"):
        inspect_sbi_identity_pdf(b"not a pdf", source_url=SOURCE)


def test_corrupt_pdf_bytes_do_not_produce_false_candidates():
    with pytest.raises(ValueError, match="PDF_PARSE_FAILED"):
        inspect_sbi_identity_pdf(b"%PDF-corrupt", source_url=SOURCE)


def test_no_evidence_is_explicitly_empty():
    assert scan("AMC scheme factsheet without labelled code or ISIN") == ()
