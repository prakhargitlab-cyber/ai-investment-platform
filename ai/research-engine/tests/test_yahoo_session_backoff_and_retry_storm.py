"""Focused regression tests for the Yahoo 401 Invalid Crumb / retry-storm
fix (Equity Radar sustained Yahoo acquisition failures).

Production evidence: in one 15-minute window, a running Equity FULL cycle
logged Invalid Crumb 681 times, Yahoo access-denied 682 times, and
PROVIDER_IDENTITY_UNAVAILABLE 1362 times, with COMPANY_NAME_MISMATCH at 0
(confirming the earlier identity-classification fix held) -- but the prior
session-rebuild mitigation (a flat 60s cooldown, see
YahooTickerContexts.note_identity_metadata_unavailable) was not actually
reducing the rate: every concurrent/subsequent instrument still reached
Yahoo and failed identically, because rebuilding the session object does
nothing when the whole session/IP is in a sustained block.

This file pins the fix: an escalating, jittered backoff window during
which acquire() fails fast WITHOUT making any Yahoo request at all (the
actual retry-storm fix), a success signal that resets the window
immediately on genuine recovery, and that the window is bounded (caps
rather than growing forever) rather than ever silently abandoning
acquisition entirely.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from yfinance.exceptions import YFRateLimitError

from app.historical_market_data import (
    HistoricalPriceIdentityConflict,
    HistoricalPriceProviderError,
    YahooHistoricalPriceProvider,
)
from app.yahoo_ticker_context import YahooSessionBackoffActive, YahooTickerContexts


def _history(price="1523.40", observed_at=None):
    observed_at = observed_at or datetime(2026, 9, 20, tzinfo=timezone.utc)

    class Timestamp:
        def to_pydatetime(self):
            return observed_at

    class History:
        def iterrows(self):
            return [(Timestamp(), {"Close": price})]

    return History()


class _FakeTicker:
    def __init__(self, info=None, price="1523.40", history_exc=None, info_exc=None):
        self._info = info or {}
        self._price = price
        self._history_exc = history_exc
        self._info_exc = info_exc

    @property
    def info(self):
        if self._info_exc is not None:
            raise self._info_exc
        return self._info

    def history(self, **_kwargs):
        if self._history_exc is not None:
            raise self._history_exc
        return _history(self._price)


def _equity_instrument(structured_ticker: str = "INFY.NS") -> dict:
    return {
        "globalInstrumentId": str(uuid4()),
        "ticker": "INFY",
        "structuredProviderTicker": structured_ticker,
        "currency": "INR",
        "isin": "INE009A01021",
        "companyName": "Infosys Limited",
        "canonicalName": "Infosys Limited",
        "exchange": "NSI",
    }


def _contexts(**kwargs):
    calls = {"factory": 0}

    def factory(symbol):
        calls["factory"] += 1
        return _FakeTicker({"longName": None, "symbol": None, "quoteType": None,
                             "exchange": None, "currency": None})

    contexts = YahooTickerContexts(
        factory, session_refresh=lambda: None,
        session_refresh_cooldown_seconds=kwargs.pop("session_refresh_cooldown_seconds", 10.0),
        random_fn=kwargs.pop("random_fn", lambda: 0.5),  # removes jitter (midpoint)
        **kwargs,
    )
    return contexts, calls


# -- Yahoo access denied / repeated unauthorized responses -----------------

def test_backoff_opens_after_metadata_unavailable_and_skips_further_requests():
    """The retry-storm fix: once backoff is open, acquire() must fail fast
    WITHOUT ever calling the ticker factory -- i.e. without making any
    further Yahoo request at all."""
    contexts, calls = _contexts()
    contexts.note_identity_metadata_unavailable()
    assert contexts.in_backoff() is True
    with pytest.raises(YahooSessionBackoffActive):
        with contexts.acquire("INFY.NS"):
            pass
    assert calls["factory"] == 0


def test_repeated_unauthorized_reports_escalate_the_backoff_window():
    """Each further report while failures continue must open a STRICTLY
    LARGER window (bounded doubling), not the same flat cooldown -- this
    is what actually reduces upstream request volume under sustained
    401s, vs. the old flat-60s mitigation that let every other attempt
    straight through."""
    contexts, _ = _contexts(session_refresh_cooldown_seconds=10.0, backoff_max_seconds=10_000.0)
    import time
    windows = []
    for _ in range(4):
        before = time.monotonic()
        contexts.note_identity_metadata_unavailable()
        windows.append(contexts._backoff_until_monotonic - before)
    # No jitter (random_fn fixed at 0.5) -> exact doubling: 10, 20, 40, 80.
    assert windows[0] == pytest.approx(10.0, abs=0.05)
    assert windows[1] == pytest.approx(20.0, abs=0.05)
    assert windows[2] == pytest.approx(40.0, abs=0.05)
    assert windows[3] == pytest.approx(80.0, abs=0.05)


# -- persistent failure: bounded, not unbounded -----------------------------

def test_persistent_failure_backoff_is_bounded_not_unbounded():
    """A genuinely persistent/permanent authorization failure must keep
    failing (never silently given up on, never retried unboundedly) --
    the window caps at backoff_max_seconds rather than growing forever."""
    contexts, _ = _contexts(session_refresh_cooldown_seconds=10.0, backoff_max_seconds=100.0)
    import time
    for _ in range(10):
        contexts.note_identity_metadata_unavailable()
    window = contexts._backoff_until_monotonic - time.monotonic()
    assert window <= 100.0 + 1.0  # capped, with a little slack for test latency
    assert contexts.in_backoff() is True


# -- successful recovery after a transient 401 ------------------------------

def test_note_identity_metadata_available_resets_backoff_immediately():
    """A transient blip must not leave acquisition throttled long after
    Yahoo has already recovered: one successful identity check resets the
    window to zero regardless of how large it had grown."""
    contexts, calls = _contexts()
    contexts.note_identity_metadata_unavailable()
    contexts.note_identity_metadata_unavailable()
    assert contexts.in_backoff() is True
    contexts.note_identity_metadata_available()
    assert contexts.in_backoff() is False
    with contexts.acquire("INFY.NS") as access:
        access.ticker  # triggers the factory; must not raise now that backoff cleared
    assert calls["factory"] == 1


# -- metadata unavailable vs. explicit identity conflict --------------------

@pytest.mark.asyncio
async def test_closes_maps_backoff_to_technical_reason_never_identity_mismatch():
    """When the shared session is in backoff, _closes must fail with a
    technical, retryable reason and must NEVER raise
    HistoricalPriceIdentityConflict -- provider unavailability is not a
    verified identity mismatch."""
    provider = YahooHistoricalPriceProvider(lambda _symbol: _FakeTicker())
    provider.ticker_contexts.note_identity_metadata_unavailable()
    with pytest.raises(HistoricalPriceProviderError, match="EXTERNAL_PROVIDER_UNAVAILABLE:YAHOO_SESSION_BACKOFF_ACTIVE") as exc_info:
        await provider.closes(
            _equity_instrument(), start=datetime(2026, 9, 1, tzinfo=timezone.utc),
            end=datetime(2026, 9, 21, tzinfo=timezone.utc),
        )
    assert not isinstance(exc_info.value, HistoricalPriceIdentityConflict)


@pytest.mark.asyncio
async def test_genuine_identity_conflict_still_rejected_and_resets_no_backoff_state():
    """A real, populated, wrong company name is unrelated to session
    backoff -- Yahoo responded with real (if wrong) data, so this must
    still raise VERIFIED_HISTORICAL_IDENTITY_MISMATCH:COMPANY_NAME_MISMATCH,
    completely independent of backoff, and must not itself open backoff."""
    provider = YahooHistoricalPriceProvider(lambda _symbol: _FakeTicker(
        {"longName": "HCL Insystems Limited", "symbol": "HCL-INSYS.NS",
         "exchange": "NSI", "currency": "INR"},
        price="10.70",
    ))
    with pytest.raises(HistoricalPriceIdentityConflict,
                        match="VERIFIED_HISTORICAL_IDENTITY_MISMATCH:COMPANY_NAME_MISMATCH"):
        await provider.closes(
            _equity_instrument("HCL-INSYS.NS"), start=datetime(2026, 9, 1, tzinfo=timezone.utc),
            end=datetime(2026, 9, 21, tzinfo=timezone.utc),
        )
    assert provider.ticker_contexts.in_backoff() is False


@pytest.mark.asyncio
async def test_valid_identity_after_prior_failure_resets_backoff():
    """Successful recovery after transient 401, exercised end-to-end: a
    prior PROVIDER_IDENTITY_UNAVAILABLE opens backoff; once Yahoo responds
    with verified data again, backoff clears."""
    provider = YahooHistoricalPriceProvider(lambda _symbol: _FakeTicker(
        {"longName": "Infosys Limited", "symbol": "INFY.NS",
         "exchange": "NSI", "currency": "INR", "isin": "INE009A01021"},
    ))
    provider.ticker_contexts.note_identity_metadata_unavailable()
    assert provider.ticker_contexts.in_backoff() is True
    provider.ticker_contexts.note_identity_metadata_available()
    rows = await provider.closes(
        _equity_instrument("INFY.NS"), start=datetime(2026, 9, 1, tzinfo=timezone.utc),
        end=datetime(2026, 9, 21, tzinfo=timezone.utc),
    )
    assert len(rows) == 1
    assert provider.ticker_contexts.in_backoff() is False


# -- Yahoo 429 and backoff ---------------------------------------------------

@pytest.mark.asyncio
async def test_rate_limited_response_classified_technical_and_opens_backoff():
    provider = YahooHistoricalPriceProvider(
        lambda _symbol: _FakeTicker(history_exc=YFRateLimitError()))
    with pytest.raises(HistoricalPriceProviderError, match="EXTERNAL_PROVIDER_RATE_LIMITED"):
        await provider.closes(
            _equity_instrument(), start=datetime(2026, 9, 1, tzinfo=timezone.utc),
            end=datetime(2026, 9, 21, tzinfo=timezone.utc),
        )
    assert provider.ticker_contexts.in_backoff() is True


# -- no duplicate historical bars on retry ----------------------------------

@pytest.mark.asyncio
async def test_failed_backoff_skipped_attempt_persists_nothing_then_retry_succeeds_once():
    """A request skipped during backoff must contribute zero rows (nothing
    to persist, so nothing can be duplicated); a later successful retry
    after recovery returns exactly one row, not an accumulated duplicate."""
    provider = YahooHistoricalPriceProvider(lambda _symbol: _FakeTicker(
        {"longName": "Infosys Limited", "symbol": "INFY.NS",
         "exchange": "NSI", "currency": "INR", "isin": "INE009A01021"},
    ))
    provider.ticker_contexts.note_identity_metadata_unavailable()
    with pytest.raises(HistoricalPriceProviderError):
        rows_during_backoff = None
        await provider.closes(
            _equity_instrument("INFY.NS"), start=datetime(2026, 9, 1, tzinfo=timezone.utc),
            end=datetime(2026, 9, 21, tzinfo=timezone.utc),
        )
    provider.ticker_contexts.note_identity_metadata_available()
    rows = await provider.closes(
        _equity_instrument("INFY.NS"), start=datetime(2026, 9, 1, tzinfo=timezone.utc),
        end=datetime(2026, 9, 21, tzinfo=timezone.utc),
    )
    assert len(rows) == 1


# -- no-hook / backward compatibility ---------------------------------------

def test_no_backoff_without_a_refresh_hook_but_still_raises_on_open_window():
    """Even with no session_refresh hook configured, note_identity_metadata_
    unavailable must still open the backoff window (the window itself does
    not depend on a refresh hook being present) -- only the session rebuild
    step is skipped."""
    calls = {"factory": 0}

    def factory(symbol):
        calls["factory"] += 1
        return _FakeTicker()

    contexts = YahooTickerContexts(factory, random_fn=lambda: 0.5)
    assert contexts.note_identity_metadata_unavailable() is False  # no refresh hook
    assert contexts.in_backoff() is True
    with pytest.raises(YahooSessionBackoffActive):
        with contexts.acquire("X"):
            pass
    assert calls["factory"] == 0
