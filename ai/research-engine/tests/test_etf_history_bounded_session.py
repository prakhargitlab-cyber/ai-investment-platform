"""Regression: app.etf_history.acquire_etf_history's default provider
construction must bound yfinance's per-request timeout, exactly like
app.structured_market.YahooFinanceProvider's default ticker_factory (see
_build_bounded_yahoo_session's docstring for the full root-cause
explanation -- yfinance's own per-request default timeout, 30s, is
otherwise unbounded from this module's perspective and can silently
outlive any caller-side expectation under a slow/rate-limited Yahoo
response). Both must reuse yfinance's own preferred session backend
(curl_cffi with Chrome impersonation, confirmed installed in this
environment) rather than silently downgrading to a plain requests.Session
that would lose that anti-bot fingerprinting.

This only exercises the construction path (no network): when acquire_etf_
history is called with provider=None (the real production default), the
yf.Ticker factory it builds must pass a session built by
_build_bounded_yahoo_session, not an uncustomized one.
"""
from datetime import datetime, timezone
from uuid import UUID

import pytest

from app.etf_history import acquire_etf_history
from app.settings import Settings


class _FakeTicker:
    """Records the session passed in by the production default factory and
    raises immediately so acquire_etf_history's broad except classifies this
    as a TECHNICAL_FAILURE without needing real network access."""

    captured_sessions = []

    def __init__(self, symbol, session=None):
        _FakeTicker.captured_sessions.append(session)
        raise RuntimeError("NO_NETWORK_IN_TEST")


class _FakeCheckpoint:
    cycle_id = "cycle-1"
    owner_id = "owner-1"


@pytest.mark.asyncio
async def test_default_provider_construction_uses_bounded_yahoo_session(monkeypatch):
    _FakeTicker.captured_sessions = []
    monkeypatch.setattr("yfinance.Ticker", _FakeTicker)

    row = {"globalInstrumentId": str(UUID(int=1)), "primarySymbol": "NIFTYBEES"}
    result = await acquire_etf_history(
        repository=None, row=row, now=datetime.now(timezone.utc), checkpoint=_FakeCheckpoint()
    )

    # The broad except in acquire_etf_history still classifies the
    # constructor's own RuntimeError as a truthful TECHNICAL_FAILURE --
    # proving this test reached the real default-provider construction path.
    assert result["status"] == "TECHNICAL_FAILURE"
    assert result["reason"] == "RuntimeError"

    assert len(_FakeTicker.captured_sessions) == 1
    session = _FakeTicker.captured_sessions[0]
    # _build_bounded_yahoo_session returns yfinance's own real session
    # object (curl_cffi or requests, whichever new_session() picked for this
    # environment), stamped with _bound_seconds for introspection -- never a
    # dedicated subclass that would imply a different (and here, wrong)
    # transport than yfinance would have chosen for itself.
    assert hasattr(session, "_bound_seconds")
    assert session._bound_seconds == Settings().structured_provider_timeout_seconds
