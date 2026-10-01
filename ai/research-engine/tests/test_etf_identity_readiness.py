"""ETF-3.1 identity ownership/freshness regressions; no provider calls."""
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

import app.etf_readiness as readiness
from app.etf_evidence import EtfAcquisitionAttempt, EtfFact, EtfListing, EtfMetric, ETF_MAX_AGE
from test_etf_readiness import ID, NOW, provenance, store


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW
    monkeypatch.setattr(readiness, "datetime", Clock)


def listing(instrument_id=ID, *, age=0, isin="IN0000000001", **updates):
    return EtfListing(instrument_id=instrument_id, isin=isin, symbol="FIXTURE", name="Fixture ETF",
        provenance=provenance(retrieved_at=NOW - timedelta(days=age)), **updates)


def evaluate(store):
    for metric in (EtfMetric.MARKET_PRICE, EtfMetric.TRADING_VOLUME):
        store.save_etf_evidence(EtfFact(instrument_id=ID, metric=metric, value=Decimal("100"),
            as_of_date=NOW.date(), provenance=provenance()))
    statuses, summary = readiness.evaluate_etf_readiness(store, ID)
    return next(status for status in statuses if status.requirement == "IDENTITY"), summary


def test_other_instrument_cannot_satisfy_identity(store):
    store.save_etf_evidence(listing(UUID(int=2)))
    identity, summary = evaluate(store)
    assert identity.status == readiness.EtfReadinessStatus.MISSING
    assert not summary.ready_for_analysis


def test_correct_fresh_listing_satisfies_its_instrument(store):
    store.save_etf_evidence(listing())
    identity, summary = evaluate(store)
    assert identity.status == readiness.EtfReadinessStatus.READY_FRESH
    assert identity.freshness == "FRESH"
    assert identity.as_of_date is None  # retrieval is not an invented effective date
    assert summary.ready_for_analysis


def test_800_day_listing_is_stale_and_blocks(store):
    store.save_etf_evidence(listing(age=800))
    identity, summary = evaluate(store)
    assert identity.status == readiness.EtfReadinessStatus.READY_STALE
    assert identity.freshness == "STALE"
    assert summary.mandatory_stale == 1
    assert not summary.ready_for_analysis


def test_failed_attempt_does_not_refresh_old_identity(store):
    original = listing(age=800)
    store.save_etf_evidence(original)
    store.save_etf_acquisition([], EtfAcquisitionAttempt(instrument_id=ID, metric="IDENTITY", provider="NSE",
        attempted_at=NOW, outcome="TECHNICAL_FAILURE", reason="TIMEOUT"))
    identity, summary = evaluate(store)
    assert identity.status == readiness.EtfReadinessStatus.READY_STALE
    assert not summary.ready_for_analysis
    assert store.etf_listings()[0].provenance.retrieved_at == original.provenance.retrieved_at


def test_unresolved_listing_cannot_satisfy_identity(store):
    store.save_etf_evidence(listing(None))
    identity, summary = evaluate(store)
    assert identity.status == readiness.EtfReadinessStatus.MISSING
    assert not summary.ready_for_analysis


@pytest.mark.parametrize("updates", [{"asset_type": "EQUITY"}, {"exchange": "NYSE"},
    {"symbol": ""}, {"symbol": " FIXTURE "}, {"isin": "invalid"}])
def test_invalid_canonical_listing_is_blocking(store, monkeypatch, updates):
    invalid = listing().model_copy(update=updates)
    monkeypatch.setattr(store, "etf_listings", lambda: [invalid])
    identity, summary = evaluate(store)
    assert identity.status == readiness.EtfReadinessStatus.TECHNICAL_FAILURE
    assert not summary.ready_for_analysis


@pytest.mark.parametrize("reverse", [False, True])
def test_multiple_listings_select_only_owned_identity(store, monkeypatch, reverse):
    rows = [listing(UUID(int=2), isin="IN0000000002"), listing(age=800), listing(None, isin="IN0000000003")]
    monkeypatch.setattr(store, "etf_listings", lambda: list(reversed(rows)) if reverse else rows)
    identity, summary = evaluate(store)
    assert identity.status == readiness.EtfReadinessStatus.READY_STALE
    assert not summary.ready_for_analysis


@pytest.mark.parametrize("reverse", [False, True])
def test_multiple_listings_preserve_correct_fresh_identity(store, monkeypatch, reverse):
    rows = [listing(UUID(int=2), age=800, isin="IN0000000002"), listing()]
    monkeypatch.setattr(store, "etf_listings", lambda: list(reversed(rows)) if reverse else rows)
    identity, summary = evaluate(store)
    assert identity.status == readiness.EtfReadinessStatus.READY_FRESH
    assert summary.ready_for_analysis


def test_conflicting_owned_isins_are_technical_failure(store, monkeypatch):
    monkeypatch.setattr(store, "etf_listings", lambda: [listing(), listing(isin="IN0000000002")])
    identity, summary = evaluate(store)
    assert identity.status == readiness.EtfReadinessStatus.TECHNICAL_FAILURE
    assert not summary.ready_for_analysis


def test_identity_uses_existing_configured_policy(store, monkeypatch):
    monkeypatch.setitem(ETF_MAX_AGE, EtfMetric.IDENTITY, timedelta(days=7))
    store.save_etf_evidence(listing(age=8))
    identity, summary = evaluate(store)
    assert identity.status == readiness.EtfReadinessStatus.READY_STALE
    assert not summary.ready_for_analysis


def test_known_old_publication_not_refreshed_by_retrieval(store):
    item = listing().model_copy(update={"provenance": provenance(published_at=NOW - timedelta(days=800))})
    store.save_etf_evidence(item)
    identity, summary = evaluate(store)
    assert identity.status == readiness.EtfReadinessStatus.READY_STALE
    assert identity.reason == "LISTING_PUBLICATION_DATE"
    assert not summary.ready_for_analysis


def test_identity_attempts_are_also_instrument_scoped(store):
    store.save_etf_acquisition([], EtfAcquisitionAttempt(instrument_id=UUID(int=2), metric="IDENTITY", provider="NSE",
        attempted_at=NOW, outcome="TECHNICAL_FAILURE", reason="OTHER_INSTRUMENT_TIMEOUT"))
    identity, _ = evaluate(store)
    assert identity.status == readiness.EtfReadinessStatus.MISSING
    store.save_etf_acquisition([], EtfAcquisitionAttempt(instrument_id=ID, metric="IDENTITY", provider="NSE",
        attempted_at=NOW, outcome="TECHNICAL_FAILURE", reason="OWN_TIMEOUT"))
    identity, summary = evaluate(store)
    assert identity.status == readiness.EtfReadinessStatus.TECHNICAL_FAILURE
    assert identity.reason == "OWN_TIMEOUT"
    assert not summary.ready_for_analysis
