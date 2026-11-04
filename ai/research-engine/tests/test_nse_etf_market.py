"""Deterministic verified-contract fixtures; no network or running database."""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

import pytest
import httpx

from app.etf_acquisition import parse_nse_etf_list
from app.etf_evidence import EtfMetric, EtfProvenance, evidence_id, etf_freshness
from app.models import EtfResearchProfile
from app.nse_etf_market import evidence, market_snapshot, verify_metadata, numeric, IST

NOW = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
KEY = UUID(int=1)
CANONICAL = {"primarySymbol": "SILVERBEES", "isin": "INF204KC1402", "currency": "INR"}
META = {"symbol": "SILVERBEES", "isin": "INF204KC1402", "isETFSec": "true",
        "isDelisted": "false", "isSuspended": "false", "activeSeries": ["EQ"], "marketType": "N"}
ROW = {"symbol": "SILVERBEES", "series": "EQ", "identifier": "SILVERBEESEQN",
       "lastPrice": 209.8, "totalTradedVolume": 14496035, "nav": 206.2542, "inav": 209.73}
PAYLOAD = {"data": {"data": [ROW], "timestamp": "09-Oct-2026 16:00:00", "navDate": "08-Oct-2026",
                    "totals": {"volume": 14496035}, "aduCount": {"advances": 1},
                    "marketStatus": {"marketStatus": "Closed"}}}
PROVENANCE = EtfProvenance(provider="NSE", source_type="NSE_OFFICIAL_ETF_SECURITY_LIST",
    source_identity="fixture", source_url="https://nsearchives.nseindia.com/fixture",
    authority="OFFICIAL_EXCHANGE", retrieved_at=NOW)
LISTINGS = parse_nse_etf_list("Symbol,SecurityName,ISINNumber\nSILVERBEES,Silver ETF,INF204KC1402\n", PROVENANCE)
PROFILE = EtfResearchProfile(instrument_id=KEY, fund_id=KEY, fund_name="Silver ETF", ticker="SILVERBEES",
                             exchange="NSE", mic="XNSE", isin="INF204KC1402", currency="INR")


def test_session_and_nav_dates_provenance_totals_and_idempotency():
    stamp, rows, envelope, failures = market_snapshot(PAYLOAD, LISTINGS, NOW)
    assert failures == {}
    verify_metadata(META, CANONICAL)
    assert stamp.astimezone(timezone.utc).hour == 10
    price = evidence(PROFILE, EtfMetric.MARKET_PRICE, rows["SILVERBEES"], stamp, envelope, META, NOW)
    nav = evidence(PROFILE, EtfMetric.NAV, ROW, stamp, envelope, META, NOW)
    assert price.value == Decimal("209.8") and price.as_of_date.isoformat() == "2026-10-09"
    assert nav.nav == Decimal("206.2542") and nav.nav_date.isoformat() == "2026-10-08"
    assert price.provenance.source_metadata["session"]["totals"] == PAYLOAD["data"]["totals"]
    assert price.provenance.source_metadata["record"]["inav"] == 209.73
    assert price.provenance.authority == "OFFICIAL_EXCHANGE"
    again = price.model_copy(deep=True)
    again.provenance.retrieved_at = NOW.replace(hour=13)
    assert evidence_id(price) == evidence_id(again)
    assert etf_freshness(price, EtfMetric.MARKET_PRICE, now=NOW.replace(day=11)) == "STALE"


@pytest.mark.parametrize("value", [True, -1, float("inf"), "NaN", "Infinity", "garbage"])
def test_invalid_values_rejected(value):
    with pytest.raises(ValueError):
        numeric(value)


@pytest.mark.parametrize("change,expected_field", [
    ({"isin": "INF000000000"}, "ISIN"),
    ({"symbol": "OTHER"}, "SYMBOL"),
    ({"isETFSec": "false"}, "NOT_ETF"),
    ({"isDelisted": "true"}, "DELISTED"),
    ({"isSuspended": "true"}, "SUSPENDED"),
    ({"activeSeries": []}, "INACTIVE_SERIES"),
    ({"tempSuspendedSeries": ["EQ"]}, "TEMP_SUSPENDED"),
    ({"marketType": "X"}, "MARKET_TYPE"),
])
def test_metadata_cannot_reassign_identity(change, expected_field):
    with pytest.raises(ValueError, match=f"NSE_METADATA_IDENTITY_CONFLICT:{expected_field}$"):
        verify_metadata(META | change, CANONICAL)


@pytest.mark.parametrize("case,expected_code", [
    ("duplicate", "DUPLICATE_MARKET_WATCH_SYMBOL"),
    ("unknown", "UNVERIFIED_MARKET_WATCH_SYMBOL"),
    ("series", "UNSUPPORTED_NSE_LISTING"),
    ("identifier", "UNSUPPORTED_NSE_LISTING"),
    ("future", "FUTURE_NSE_SESSION"),
    ("empty", "EMPTY_NSE_MARKET_SNAPSHOT"),
])
def test_unverified_market_snapshot_rejected(case, expected_code):
    payload = deepcopy(PAYLOAD)
    if case == "duplicate": payload["data"]["data"].append(dict(ROW))
    if case == "unknown": payload["data"]["data"][0]["symbol"] = "OTHER"
    if case == "series": payload["data"]["data"][0]["series"] = "BE"
    if case == "identifier": payload["data"]["data"][0]["identifier"] = "OTHER"
    if case == "future": payload["data"]["timestamp"] = "10-Oct-2026 16:00:00"
    if case == "empty": payload["data"]["data"] = []
    with pytest.raises(ValueError, match=expected_code):
        market_snapshot(payload, LISTINGS, NOW)


def test_undated_market_snapshot_raises():
    """Malformed timestamp raises ValueError from date parsing, not NseContractError."""
    payload = deepcopy(PAYLOAD)
    payload["data"]["timestamp"] = ""
    with pytest.raises(ValueError):
        market_snapshot(payload, LISTINGS, NOW)


def test_unrelated_duplicate_or_unknown_symbol_elsewhere_in_the_shared_feed_does_not_block_a_bounded_request():
    # Root cause of the GOLDBEES PARTIAL_FAILURE that persisted even after
    # the case/whitespace normalization fix: getETFData's parent=all&child=
    # all feed (and the official security-list CSV) cover NSE's ENTIRE ETF
    # universe, but market_snapshot() validated ALL of it for every call,
    # so a totally unrelated ETF's duplicate/unknown-symbol row anywhere in
    # that shared universe aborted the whole snapshot -- failing the
    # bounded, single-instrument request too, even though the requested
    # instrument's own row was perfectly fine. requested_symbols scopes
    # strict uniqueness/format validation to only the instrument(s) actually
    # being ingested; unrelated rows/listings are skipped, never fatal.
    payload = deepcopy(PAYLOAD)
    payload["data"]["data"].append({"symbol": "UNRELATEDETF", "series": "EQ",
        "identifier": "UNRELATEDETFEQN", "lastPrice": 50, "totalTradedVolume": 1000, "nav": 49})
    payload["data"]["data"].append({"symbol": "UNRELATEDETF", "series": "EQ",  # duplicate, unrelated
        "identifier": "UNRELATEDETFEQN", "lastPrice": 51, "totalTradedVolume": 900, "nav": 49.2})
    stamp, rows, envelope, failures = market_snapshot(payload, LISTINGS, NOW, requested_symbols={"SILVERBEES"})
    assert set(rows) == {"SILVERBEES"}
    assert failures == {}  # unrelated symbols are skipped, not failed


@pytest.mark.parametrize("case,expected_code", [
    ("duplicate", "DUPLICATE_MARKET_WATCH_SYMBOL"),
    ("unknown", "UNVERIFIED_MARKET_WATCH_SYMBOL"),
    ("series", "UNSUPPORTED_NSE_LISTING"),
    ("identifier", "UNSUPPORTED_NSE_LISTING"),
])
def test_requested_instruments_still_get_strict_checks_when_scoped(case, expected_code):
    # requested_symbols must never weaken validation for the instrument(s)
    # actually being requested -- only unrelated rows are skipped.
    # Per-instrument failures are now collected in the failures dict rather
    # than raising, so the instrument is excluded from selected rows.
    payload = deepcopy(PAYLOAD)
    if case == "duplicate": payload["data"]["data"].append(dict(ROW))
    if case == "unknown": payload["data"]["data"][0]["symbol"] = "OTHER"
    if case == "series": payload["data"]["data"][0]["series"] = "BE"
    if case == "identifier": payload["data"]["data"][0]["identifier"] = "OTHER"
    failed_symbol = "SILVERBEES" if case != "unknown" else "OTHER"
    requested = {"SILVERBEES"} if case != "unknown" else {"SILVERBEES", "OTHER"}
    stamp, rows, envelope, failures = market_snapshot(payload, LISTINGS, NOW, requested_symbols=requested)
    assert failed_symbol in failures
    assert failures[failed_symbol] == expected_code
    assert failed_symbol not in rows  # failed symbol must not appear in selected


@pytest.mark.parametrize("live_symbol", ["silverbees", " SILVERBEES", "SILVERBEES "])
def test_live_feed_symbol_case_or_whitespace_does_not_block_a_verified_instrument(live_symbol):
    # Root cause of the reported GOLDBEES PARTIAL_FAILURE/NSE_ACQUISITION_FAILED
    # incident (UNVERIFIED_OR_DUPLICATE_MARKET_SYMBOL on all three metrics):
    # the official-list symbol (by_symbol's keys) is normalized to
    # .strip().upper() by parse_nse_etf_list, but the live getETFData
    # market-watch feed's own "symbol" field was compared to that key with
    # no equivalent normalization. NSE maintains the official ETF security
    # list and the live market-watch feed as two separately-sourced
    # endpoints; incidental case/whitespace drift between them for a
    # genuinely verified, unambiguous instrument must not be treated as an
    # unverified or duplicate symbol. A row that still names a different or
    # duplicated symbol (test_unverified_market_snapshot_rejected's
    # "duplicate"/"unknown" cases, above) must keep being rejected -- this
    # test only covers the same canonical symbol, differently cased/padded.
    payload = deepcopy(PAYLOAD)
    payload["data"]["data"][0]["symbol"] = live_symbol
    payload["data"]["data"][0]["identifier"] = "SILVERBEESEQN"
    stamp, rows, envelope, failures = market_snapshot(payload, LISTINGS, NOW)
    assert "SILVERBEES" in rows
    assert failures == {}


def test_metadata_currency_mismatch_reports_currency_field():
    with pytest.raises(ValueError, match="NSE_METADATA_IDENTITY_CONFLICT:CURRENCY$"):
        verify_metadata(META, CANONICAL | {"currency": "USD"})


def test_metadata_multiple_mismatches_reports_all_fields():
    """Multiple simultaneous mismatches must all appear in the error code."""
    bad_meta = META | {"symbol": "OTHER", "isin": "INF000000000", "isDelisted": "true"}
    with pytest.raises(ValueError, match=r"NSE_METADATA_IDENTITY_CONFLICT:SYMBOL\+ISIN\+DELISTED$"):
        verify_metadata(bad_meta, CANONICAL)


def test_zero_volume_does_not_make_carried_price_fresh():
    stamp, _, envelope, _ = market_snapshot(PAYLOAD, LISTINGS, NOW)
    row = ROW | {"totalTradedVolume": 0}
    assert evidence(PROFILE, EtfMetric.MARKET_PRICE, row, stamp, envelope, META, NOW) is None
    assert evidence(PROFILE, EtfMetric.TRADING_VOLUME, row, stamp, envelope, META, NOW).value == 0
    with pytest.raises(ValueError):
        evidence(PROFILE, EtfMetric.TRADING_VOLUME, ROW | {"totalTradedVolume": 0.5}, stamp, envelope, META, NOW)


@pytest.mark.parametrize("raw", [0, 0.0, "0", "0.0", "0.00"])
def test_zero_nav_raises_unavailable_not_invalid(raw):
    """Zero is NSE's 'NAV unavailable today' marker. It must NOT be treated
    as INVALID_NSE_NUMBER (the old behavior), and it must NOT produce an
    EtfNavObservation.  evidence() raises NSE_NAV_ZERO_UNAVAILABLE so the
    caller can record SUCCESS_EMPTY with a distinct audit reason."""
    stamp, _, envelope, _ = market_snapshot(PAYLOAD, LISTINGS, NOW)
    with pytest.raises(ValueError, match="NSE_NAV_ZERO_UNAVAILABLE"):
        evidence(PROFILE, EtfMetric.NAV, ROW | {"nav": raw}, stamp, envelope, META, NOW)


def test_positive_nav_produces_observation():
    stamp, _, envelope, _ = market_snapshot(PAYLOAD, LISTINGS, NOW)
    obs = evidence(PROFILE, EtfMetric.NAV, ROW, stamp, envelope, META, NOW)
    assert obs is not None
    assert obs.nav == Decimal("206.2542")
    assert obs.currency == "INR"


@pytest.mark.parametrize("raw", [None, "", "-"])
def test_absent_nav_returns_none(raw):
    """None, empty string and dash are legitimately absent — no observation."""
    stamp, _, envelope, _ = market_snapshot(PAYLOAD, LISTINGS, NOW)
    assert evidence(PROFILE, EtfMetric.NAV, ROW | {"nav": raw}, stamp, envelope, META, NOW) is None


@pytest.mark.parametrize("raw", ["NaN", -1, True, float("inf"), "garbage"])
def test_malformed_nav_raises_invalid_nse_number(raw):
    """Negative, nonfinite, boolean and malformed NAV values must still raise
    INVALID_NSE_NUMBER — only exact zero gets the unavailable classification."""
    stamp, _, envelope, _ = market_snapshot(PAYLOAD, LISTINGS, NOW)
    with pytest.raises(ValueError, match="INVALID_NSE_NUMBER"):
        evidence(PROFILE, EtfMetric.NAV, ROW | {"nav": raw}, stamp, envelope, META, NOW)


def test_zero_nav_does_not_affect_market_price_or_volume():
    """Zero-NAV interception is NAV-specific. Market price and volume must
    be completely unaffected — zero volume is valid, zero price is rejected
    as INVALID_NSE_NUMBER (not NSE_NAV_ZERO_UNAVAILABLE)."""
    stamp, _, envelope, _ = market_snapshot(PAYLOAD, LISTINGS, NOW)
    vol = evidence(PROFILE, EtfMetric.TRADING_VOLUME, ROW | {"totalTradedVolume": 0}, stamp, envelope, META, NOW)
    assert vol is not None and vol.value == 0
    with pytest.raises(ValueError, match="INVALID_NSE_NUMBER"):
        evidence(PROFILE, EtfMetric.MARKET_PRICE, ROW | {"lastPrice": 0}, stamp, envelope, META, NOW)


def test_absent_supporting_nav_and_future_nav_are_distinct():
    stamp, _, envelope, _ = market_snapshot(PAYLOAD, LISTINGS, NOW)
    assert evidence(PROFILE, EtfMetric.NAV, ROW | {"nav": "-"}, stamp, envelope, META, NOW) is None
    with pytest.raises(ValueError):
        evidence(PROFILE, EtfMetric.NAV, ROW, stamp, envelope | {"navDate": "10-Oct-2026"}, META, NOW)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 429, 500])
async def test_http_failure_is_bounded_and_does_not_log_body_or_headers(status):
    from app.nse_etf_market import get_json, failure_code, MARKET_URL
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, text="fixture-secret", headers={"Set-Cookie": "fixture-secret"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.HTTPStatusError) as raised:
            await get_json(client, MARKET_URL)
    assert failure_code(raised.value) == f"NSE_HTTP_{status}"
    assert len(calls) == 1
