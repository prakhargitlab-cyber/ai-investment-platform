"""Offline, synthetic canonical preflight cases; no genuine SBI mappings embedded."""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from uuid import UUID

import pytest

from app.etf_evidence import EtfAuthority, EtfListing, EtfProvenance
from app.sbi_ter_canonical_preflight import preflight_sbi_candidates
from app.sbi_ter_nse_circular_review import (
    SbiNseIdentityReviewCandidate, SbiNseReconciliationReport,
)

NOW = datetime(2026, 11, 4, 12, tzinfo=timezone.utc)
ID = UUID("11111111-1111-4111-8111-111111111111")
OTHER = UUID("22222222-2222-4222-8222-222222222222")
ISIN = "INF999999999"  # Synthetic test ISIN, not a real approved mapping
SYMBOL = "TESTETF"
NSE_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"


class Store:
    def __init__(self, listings=()):
        self.listings = list(listings)
        self.attempts = []

    def etf_listings(self):
        return self.listings

    def etf_attempts(self, instrument_id):
        return self.attempts


def candidate(**kw):
    fields = dict(
        nsdl_scheme_code="TEST/SYNTHETIC/001", scheme_name="SBI Example",
        isin=ISIN, old_nse_symbol="OLDTEST", new_nse_symbol=SYMBOL,
        nse_circular_row=1, nse_circular_page=1,
        nse_pdf_sha256="a" * 64, ter_workbook_sha256="b" * 64,
        nse_effective_date=date(2026, 10, 30),
    )
    fields.update(kw)
    return SbiNseIdentityReviewCandidate(**fields)


def report(*rows):
    return SbiNseReconciliationReport(
        nse_pdf_sha256="a" * 64, ter_workbook_sha256="b" * 64,
        circular_reference="NSE/CML/77115", nse_effective_date=date(2026, 10, 30),
        candidate_rows=rows, unmatched_scheme_codes=(),
    )


def canonical(**kw):
    fields = dict(globalInstrumentId=str(ID), assetType="ETF", status="ACTIVE",
                  primaryExchange="NSE", isin=ISIN, primarySymbol=SYMBOL)
    fields.update(kw)
    return fields


def listing(*, isin=ISIN, symbol=SYMBOL, instrument_id=ID, observed=NOW,
            provider="NSE", authority=EtfAuthority.OFFICIAL_EXCHANGE,
            source_url=NSE_URL):
    return EtfListing(
        isin=isin, symbol=symbol, name="SBI Example",
        instrument_id=instrument_id,
        provenance=EtfProvenance(
            provider=provider, source_type="EXCHANGE_LISTING",
            source_identity="NSE:ETF:EXAMPLE", source_url=source_url,
            authority=authority, retrieved_at=observed, source_mode="REAL",
        ),
    )


def run(*, candidates=None, canonical_rows=None, listings=None, now=NOW):
    return preflight_sbi_candidates(
        report(*(candidates if candidates is not None else [candidate()])),
        [canonical()] if canonical_rows is None else canonical_rows,
        Store([listing()] if listings is None else listings),
        now=now,
    )


def result(**kwargs):
    output = run(**kwargs)
    assert not output.approved_for_persistence
    assert all(not row.approved_for_persistence for row in output.rows)
    return output.rows[0]


def test_matching_canonical_and_fresh_official_listing_is_only_review_candidate():
    row = result()
    assert row.status == "VERIFIED_CANONICAL_CANDIDATE"
    assert row.canonical_instrument_id == ID
    assert row.nse_listing_source_url == NSE_URL


def test_absent_canonical_is_missing_not_approved():
    assert result(canonical_rows=[]).status == "MISSING_CANONICAL_IDENTITY"


@pytest.mark.parametrize("rows", [
    [canonical(status="INACTIVE")],
    [canonical(primarySymbol="OLDTEST")],
    [canonical(assetType="EQUITY")],
    [canonical(), canonical(globalInstrumentId=str(OTHER))],
    [canonical(globalInstrumentId="not-uuid")],
    [canonical(), canonical(isin="INF888888888")],
])
def test_bad_canonical_rows_fail_closed(rows):
    assert result(canonical_rows=rows).status == "CONFLICT"


@pytest.mark.parametrize("listings,status", [
    ([], "MISSING_NSE_LISTING"),
    ([listing(isin="INF888888888")], "CONFLICT"),
    ([listing(symbol="SBIOTHER")], "CONFLICT"),
    ([listing(instrument_id=OTHER)], "CONFLICT"),
    ([listing(), listing(instrument_id=OTHER)], "CONFLICT"),
    ([listing(provider="OTHER")], "CONFLICT"),
    ([listing(authority=EtfAuthority.SECONDARY)], "CONFLICT"),
    ([listing(source_url="https://untrusted.example/file.csv")], "CONFLICT"),
    ([listing(observed=NOW - timedelta(days=900))], "NSE_LISTING_NOT_FRESH"),
])
def test_invalid_listing_not_verified(listings, status):
    assert result(listings=listings).status == status


def test_future_circular_effective_date_rejected():
    with pytest.raises(ValueError, match="CANDIDATE_AMBIGUOUS_OR_UNTRUSTED"):
        run(candidates=[candidate(nse_effective_date=date(2026, 12, 1))])


def test_promoted_candidate_rejected():
    with pytest.raises(ValueError, match="CANDIDATE_AMBIGUOUS_OR_UNTRUSTED"):
        run(candidates=[replace(candidate(), approved_for_persistence=True)])


def test_source_hash_conflict_rejected():
    with pytest.raises(ValueError, match="CANDIDATE_AMBIGUOUS_OR_UNTRUSTED"):
        run(candidates=[candidate(nse_pdf_sha256="0" * 64)])


def test_duplicate_candidate_isin_rejected():
    with pytest.raises(ValueError, match="CANDIDATE_AMBIGUOUS_OR_UNTRUSTED"):
        run(candidates=[candidate(), candidate(nsdl_scheme_code="OTHER")])


def test_empty_or_promoted_report_rejected():
    with pytest.raises(ValueError, match="REVIEW_REPORT_INVALID"):
        preflight_sbi_candidates(report(), [canonical()], Store([listing()]), now=NOW)


def test_future_listing_is_not_fresh():
    assert result(listings=[listing(observed=NOW + timedelta(days=10))]).status != "VERIFIED_CANONICAL_CANDIDATE"
