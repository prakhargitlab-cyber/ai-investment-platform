"""Synthetic unit fixtures only; no fixture is actual BSE trading evidence.

Some fabricated fixtures use REAL to exercise validation at the live boundary.
They are never fetched from an exchange or saved to a database. The supported
offline adapter's DEMO/synthetic evidence must remain blocked by readiness.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from app.etf_evidence import EtfFact, EtfListing, EtfMetric, EtfProvenance
from app.etf_readiness import EtfReadinessStatus, evaluate_etf_readiness


NOW = datetime(2026, 11, 4, 12, tzinfo=timezone.utc)
ID = UUID(int=991)
ISIN = "IN0000000001"


def _provenance(provider="NSE", **updates):
    values = dict(provider=provider, source_type="SYNTHETIC_UNIT_TEST",
                  source_identity="synthetic-unit-test", source_url="https://www.nseindia.com/",
                  retrieved_at=NOW, authority="OFFICIAL_EXCHANGE")
    return EtfProvenance(**(values | updates))


def _synthetic_contract_pair():
    metadata = dict(exchange="BSE", security_code="500001", isin=ISIN,
                    currency="INR", instrument_type="ETF", identity_verified=True,
                    session_date="2026-11-04", session_timestamp="2026-11-04T15:30:00+05:30",
                    last_trade_timestamp="2026-11-04T15:29:00+05:30",
                    price_type="EXECUTED_TRADE", volume_type="EXECUTED_QUANTITY")
    provenance = _provenance("BSE", source_type="BSE_VERIFIED_ETF_SESSION",
                             source_identity="synthetic-bse-security-session",
                             source_url="https://www.bseindia.com/", source_metadata=metadata)
    return [EtfFact(instrument_id=ID, metric=metric, value=value, unit=unit,
                    as_of_date=NOW.date(), provenance=provenance.model_copy(deep=True))
            for metric, value, unit in ((EtfMetric.MARKET_PRICE, "101.25", "INR"),
                                       (EtfMetric.TRADING_VOLUME, "20", "shares"))]


class _ReadOnlyStore:
    def __init__(self, facts):
        self.facts = facts
        self.listing = EtfListing(instrument_id=ID, isin=ISIN, symbol="SYNTHETICETF",
                                  name="Synthetic ETF test identity", provenance=_provenance())

    def etf_facts(self, instrument_id, metric=None):
        return [fact for fact in self.facts if fact.instrument_id == instrument_id and fact.metric == metric]

    def etf_listings(self):
        return [self.listing]

    def etf_attempts(self, instrument_id):
        return []

    def etf_navs(self, instrument_id):
        return []

    def etf_holdings(self, instrument_id):
        return []

    def load_daily_market_bars(self, instrument_ids):
        return []


def _evaluate(facts, *, now=NOW):
    statuses, summary = evaluate_etf_readiness(_ReadOnlyStore(facts), ID, now=now)
    return {status.requirement: status for status in statuses}, summary


def _assert_blocked(facts, reason=None, *, now=NOW):
    statuses, summary = _evaluate(facts, now=now)
    assert not summary.ready_for_analysis
    assert statuses["MARKET_PRICE"].status == EtfReadinessStatus.TECHNICAL_FAILURE
    assert statuses["TRADING_VOLUME"].status == EtfReadinessStatus.TECHNICAL_FAILURE
    if reason is not None:
        assert statuses["MARKET_PRICE"].reason == reason


def test_valid_pair_shape_cannot_approve_an_unverified_live_feed():
    statuses, summary = _evaluate(_synthetic_contract_pair())
    assert not summary.ready_for_analysis
    assert statuses["MARKET_PRICE"].reason == "BSE_LIVE_SOURCE_CONTRACT_UNVERIFIED"
    assert statuses["MARKET_PRICE"].provider == "BSE"
    assert statuses["TRADING_VOLUME"].provider == "BSE"


@pytest.mark.parametrize("marker", ["lowercase", "XBOM", "xbom"])
def test_bse_case_and_mic_markers_cannot_bypass_strict_validation(marker):
    facts = _synthetic_contract_pair()
    for fact in facts:
        if marker == "lowercase":
            fact.provenance.provider = "bse"
            fact.provenance.source_type = "bse_verified_etf_session"
            fact.provenance.source_metadata["exchange"] = "bse"
        else:
            fact.provenance.provider = "NSE"
            fact.provenance.source_type = "SYNTHETIC_UNIT_TEST"
            fact.provenance.source_metadata["exchange"] = marker
    _assert_blocked(facts, "BSE_MARKET_PAIR_PROVIDER_MISMATCH")


@pytest.mark.parametrize("bse_side", [0, 1])
def test_bse_cannot_pair_with_nse_evidence(bse_side):
    facts = _synthetic_contract_pair()
    facts[1 - bse_side].provenance = _provenance("NSE")
    _assert_blocked(facts, "BSE_MARKET_PAIR_PROVIDER_MISMATCH")


@pytest.mark.parametrize("key,value", [
    ("security_code", "500002"), ("isin", "IN0000000002"),
    ("exchange", "NSE"), ("session_date", "2026-11-03"),
    ("session_timestamp", "2026-11-04T15:29:00+05:30"),
])
def test_bse_pair_cannot_mix_security_session_or_venue(key, value):
    facts = _synthetic_contract_pair()
    facts[1].provenance.source_metadata[key] = value
    _assert_blocked(facts)


def test_bse_price_and_volume_from_distinct_fresh_sessions_cannot_pair():
    facts = _synthetic_contract_pair()
    early_now = NOW.replace(hour=9)
    for fact in facts:
        fact.provenance.retrieved_at = early_now
    facts[0].provenance.source_metadata.update(
        session_timestamp="2026-11-04T14:00:00+05:30",
        last_trade_timestamp="2026-11-04T13:59:00+05:30")
    facts[1].as_of_date -= timedelta(days=1)
    facts[1].provenance.source_metadata.update(
        session_date="2026-11-03", session_timestamp="2026-11-03T14:30:00+05:30",
        last_trade_timestamp="2026-11-03T14:30:00+05:30")
    _assert_blocked(facts, "BSE_MARKET_PAIR_SECURITY_OR_SESSION_MISMATCH", now=early_now)


@pytest.mark.parametrize("timestamp", [None, "", "invalid", "2026-11-04T15:30:00",
                                        "2026-11-05T15:30:00+05:30",
                                        "2026-11-04T19:30:00+05:30"])
def test_bse_missing_invalid_naive_or_future_timestamp_blocks(timestamp):
    facts = _synthetic_contract_pair()
    for fact in facts:
        if timestamp is None:
            fact.provenance.source_metadata.pop("session_timestamp")
        else:
            fact.provenance.source_metadata["session_timestamp"] = timestamp
    _assert_blocked(facts)


def test_bse_stale_session_cannot_become_ready_by_recent_retrieval():
    facts = _synthetic_contract_pair()
    for fact in facts:
        fact.as_of_date -= timedelta(days=3)
        fact.provenance.source_metadata.update(
            session_date="2026-11-01", session_timestamp="2026-11-01T15:30:00+05:30",
            last_trade_timestamp="2026-11-01T15:29:00+05:30")
    _assert_blocked(facts, "BSE_MARKET_SESSION_STALE")


@pytest.mark.parametrize("session,trade", [
    ("2026-11-03T15:30:00+05:30", "2026-11-03T15:29:00+05:30"),
    ("2026-11-03T18:30:00+05:30", "2026-11-03T15:29:00+05:30"),
])
def test_bse_session_and_trade_ages_cannot_exceed_24_hours(session, trade):
    facts = _synthetic_contract_pair()
    for fact in facts:
        fact.as_of_date -= timedelta(days=1)
        fact.provenance.source_metadata.update(
            session_date="2026-11-03", session_timestamp=session, last_trade_timestamp=trade)
    _assert_blocked(facts, "BSE_MARKET_SESSION_STALE")


def test_bse_future_retrieval_timestamp_is_rejected():
    facts = _synthetic_contract_pair()
    for fact in facts:
        fact.provenance.retrieved_at = NOW + timedelta(seconds=1)
    _assert_blocked(facts, "BSE_MARKET_SESSION_INVALID")


@pytest.mark.parametrize("source_mode,extra", [
    ("DEMO", {}), ("REAL", {"synthetic": True}), ("REAL", {"fixture_kind": "SYNTHETIC"}),
])
def test_synthetic_bse_evidence_is_never_ready_even_if_store_returns_it(source_mode, extra):
    facts = _synthetic_contract_pair()
    for fact in facts:
        fact.provenance.source_mode = source_mode
        fact.provenance.source_metadata.update(extra)
    _assert_blocked(facts, "BSE_SYNTHETIC_MARKET_EVIDENCE")


@pytest.mark.parametrize("timestamp", [None, "invalid", "2026-11-04T15:29:00",
                                        "2026-11-03T15:29:00+05:30",
                                        "2026-11-04T15:31:00+05:30"])
def test_bse_missing_stale_or_invalid_actual_trade_time_cannot_be_revived(timestamp):
    facts = _synthetic_contract_pair()
    for fact in facts:
        if timestamp is None:
            fact.provenance.source_metadata.pop("last_trade_timestamp")
        else:
            fact.provenance.source_metadata["last_trade_timestamp"] = timestamp
    _assert_blocked(facts)


@pytest.mark.parametrize("updates", [
    {"authority": "SECONDARY"}, {"authority": "OFFICIAL_FUND"},
    {"source_url": "https://example.org/bseindia.com"},
    {"source_url": "https://bseindia.com.example.org/"},
    {"source_url": "http://www.bseindia.com/"},
    {"source_url": "https://api.bseindia.com/"},
    {"source_url": "https://www.bseindia.com:8443/"},
    {"source_url": "https://user:password@www.bseindia.com/"},
    {"source_type": "UNVERIFIED_BSE_QUOTE"},
])
def test_bse_requires_official_exchange_provenance(updates):
    facts = _synthetic_contract_pair()
    for fact in facts:
        fact.provenance = fact.provenance.model_copy(update=updates)
    _assert_blocked(facts, "BSE_MARKET_PROVENANCE_UNVERIFIED")


@pytest.mark.parametrize("key,value", [
    ("identity_verified", False), ("currency", "USD"), ("instrument_type", "EQUITY"),
    ("security_code", ""), ("security_code", "12345"), ("security_code", "５００００１"),
    ("price_type", "NAV"), ("price_type", "INAV"), ("price_type", "PREVIOUS_CLOSE"),
    ("price_type", "LAST_PRICE"), ("volume_type", "QUOTED_QUANTITY"),
])
def test_bse_identity_currency_and_executed_trade_contract_required(key, value):
    facts = _synthetic_contract_pair()
    for fact in facts:
        fact.provenance.source_metadata[key] = value
    _assert_blocked(facts)


@pytest.mark.parametrize("index,value", [(0, Decimal("0")), (1, Decimal("0")),
                                          (1, Decimal("0.5"))])
def test_bse_requires_positive_price_and_positive_integer_executed_volume(index, value):
    facts = _synthetic_contract_pair()
    facts[index].value = value
    _assert_blocked(facts, "BSE_EXECUTED_TRADE_REQUIRED")


@pytest.mark.parametrize("missing_index", [0, 1])
def test_bse_absent_price_or_volume_cannot_be_ready(missing_index):
    facts = _synthetic_contract_pair()
    facts.pop(missing_index)
    _assert_blocked(facts, "BSE_MARKET_PAIR_INCOMPLETE")


def test_bse_identity_must_match_owned_canonical_nse_listing_isin():
    facts = _synthetic_contract_pair()
    for fact in facts:
        fact.provenance.source_metadata["isin"] = "IN0000000002"
    _assert_blocked(facts, "BSE_MARKET_CANONICAL_ISIN_MISMATCH")


@pytest.mark.parametrize("field,value", [
    ("source_identity", "another-synthetic-session"),
    ("source_url", "https://bseindia.com/"),
])
def test_bse_pair_must_share_source_identity_and_source_url(field, value):
    facts = _synthetic_contract_pair()
    setattr(facts[1].provenance, field, value)
    _assert_blocked(facts, "BSE_MARKET_PAIR_SOURCE_MISMATCH")


def test_existing_nse_zero_volume_and_metric_specific_identities_remain_valid():
    facts = _synthetic_contract_pair()
    for fact in facts:
        fact.provenance = _provenance(source_identity=f"synthetic-nse:{fact.metric}", source_metadata={
            "session": {"timestamp": "04-Nov-2026 15:30:00"},
            "record": {"symbol": "SYNTHETICETF", "series": "EQ", "identifier": "SYNTHETICETFEQN"}})
    facts[1].value = Decimal("0")
    _, summary = _evaluate(facts)
    assert summary.ready_for_analysis


@pytest.mark.parametrize("change", ["provider", "date", "session", "security"])
def test_other_fresh_market_pairs_cannot_mix_provider_date_session_or_security(change):
    facts = _synthetic_contract_pair()
    for fact in facts:
        fact.provenance = _provenance(source_metadata={
            "session": {"timestamp": "04-Nov-2026 15:30:00"},
            "record": {"symbol": "SYNTHETICETF", "series": "EQ", "identifier": "SYNTHETICETFEQN"}})
    if change == "provider":
        facts[1].provenance.provider = "YAHOO_FINANCE"
    elif change == "date":
        facts[1].as_of_date -= timedelta(days=1)
    elif change == "session":
        facts[1].provenance.source_metadata["session"]["timestamp"] = "04-Nov-2026 15:29:00"
    else:
        facts[1].provenance.source_metadata["record"]["identifier"] = "SYNTHETICETFRLN"
    _assert_blocked(facts)
