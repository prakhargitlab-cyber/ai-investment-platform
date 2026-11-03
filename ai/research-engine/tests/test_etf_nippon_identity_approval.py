"""Identity approval packets are review-only, never an authorization token."""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from uuid import UUID

import pytest

from app.etf_nippon_identity_approval import (
    IssuerSchemeIdentityEvidence, prepare_identity_review_packet,
    authorize_identity_for_scoring,
)
from app.etf_nippon_identity_preflight import SchemeIdentityPreflight
from app.etf_nippon_factsheet_review import HEADINGS
from app.etf_nippon_persistence_guard import (
    StagedNipponFactsheet, StagedNipponMetric,
)

NOW = datetime.now(timezone.utc) - timedelta(minutes=2)
ISINS = {"NIFTYBEES": "INF204KB14I2", "JUNIORBEES": "INF732E01045"}
UUIDS = {
    "NIFTYBEES": UUID("805e0ebc-ea95-4859-b739-bdfbf5db327e"),
    "JUNIORBEES": UUID("526e78c2-a5af-46e0-af0e-af470ca966ca"),
}
PDF_SHA = "a" * 64
NSE_SHA = "b" * 64
STAGING_ID = "c" * 64
SOURCE_URL = "https://mf.nipponindiaim.com/FundsAndPerformance/identity.pdf"
MONTH_URL = "https://mf.nipponindiaim.com/InvestorServices/FactSheetsDocuments/Nippon-FS-October-2026.pdf"


def staged():
    items = []
    for ticker in ISINS:
        for metric in ("AUM", "EXPENSE_RATIO", "TRACKING_ERROR", "TRACKING_DIFFERENCE"):
            items.append(StagedNipponMetric(
                ticker=ticker, isin=ISINS[ticker], metric=metric, value="0.02",
                unit="PERCENT", report_date=date(2026, 9, 30),
                page=207 if ticker == "NIFTYBEES" else 216,
                source_url=MONTH_URL, pdf_sha256=PDF_SHA,
                nse_csv_sha256=NSE_SHA, nse_csv_row=2 if ticker == "NIFTYBEES" else 3,
                nse_security_name="NIP IND ETF", scheme_heading=HEADINGS[ticker],
            ))
    return StagedNipponFactsheet(
        status="REVIEW_ONLY_NOT_INGESTIBLE", staging_id=STAGING_ID,
        source_url=MONTH_URL, pdf_sha256=PDF_SHA, nse_csv_sha256=NSE_SHA,
        reporting_date=date(2026, 9, 30), metrics=tuple(items),
    )


def preflights():
    return tuple(SchemeIdentityPreflight(
        status="READY_FOR_MANUAL_REVIEW", ticker=ticker, isin=ISINS[ticker],
        instrument_id=UUIDS[ticker], review_reference="review-123",
        nse_listing_source_url="https://nsearchives.nseindia.com/content/equities/eq_etfseclist.csv",
    ) for ticker in ISINS)


def issuer_evidence(ticker, *, body=None, **changes):
    raw = (body if body is not None else
           f"Scheme Name: {HEADINGS[ticker]}\nISIN: {ISINS[ticker]}\n").encode("utf-8")
    fields = dict(
        ticker=ticker, isin=ISINS[ticker], scheme_heading=HEADINGS[ticker],
        source_url=SOURCE_URL, source_sha256=sha256(raw).hexdigest(),
        source_bytes=raw, reviewer_id="reviewer@example.test",
        reviewer_reference="operator-reviewed-external-source",
        reviewed_at=NOW,
    )
    fields.update(changes)
    return IssuerSchemeIdentityEvidence(**fields)


def inputs():
    return tuple(issuer_evidence(ticker) for ticker in ISINS)


def test_packet_binds_both_identities_without_ever_approving_or_writing():
    packet = prepare_identity_review_packet(staged(), preflights(), inputs())
    assert packet.status == "IDENTITY_EVIDENCE_REVIEW_ONLY"
    assert len(packet.records) == 2
    assert {r.instrument_id for r in packet.records} == set(UUIDS.values())
    assert {r.pdf_sha256 for r in packet.records} == {PDF_SHA}
    assert {r.nse_csv_sha256 for r in packet.records} == {NSE_SHA}
    assert all(not r.source_authenticated and not r.human_approval_verified
               and not r.identity_verified_for_scoring
               and not r.eligible_for_etf_fact_persistence for r in packet.records)
    assert not packet.identity_verified_for_scoring
    assert not packet.eligible_for_etf_fact_persistence


@pytest.mark.parametrize("bad", [
    lambda e: replace(e, isin="INF999999999"),
    lambda e: replace(e, scheme_heading="Wrong ETF Scheme"),
    lambda e: replace(e, source_url="https://evil.example/identity.pdf"),
    lambda e: replace(e, source_url="https://mf.nipponindiaim.com.evil.example/identity.pdf"),
    lambda e: replace(e, reviewer_id=""),
    lambda e: replace(e, reviewer_reference=""),
    lambda e: replace(e, reviewed_at=datetime.now(timezone.utc) + timedelta(days=1)),
])
def test_wrong_isin_heading_issuer_host_or_review_rejected(bad):
    evidence = inputs()
    with pytest.raises(ValueError, match="NIPPON_IDENTITY_REVIEW_NOT_ESTABLISHED"):
        prepare_identity_review_packet(staged(), preflights(),
                                       (bad(evidence[0]), evidence[1]))


@pytest.mark.parametrize("body", [
    "Scheme Name: Nippon India ETF Nifty 50 BeES\n",
    "ISIN: INF204KB14I2\n",
    "Scheme Name: Nippon India ETF Nifty 50 BeES\nISIN: INF732E01045\n",
    "Scheme Name: Nippon India ETF Nifty 50 BeES\nISIN: INF204KB14I2\nISIN: INF204KB14I2\n",
    "Scheme Name: Nippon India ETF Nifty 50 BeES\nScheme Name: Another Scheme\nISIN: INF204KB14I2\n",
])
def test_missing_wrong_or_ambiguous_labels_rejected(body):
    with pytest.raises(ValueError, match="NIPPON_IDENTITY_LABELLED_SCHEME_ISIN_MISSING_OR_AMBIGUOUS"):
        prepare_identity_review_packet(
            staged(), preflights(), (issuer_evidence("NIFTYBEES", body=body), inputs()[1]))


def test_wrong_source_bytes_hash_rejected():
    first, second = inputs()
    with pytest.raises(ValueError, match="NIPPON_IDENTITY_SOURCE_HASH_MISMATCH"):
        prepare_identity_review_packet(staged(), preflights(),
            (replace(first, source_bytes=first.source_bytes + b"modified"), second))


def test_missing_one_identity_rejected():
    with pytest.raises(ValueError, match="NIPPON_IDENTITY_REVIEW_SCOPE_INVALID"):
        prepare_identity_review_packet(staged(), preflights(), inputs()[:1])


def test_duplicate_canonical_uuid_rejected():
    first, second = preflights()
    with pytest.raises(ValueError, match="NIPPON_IDENTITY_REVIEW_SCOPE_INVALID"):
        prepare_identity_review_packet(
            staged(), (first, replace(second, instrument_id=first.instrument_id)), inputs())


def test_staging_provenance_tampering_rejected():
    original = staged()
    changed = replace(original, metrics=(
        replace(original.metrics[0], pdf_sha256="d" * 64), *original.metrics[1:]
    ))
    with pytest.raises(ValueError, match="NIPPON_IDENTITY_STAGED_EVIDENCE_CONFLICT"):
        prepare_identity_review_packet(changed, preflights(), inputs())


def test_fresh_listing_preflight_is_not_a_trusted_approval():
    first, second = preflights()
    with pytest.raises(ValueError, match="NIPPON_IDENTITY_REVIEW_NOT_ESTABLISHED"):
        prepare_identity_review_packet(
            staged(), (replace(first, status="OTHER"), second), inputs())


def test_no_scoring_authorization_even_with_review_packet():
    packet = prepare_identity_review_packet(staged(), preflights(), inputs())
    with pytest.raises(ValueError, match="NIPPON_IDENTITY_APPROVAL_NOT_AUTHORIZED"):
        authorize_identity_for_scoring(packet)
