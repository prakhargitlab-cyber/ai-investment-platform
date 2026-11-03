"""Nippon identity preflight must not invent an ISIN from an AMC heading."""
from dataclasses import replace
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from app.etf_evidence import EtfAuthority, EtfListing, EtfProvenance
from app.etf_nippon_factsheet_review import ReviewOnlyEtfMetrics, HEADINGS
from app.etf_nippon_identity_preflight import (
    SchemeIdentityAttestation, preflight_scheme_identity, preflight_shared_review_identities,
)

ID = UUID("11111111-1111-4111-8111-111111111111")
NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
ISIN = "INF204KB14I2"
NSE_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"


class FakeStore:
    def __init__(self, listings):
        self._listings = listings

    def etf_listings(self):
        return self._listings

    def etf_attempts(self, instrument_id):
        return []


def metric():
    return ReviewOnlyEtfMetrics(
        status="DOCUMENT_REVIEW_ONLY", ticker="NIFTYBEES",
        scheme_heading=HEADINGS["NIFTYBEES"], pdf_page=207,
        report_date=date(2026, 9, 30), aum_crore=Decimal("65178.85"),
        average_aum_crore=Decimal("66163.18"),
        monthly_average_expense_ratio_percent=Decimal("0.04"),
        tracking_error_1y_percent=Decimal("0.02"),
        tracking_difference_1y_percent=Decimal("-0.02"),
        raw_pdf_sha256="a" * 64,
    )


def attestation(**kwargs):
    values = dict(
        ticker="NIFTYBEES", isin=ISIN,
        scheme_heading=HEADINGS["NIFTYBEES"], nse_instrument_id=ID,
        review_reference="offline-example-review-reference",
        reviewed_source_url=NSE_URL,
    )
    values.update(kwargs)
    return SchemeIdentityAttestation(**values)


def listing(*, isin=ISIN, symbol="NIFTYBEES", observed=NOW, instrument_id=ID,
            authority=EtfAuthority.OFFICIAL_EXCHANGE, source_url=NSE_URL):
    return EtfListing(
        isin=isin, symbol=symbol, name="Nippon India ETF Nifty 50 BeES",
        instrument_id=instrument_id,
        provenance=EtfProvenance(
            provider="NSE", source_type="EXCHANGE_LISTING",
            source_identity="NSE:ETF:INF204KB14I2", source_url=source_url,
            authority=authority, retrieved_at=observed,
            source_mode="REAL",
        ),
    )


def test_fresh_nse_listing_and_explicit_attestation_only_pass_preflight():
    result = preflight_scheme_identity(FakeStore([listing()]), metric(), attestation(), now=NOW)
    assert result.status == "READY_FOR_MANUAL_REVIEW"
    assert result.isin == ISIN
    assert result.nse_listing_source_url == NSE_URL
    assert result.identity_verified_for_scoring is False


@pytest.mark.parametrize("broken,reason", [
    (lambda: FakeStore([]), "SCHEME_IDENTITY_NSE_LISTING_NOT_FRESH"),
    (lambda: FakeStore([listing(observed=NOW-timedelta(days=900))]), "SCHEME_IDENTITY_NSE_LISTING_NOT_FRESH"),
    (lambda: FakeStore([listing(isin="INF999999999")]), "SCHEME_IDENTITY_OFFICIAL_LISTING_CONFLICT"),
    (lambda: FakeStore([listing(symbol="WRONG")]), "SCHEME_IDENTITY_OFFICIAL_LISTING_CONFLICT"),
    (lambda: FakeStore([listing(authority=EtfAuthority.SECONDARY)]), "SCHEME_IDENTITY_NSE_LISTING_NOT_FRESH"),
    (lambda: FakeStore([listing(), listing(instrument_id=UUID(int=20))]), "SCHEME_IDENTITY_OFFICIAL_LISTING_CONFLICT"),
])
def test_missing_stale_wrong_or_conflicting_exchange_identity_rejected(broken, reason):
    with pytest.raises(ValueError, match=reason):
        preflight_scheme_identity(broken(), metric(), attestation(), now=NOW)


@pytest.mark.parametrize("changes,reason", [
    ({"isin": "INF999999999"}, "SCHEME_IDENTITY_OFFICIAL_LISTING_CONFLICT"),
    ({"ticker": "JUNIORBEES"}, "SCHEME_TO_ISIN_REVIEW_NOT_ESTABLISHED"),
    ({"scheme_heading": "Nippon India ETF BSE Sensex"}, "SCHEME_TO_ISIN_REVIEW_NOT_ESTABLISHED"),
    ({"review_reference": ""}, "SCHEME_TO_ISIN_REVIEW_NOT_ESTABLISHED"),
    ({"reviewed_source_url": "https://mf.nipponindiaim.com/scheme.pdf"}, "SCHEME_IDENTITY_REVIEW_SOURCE_NOT_EXCHANGE"),
])
def test_unsubstantiated_attestation_rejected(changes, reason):
    with pytest.raises(ValueError, match=reason):
        preflight_scheme_identity(FakeStore([listing()]), metric(), attestation(**changes), now=NOW)


def test_tampered_review_only_state_cannot_enter_preflight():
    with pytest.raises(ValueError, match="SCHEME_TO_ISIN_REVIEW_NOT_ESTABLISHED"):
        preflight_scheme_identity(FakeStore([listing()]),
                                  replace(metric(), identity_verified=True), attestation(), now=NOW)


def test_shared_review_requires_every_explicit_attestation():
    from types import SimpleNamespace
    shared = SimpleNamespace(status="DOCUMENT_REVIEW_ONLY", identity_verified=False,
                             metrics=(metric(), replace(metric(), ticker="JUNIORBEES",
                               scheme_heading=HEADINGS["JUNIORBEES"])))
    with pytest.raises(ValueError, match="SHARED_SCHEME_IDENTITY_ATTESTATIONS_INCOMPLETE"):
        preflight_shared_review_identities(
            FakeStore([listing()]), shared, {"NIFTYBEES": attestation()}, now=NOW)


def test_shared_review_rejects_duplicate_canonical_identity():
    from types import SimpleNamespace
    shared = SimpleNamespace(status="DOCUMENT_REVIEW_ONLY", identity_verified=False,
                             metrics=(metric(), replace(metric(), ticker="JUNIORBEES",
                               scheme_heading=HEADINGS["JUNIORBEES"])))
    with pytest.raises(ValueError, match="SCHEME_IDENTITY_OFFICIAL_LISTING_CONFLICT"):
        preflight_shared_review_identities(
            FakeStore([listing()]), shared,
            {"NIFTYBEES": attestation(),
             "JUNIORBEES": attestation(ticker="JUNIORBEES",
                scheme_heading=HEADINGS["JUNIORBEES"])}, now=NOW)
