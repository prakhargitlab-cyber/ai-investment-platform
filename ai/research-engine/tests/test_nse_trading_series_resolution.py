"""Focused tests for Settings.nse_verified_trading_series_overrides and the
series-resolution path it drives in app.nse_historical_daily.

Scope, deliberately narrow:
  * Default behavior (no override configured) is byte-identical to the
    previous hardcoded-EQ behavior for every instrument.
  * An explicit, independently-verified override lets exactly the
    configured instrument request/accept BE, and never leaks to any other
    instrument.
  * There is no automatic fallback: a response that is empty under the
    resolved series never triggers a retry under a different series.
  * Settings rejects an unverified/unsupported series value outright
    (fail-closed), the same way other identity-adjacent settings here do.
"""
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest

from app.nse_historical_daily import (
    BOOTSTRAP,
    NseHistoricalDailyProvider,
    NseHistoricalResult,
    SUPPORTED_TRADING_SERIES,
    parse_csv,
    verified_trading_series,
)
from app.settings import Settings

KEY = UUID(int=1)
OTHER_KEY = UUID(int=2)
START, END = date(2026, 9, 1), date(2026, 9, 4)
META = dict(
    globalInstrumentId=str(KEY), status="ACTIVE", assetType="EQUITY", country="IN",
    primaryExchange="NSE", currency="INR",
    providerMappings=[dict(provider="NSE", providerSymbol="TRIVENIPT", status="VERIFIED",
                            resolutionSource="NSE_OFFICIAL_ISIN_BOOTSTRAP", currency="INR")],
)


def result(**kwargs):
    kwargs.setdefault("provider_symbol", "TRIVENIPT")
    return NseHistoricalResult(KEY, START, END, **kwargs)


async def fetch_with(handler, metadata=META, **settings):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sleep = AsyncMock()
        provider = NseHistoricalDailyProvider(
            SimpleNamespace(global_instrument_metadata=AsyncMock(return_value=metadata)),
            Settings(**settings), client=client, sleep=sleep,
        )
        return await provider.fetch(KEY, start=START, end=END), sleep


# --- Settings validation (fail-closed) --------------------------------------

def test_default_overrides_is_empty_and_resolution_defaults_to_eq():
    settings = Settings()
    assert settings.nse_verified_trading_series_overrides == {}
    assert verified_trading_series(settings, KEY) == "EQ"


def test_explicit_override_resolves_to_be_for_exactly_that_instrument():
    settings = Settings(nse_verified_trading_series_overrides={str(KEY): "be"})
    assert verified_trading_series(settings, KEY) == "BE"
    # A different, unconfigured instrument is completely unaffected.
    assert verified_trading_series(settings, OTHER_KEY) == "EQ"


@pytest.mark.parametrize("bad_value", ["SME", "MF", "eq-ish", "", "BOND"])
def test_unsupported_series_value_is_rejected_fail_closed(bad_value):
    with pytest.raises(ValueError):
        Settings(nse_verified_trading_series_overrides={str(KEY): bad_value})


def test_malformed_instrument_key_is_rejected_fail_closed():
    with pytest.raises(ValueError):
        Settings(nse_verified_trading_series_overrides={"not-a-uuid": "BE"})


def test_supported_trading_series_is_exactly_eq_and_be():
    assert SUPPORTED_TRADING_SERIES == {"EQ", "BE"}


# --- parse_csv: resolved series drives row acceptance, not a hardcoded literal --

def test_parse_csv_defaults_to_eq_and_rejects_be_rows_unchanged_from_before():
    r = result()
    parse_csv(b"Date,Symbol,Series,Open,High,Low,Close\n01-09-2026,TRIVENIPT,BE,1,2,1,2\n", r, "INR")
    assert r.rows_accepted == 0 and r.rejection_reasons == {"UNSUPPORTED_SERIES": 1}


def test_parse_csv_accepts_be_rows_when_be_is_the_resolved_series():
    r = result()
    parse_csv(b"Date,Symbol,Series,Open,High,Low,Close\n01-09-2026,TRIVENIPT,BE,1,2,1,2\n", r, "INR", "BE")
    assert r.rows_accepted == 1 and r.rows_rejected == 0


def test_parse_csv_with_be_resolved_still_rejects_eq_rows():
    r = result()
    parse_csv(b"Date,Symbol,Series,Open,High,Low,Close\n01-09-2026,TRIVENIPT,EQ,1,2,1,2\n", r, "INR", "BE")
    assert r.rows_accepted == 0 and r.rejection_reasons == {"UNSUPPORTED_SERIES": 1}


# --- fetch(): the resolved series is what's requested, and nothing is ever guessed --

@pytest.mark.asyncio
async def test_fetch_requests_eq_by_default_no_override_configured():
    seen = {}
    def handler(req):
        if str(req.url) == BOOTSTRAP:
            return httpx.Response(200)
        seen["series"] = req.url.params["series"]
        return httpx.Response(200, content=b"Date,Symbol,Series,Open,High,Low,Close\n", headers={"content-type": "text/csv"})
    r, _ = await fetch_with(handler)
    assert seen["series"] == "EQ" and r.requested_series == "EQ"
    assert r.failure_reason == "NO_VALID_HISTORY"


@pytest.mark.asyncio
async def test_fetch_requests_and_accepts_be_when_explicitly_verified_and_configured():
    seen = {}
    def handler(req):
        if str(req.url) == BOOTSTRAP:
            return httpx.Response(200)
        seen["series"] = req.url.params["series"]
        body = b"Date,Symbol,Series,Open,High,Low,Close\n02-09-2026,TRIVENIPT,BE,1,2,1,2\n"
        return httpx.Response(200, content=body, headers={"content-type": "text/csv"})
    r, _ = await fetch_with(handler, nse_verified_trading_series_overrides={str(KEY): "BE"})
    assert seen["series"] == "BE" and r.requested_series == "BE"
    assert r.status == "SUCCESS" and r.rows_accepted == 1


@pytest.mark.asyncio
async def test_empty_eq_response_never_triggers_an_automatic_series_retry():
    """The exact failure mode this change fixes: a zero-row EQ response must
    end as NO_VALID_HISTORY after ONE request, never silently retried under
    BE or any other series, unless BE was already explicitly configured."""
    requests_seen = []
    def handler(req):
        if str(req.url) == BOOTSTRAP:
            return httpx.Response(200)
        requests_seen.append(req.url.params.get("series"))
        return httpx.Response(200, content=b"Date,Symbol,Series,Open,High,Low,Close\n", headers={"content-type": "text/csv"})
    r, _ = await fetch_with(handler)
    assert requests_seen == ["EQ"]  # exactly one request, exactly EQ, no retry under BE
    assert r.failure_reason == "NO_VALID_HISTORY" and not r.bars


@pytest.mark.asyncio
async def test_override_for_one_instrument_does_not_affect_a_different_instrument():
    other_meta = META | dict(globalInstrumentId=str(OTHER_KEY))
    seen = {}
    def handler(req):
        if str(req.url) == BOOTSTRAP:
            return httpx.Response(200)
        seen["series"] = req.url.params["series"]
        return httpx.Response(200, content=b"Date,Symbol,Series,Open,High,Low,Close\n", headers={"content-type": "text/csv"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        settings = Settings(nse_verified_trading_series_overrides={str(KEY): "BE"})
        provider = NseHistoricalDailyProvider(
            SimpleNamespace(global_instrument_metadata=AsyncMock(return_value=other_meta)),
            settings, client=client, sleep=AsyncMock(),
        )
        r = await provider.fetch(OTHER_KEY, start=START, end=END)
    assert seen["series"] == "EQ" and r.requested_series == "EQ"
