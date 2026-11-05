from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID
import pytest
from app.etf_amfi_navall import AmfiEtfScheme, AmfiNavAllError, fund_identity_for, parse_amfi_navall
from app.etf_evidence import EtfAcquisitionOutcome, EtfMetric

TEXT = (Path(__file__).parent / "fixtures" / "amfi_navall_excerpt.txt").read_bytes().decode("utf-8")
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
    text = TEXT.replace("SBI Mutual Fund\r\n \r\n121109", "999999;INF204KB14I2;-;Other ETF;Direct Plan;;1.0;05-Nov-2026\r\nSBI Mutual Fund\r\n \r\n121109")
    index = parse_amfi_navall(text)
    assert "INF204KB14I2" in index.ambiguous_isins and "INF204KB14I2" not in index.by_isin
    evidence, attempt = fund_identity_for(index, isin="INF204KB14I2", instrument_id=ID, retrieved_at=NOW)
    assert evidence == [] and attempt.reason == "AMFI_ISIN_AMBIGUOUS"


@pytest.mark.parametrize("mutate", [
    lambda t: t.replace("Scheme Code;", "Code;"),
    lambda t: t.replace("140084;INF204KB14I2;-;Nippon", "140084;INF204KB14I2;Nippon"),
    lambda t: t.replace("INF204KB14I2", "XX204KB14I2"),
    lambda t: t.replace("Nippon India Mutual Fund\r\n \r\n140084", "140084"),
])
def test_malformed_files_are_rejected_not_guessed(mutate):
    with pytest.raises(AmfiNavAllError):
        parse_amfi_navall(mutate(TEXT))


def test_name_never_identifies_an_etf():
    index = parse_amfi_navall(TEXT)
    evidence, attempt = fund_identity_for(index, isin="INF999NAMEMATCH0"[:12], instrument_id=ID, retrieved_at=NOW)
    assert evidence == [] and attempt.outcome == EtfAcquisitionOutcome.SUCCESS_EMPTY
