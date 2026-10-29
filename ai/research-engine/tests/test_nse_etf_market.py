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
    stamp, rows, envelope = market_snapshot(PAYLOAD, LISTINGS, NOW)
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


@pytest.mark.parametrize("change", [{"isin": "INF000000000"}, {"symbol": "OTHER"},
    {"isETFSec": "false"}, {"isDelisted": "true"}, {"isSuspended": "true"},
    {"activeSeries": []}, {"tempSuspendedSeries": ["EQ"]}, {"marketType": "X"}])
def test_metadata_cannot_reassign_identity(change):
    with pytest.raises(ValueError, match="IDENTITY_CONFLICT"):
        verify_metadata(META | change, CANONICAL)


@pytest.mark.parametrize("case", ["duplicate", "unknown", "series", "identifier", "future", "undated", "empty"])
def test_unverified_market_snapshot_rejected(case):
    payload = deepcopy(PAYLOAD)
    if case == "duplicate": payload["data"]["data"].append(dict(ROW))
    if case == "unknown": payload["data"]["data"][0]["symbol"] = "OTHER"
    if case == "series": payload["data"]["data"][0]["series"] = "BE"
    if case == "identifier": payload["data"]["data"][0]["identifier"] = "OTHER"
    if case == "future": payload["data"]["timestamp"] = "10-Oct-2026 16:00:00"
    if case == "undated": payload["data"]["timestamp"] = ""
    if case == "empty": payload["data"]["data"] = []
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
    stamp, rows, envelope = market_snapshot(payload, LISTINGS, NOW, requested_symbols={"SILVERBEES"})
    assert set(rows) == {"SILVERBEES"}


@pytest.mark.parametrize("case", ["duplicate", "unknown", "series", "identifier"])
def test_requested_instruments_still_get_strict_checks_when_scoped(case):
    # requested_symbols must never weaken validation for the instrument(s)
    # actually being requested -- only unrelated rows are skipped.
    payload = deepcopy(PAYLOAD)
    if case == "duplicate": payload["data"]["data"].append(dict(ROW))
    if case == "unknown": payload["data"]["data"][0]["symbol"] = "OTHER"
    if case == "series": payload["data"]["data"][0]["series"] = "BE"
    if case == "identifier": payload["data"]["data"][0]["identifier"] = "OTHER"
    requested = {"SILVERBEES"} if case != "unknown" else {"SILVERBEES", "OTHER"}
    with pytest.raises(ValueError):
        market_snapshot(payload, LISTINGS, NOW, requested_symbols=requested)


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
    stamp, rows, envelope = market_snapshot(payload, LISTINGS, NOW)
    assert "SILVERBEES" in rows


def test_zero_volume_does_not_make_carried_price_fresh():
    stamp, _, envelope = market_snapshot(PAYLOAD, LISTINGS, NOW)
    row = ROW | {"totalTradedVolume": 0}
    assert evidence(PROFILE, EtfMetric.MARKET_PRICE, row, stamp, envelope, META, NOW) is None
    assert evidence(PROFILE, EtfMetric.TRADING_VOLUME, row, stamp, envelope, META, NOW).value == 0
    with pytest.raises(ValueError):
        evidence(PROFILE, EtfMetric.TRADING_VOLUME, ROW | {"totalTradedVolume": 0.5}, stamp, envelope, META, NOW)


def test_absent_supporting_nav_and_future_nav_are_distinct():
    stamp, _, envelope = market_snapshot(PAYLOAD, LISTINGS, NOW)
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
