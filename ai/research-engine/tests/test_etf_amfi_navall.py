from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID
import pytest
from app.etf_amfi_navall import AmfiEtfScheme, AmfiNavAllError, fund_identity_for, parse_amfi_navall
from app.etf_evidence import EtfAcquisitionOutcome, EtfMetric

import re

_RAW = (Path(__file__).parent / "fixtures" / "amfi_navall_excerpt.txt").read_bytes().decode("utf-8")
# Line-ending independent: a checkout may turn the fixture into LF/CRLF or strip trailing blanks. Whitespace-only separator
# lines are kept as in the real file; the canonical test text is CRLF like the real NAVAll.txt.
TEXT = "\r\n".join(line.rstrip("\r") for line in _RAW.replace("\r\n", "\n").split("\n"))


def mutate(text, pattern, replacement):
    """Regex replacement that FAILS if it changes nothing (a silent no-op would make a negative test vacuous)."""
    result, count = re.subn(pattern, replacement, text, count=1, flags=re.S)
    assert count == 1 and result != text, f"mutation did not apply: {pattern!r}"
    return result
NOW = datetime(2026, 11, 5, 22, 0, tzinfo=timezone.utc)
ID = UUID(int=7)


def test_real_excerpt_matches_only_etf_categories_by_exact_isin():
    index = parse_amfi_navall(TEXT)
    assert index.row_count == 5 and index.etf_row_count == 4
    assert "INF200KB1233" not in index.by_isin            # "...ETF Fund of Fund" under FoF category
    scheme = index.by_isin["INF204KB14I2"]
    assert (scheme.scheme_code, scheme.fund_house) == ("140084", "Nippon India Mutual Fund")
    assert index.by_isin["INF200K01VT2"].fund_house == "SBI Mutual Fund"
    assert index.by_isin["INF204KB17I5"].category == "Other Scheme - Gold ETF"


def test_fund_identity_evidence_and_unmatched_isin():
    index = parse_amfi_navall(TEXT)
    evidence, attempt = fund_identity_for(index, isin=" inf204kb14i2 ", instrument_id=ID, retrieved_at=NOW)
    assert attempt.outcome == EtfAcquisitionOutcome.SUCCESS_WITH_DATA and len(evidence) == 1
    fact = evidence[0]
    assert fact.metric == EtfMetric.AMC_FUND_HOUSE and fact.value == "Nippon India Mutual Fund"
    assert fact.provenance.source_metadata["amfi_scheme_code"] == "140084"
    evidence, attempt = fund_identity_for(index, isin="INF000000000", instrument_id=ID, retrieved_at=NOW)
    assert evidence == [] and attempt.outcome == EtfAcquisitionOutcome.SUCCESS_EMPTY


def test_duplicate_isin_on_different_schemes_is_ambiguous_and_yields_no_evidence():
    text = mutate(TEXT, r"(?=\r\n\s*\r\nSBI Mutual Fund\r\n\s*\r\n121109)",
                  "\r\n999999;INF204KB14I2;-;Other ETF;Direct Plan;;1.0;05-Nov-2026")
    index = parse_amfi_navall(text)
    assert "INF204KB14I2" in index.ambiguous_isins and "INF204KB14I2" not in index.by_isin
    evidence, attempt = fund_identity_for(index, isin="INF204KB14I2", instrument_id=ID, retrieved_at=NOW)
    assert evidence == [] and attempt.reason == "AMFI_ISIN_AMBIGUOUS"


@pytest.mark.parametrize("mutate", [
    lambda t: mutate(t, r"Scheme Code;", "Code;"),
    lambda t: mutate(t, r"140084;INF204KB14I2;-;Nippon", "140084;INF204KB14I2;Nippon"),
    lambda t: mutate(t, r"INF204KB14I2", "XX204KB14I2"),
    lambda t: mutate(t, r"Nippon India Mutual Fund\r\n\s*\r\n(?=140084)", ""),
])
def test_malformed_files_are_rejected_not_guessed(mutate):
    with pytest.raises(AmfiNavAllError):
        parse_amfi_navall(mutate(TEXT))


def test_name_never_identifies_an_etf():
    index = parse_amfi_navall(TEXT)
    evidence, attempt = fund_identity_for(index, isin="INF999NAMEMATCH0"[:12], instrument_id=ID, retrieved_at=NOW)
    assert evidence == [] and attempt.outcome == EtfAcquisitionOutcome.SUCCESS_EMPTY


@pytest.mark.parametrize("eol", ["\r\n", "\n"])
@pytest.mark.parametrize("strip_blank_lines", [False, True])
def test_parse_result_is_independent_of_line_endings_and_blank_line_whitespace(eol, strip_blank_lines):
    lines = TEXT.split("\r\n")
    if strip_blank_lines:
        lines = [line if line.strip() else "" for line in lines]
    index = parse_amfi_navall(eol.join(lines))
    reference = parse_amfi_navall(TEXT)
    assert index.by_isin == reference.by_isin and index.row_count == reference.row_count == 5
    assert index.etf_row_count == 4 and index.ambiguous_isins == set()
