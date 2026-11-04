"""No hardcoded identity mappings; NSE rows come from supplied PDF text."""
from datetime import date
from hashlib import sha256
from unittest.mock import patch

import pytest

from app.sbi_ter_nse_circular_review import (
    extract_nse_scheme_rows, reconcile_sbi_ter_nse_circular,
)
from app.sbi_ter_workbook_adapter import AdapterOutcome, SbiTerAdapterResult

URL = "https://nsearchives.nseindia.com/content/circulars/CML77115.pdf"
PDF = b"%PDF- synthetic bytes"
TER_HASH = "c" * 64


def line(number, old, new, isin, old_symbol, new_symbol):
    return f"{number}     {old}     {new}     {isin}     {old_symbol}     {new_symbol}"


def page(*lines):
    return ("National Stock Exchange of India\n"
            "Download Ref No: NSE/CML/ 77115\n"
            "Subject: Change in scheme name and symbol of ETFs\n"
            "This circular shall be effective from October 30, 2026.\n"
            + "\n".join(lines))


def staged(code="TEST/SCHEME/001", name="SBI Example ETF"):
    return SbiTerAdapterResult(
        code, name, AdapterOutcome.IDENTITY_UNRESOLVED_STAGED_ONLY
    )


def compare(*stages, text=None, effective=date(2026, 11, 4)):
    if text is None:
        text = page(line(8, "Former SBI ETF", "SBI Example ETF",
                         "INF200KA1FS1", "OLDNSE", "NEWNSE"))
    with patch("app.sbi_ter_nse_circular_review._nse_pdf_pages", return_value=(text,)):
        return reconcile_sbi_ter_nse_circular(
            stages, ter_workbook_sha256=TER_HASH,
            nse_pdf_bytes=PDF, claimed_nse_url=URL,
            effective_on=effective,
        )


def test_exact_name_yields_candidate_but_not_fact_authorization():
    result = compare(staged())
    assert len(result.candidate_rows) == 1
    candidate = result.candidate_rows[0]
    assert candidate.isin == "INF200KA1FS1"
    assert candidate.nsdl_scheme_code == "TEST/SCHEME/001"
    assert candidate.new_nse_symbol == "NEWNSE"
    assert candidate.nse_circular_page == 1
    assert candidate.nse_circular_row == 8
    assert candidate.nse_pdf_sha256 == sha256(PDF).hexdigest()
    assert candidate.ter_workbook_sha256 == TER_HASH
    assert not candidate.approved_for_persistence
    assert not result.approved_for_persistence


def test_similar_name_never_matches():
    report = compare(staged(name="Former SBI ETF"))
    assert not report.candidate_rows
    assert report.unmatched_scheme_codes == ("TEST/SCHEME/001",)


def test_case_and_whitespace_only_are_normalized():
    assert len(compare(staged(name=" sbi   example   etf ")).candidate_rows) == 1


def test_duplicate_staged_scheme_names_rejected():
    with pytest.raises(ValueError, match="STAGED_IDENTITY_AMBIGUOUS"):
        compare(staged(), staged(code="ANOTHER"))


def test_persisted_or_resolved_adapter_result_rejected():
    resolved = SbiTerAdapterResult(
        "TEST/SCHEME/001", "SBI Example ETF", AdapterOutcome.INGESTED
    )
    with pytest.raises(ValueError, match="REQUIRES_STAGING_ONLY"):
        compare(resolved)


def test_circular_effective_date_rejected_before_effective():
    with pytest.raises(ValueError, match="NOT_YET_EFFECTIVE"):
        compare(staged(), effective=date(2026, 10, 29))


def test_duplicate_isin_fails_closed():
    text = page(
        line(8, "Former A", "SBI Example ETF", "INF200KA1FS1", "OLDA", "NEWA"),
        line(9, "Former B", "SBI Other ETF", "INF200KA1FS1", "OLDB", "NEWB"),
    )
    with patch("app.sbi_ter_nse_circular_review._nse_pdf_pages", return_value=(text,)):
        with pytest.raises(ValueError, match="DUPLICATE_ROW_OR_ISIN"):
            extract_nse_scheme_rows(PDF, claimed_source_url=URL)


def test_multiline_title_not_reconstructed():
    text = page(
        line(7, "Former A", "SBI Nifty 10 yr Benchmark", "INF200KA1JT1", "OLD", "NEW"),
        "    G-Sec ETF",
    )
    report = compare(staged(name="SBI Nifty 10 yr Benchmark G-Sec ETF"), text=text)
    assert not report.candidate_rows


@pytest.mark.parametrize("url", [
    "https://other.example/a.pdf",
    URL + "?token=1",
    URL.replace("https://", "http://"),
])
def test_wrong_url_rejected(url):
    with pytest.raises(ValueError, match="SOURCE_URL_NOT_REVIEWED"):
        extract_nse_scheme_rows(PDF, claimed_source_url=url)


def test_circular_reference_must_match_actual_text():
    text = page(line(8, "Former A", "SBI Example ETF", "INF200KA1FS1", "OLD", "NEW"))
    with pytest.raises(ValueError, match="REFERENCE_MISMATCH"):
        compare(staged(), text=text.replace("77115", "99999"))


def test_invalid_pdf_is_rejected():
    with pytest.raises(ValueError, match="BYTES_INVALID"):
        extract_nse_scheme_rows(b"not pdf", claimed_source_url=URL)


def test_real_user_circular_optional_fixture():
    from pathlib import Path
    p = Path(__file__).resolve().parents[1] / ".tmp" / "etf-validation" / "CML77115.pdf"
    if not p.exists():
        pytest.skip("Optional local original NSE circular absent")
    rows, digest = extract_nse_scheme_rows(p.read_bytes(), claimed_source_url=URL)
    assert len(digest) == 64
    assert any(row.row_number == 8 and row.scheme_name == "SBI Nifty 50 ETF"
               and row.isin == "INF200KA1FS1" and row.new_symbol == "SBINIFTY50"
               for row in rows)
