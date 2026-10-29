"""Regression: a genuine identity-verification rejection in ETF_ONBOARD
history acquisition must surface its own specific, stable reason text
(e.g. "VERIFIED_HISTORICAL_IDENTITY_MISMATCH:COMPANY_NAME_MISMATCH"), not
be collapsed down to the exception class name
("HistoricalPriceIdentityConflict") -- which made a genuine identity
conflict indistinguishable from any other technical failure, confounding
the 352-ETF_ONBOARD-failure investigation's source-specific failure
classification (task 3B/3D). This does not change retry/state
classification: both remain TECHNICAL_FAILURE/RETRYABLE_FAILURE, exactly
as before -- only the diagnostic reason text is more truthful.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from app.etf_history import EtfHistoryProvider, acquire_etf_history
from app.historical_market_data import HistoricalPriceIdentityConflict, HistoricalPriceProviderError
from app.etf_evidence import EtfFact, EtfListing, EtfMetric
from app.etf_metrics import compute_etf_deterministic_metrics
from app.etf_readiness import evaluate_etf_readiness
from app.etf_rule_engine import EtfRuleEngineInput, score_etf
from app.models import MarketPriceObservation
from test_etf_ingestion import META, NOW, repo
from test_etf_full_universe import checkpoint
from test_etf_preflight import compatible_dispatch
from test_etf_readiness import provenance


class _FakeCheckpoint:
    cycle_id = "cycle-1"
    owner_id = "owner-1"


class _RejectingProvider:
    """Mimics EtfHistoryProvider's own identity-conflict rejection path."""

    async def closes(self, instrument, *, start, end):
        raise HistoricalPriceIdentityConflict("VERIFIED_HISTORICAL_IDENTITY_MISMATCH:COMPANY_NAME_MISMATCH")


class _GenericFailureProvider:
    async def closes(self, instrument, *, start, end):
        raise ConnectionError("boom")


@pytest.mark.asyncio
async def test_identity_conflict_preserves_specific_reason_not_exception_class_name():
    row = {"globalInstrumentId": str(UUID(int=1)), "primarySymbol": "NIFTYBEES"}
    result = await acquire_etf_history(
        repository=None, row=row, now=datetime.now(timezone.utc),
        checkpoint=_FakeCheckpoint(), provider=_RejectingProvider(),
    )
    assert result["status"] == "TECHNICAL_FAILURE"
    # Must be the specific, stable identity-gate reason -- not the generic
    # "HistoricalPriceIdentityConflict" class name, which tells an operator
    # nothing about WHY identity verification rejected this instrument.
    assert result["reason"] == "VERIFIED_HISTORICAL_IDENTITY_MISMATCH:COMPANY_NAME_MISMATCH"


@pytest.mark.asyncio
async def test_generic_provider_exception_still_falls_back_to_class_name():
    """A genuinely generic/unexpected exception (no stable, specific message
    of its own) keeps the prior behavior -- only the known provider-error
    family gets its own message preserved."""
    row = {"globalInstrumentId": str(UUID(int=2)), "primarySymbol": "GOLDBEES"}
    result = await acquire_etf_history(
        repository=None, row=row, now=datetime.now(timezone.utc),
        checkpoint=_FakeCheckpoint(), provider=_GenericFailureProvider(),
    )
    assert result["status"] == "TECHNICAL_FAILURE"
    assert result["reason"] == "ConnectionError"


def _canonical_etf(symbol="DIVIDEND"):
    return META | {
        "primarySymbol": symbol, "canonicalName": "MIRAEAMC - " + symbol,
        "providerMappings": [META["providerMappings"][0] | {"providerSymbol": symbol}],
    }


def _provider(row, *, count=260, **metadata):
    info = {
        "symbol": row["primarySymbol"] + ".NS", "quoteType": "EQUITY",
        "exchange": "NSI", "currency": "INR",
        "longName": "Mirae Asset BSE 500 Dividend Leaders 50 ETF",
    } | metadata

    class Timestamp:
        def __init__(self, value):
            self.value = value

        def to_pydatetime(self):
            return self.value

    class History:
        def iterrows(self):
            return [(Timestamp(NOW - timedelta(days=count - i)), {"Close": str(100 + i)})
                    for i in range(count)]

    ticker = SimpleNamespace(info=info, history=lambda **kwargs: History())
    return EtfHistoryProvider(lambda symbol: ticker)


@pytest.mark.asyncio
@pytest.mark.parametrize("symbol,metadata", [
    ("DIVIDEND", {}),
    ("TOP20", {"longName": "Mirae Asset Nifty Top 20 ETF"}),
    ("SMALL250", {"longName": "Mirae Asset Nifty Smallcap 250 ETF"}),
    ("DIVIDEND", {"longName": None}),
    ("DIVIDEND", {"quoteType": None}),
    ("DIVIDEND", {"symbol": "DIVIDEND", "exchange": "XNSE"}),
])
async def test_verified_nse_etf_accepts_legitimate_yahoo_metadata_differences(symbol, metadata):
    row = _canonical_etf(symbol)
    observations = await _provider(row, count=1, **metadata).closes(
        row | {"structuredProviderTicker": symbol + ".NS"},
        start=NOW - timedelta(days=10), end=NOW,
    )
    assert len(observations) == 1
    assert observations[0].instrument_id == UUID(row["globalInstrumentId"])
    assert observations[0].currency == "INR"


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata,reason", [
    ({"isin": "IN9999999999"}, "ISIN_MISMATCH"),
    ({"exchange": "BSE"}, "EXCHANGE_MISMATCH"),
    ({"currency": "USD"}, "CURRENCY_MISMATCH"),
    ({"symbol": "UNRELATED.NS"}, "SYMBOL_MISMATCH"),
    ({"symbol": "DIVIDEND.BO"}, "SYMBOL_MISMATCH"),
    ({"quoteType": "MUTUALFUND"}, "QUOTE_TYPE_MISMATCH"),
])
async def test_verified_etf_rejects_explicit_conflict_before_persistence(repo, metadata, reason):
    row = _canonical_etf()
    result = await acquire_etf_history(repo, row, now=NOW, checkpoint=await checkpoint(repo),
        provider=_provider(row, count=1, **metadata))
    assert result["status"] == "TECHNICAL_FAILURE"
    assert reason in result["reason"]
    assert repo.persistence.load_daily_market_bars({UUID(row["globalInstrumentId"])}) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 22, 260])
async def test_verified_history_persists_idempotently_and_reaches_readiness_and_scoring(repo, count):
    row = _canonical_etf()
    key = UUID(row["globalInstrumentId"])
    cp = await checkpoint(repo)
    for _ in range(2):
        result = await acquire_etf_history(repo, row, now=NOW, checkpoint=cp,
            provider=_provider(row, count=count))
        assert result["status"] == "SUCCESS_WITH_DATA"
        assert result["observations"] == count
    bars = repo.persistence.load_daily_market_bars({key})
    assert len(bars) == count
    assert all(b.global_instrument_id == key and b.provider_symbol == "DIVIDEND.NS" for b in bars)
    assert all(b.open is b.high is b.low is b.volume is None for b in bars)
    repo.persistence.save_etf_evidence(EtfListing(instrument_id=key, isin=row["isin"], symbol="DIVIDEND",
        name=row["canonicalName"], provenance=provenance(retrieved_at=NOW)))
    for metric in (EtfMetric.MARKET_PRICE, EtfMetric.TRADING_VOLUME):
        repo.persistence.save_etf_evidence(EtfFact(instrument_id=key, metric=metric, value=Decimal(100),
            as_of_date=NOW.date(), provenance=provenance(retrieved_at=NOW)))
    statuses, summary = evaluate_etf_readiness(repo.persistence, key, now=NOW)
    by_requirement = {status.requirement: status.status for status in statuses}
    assert by_requirement["HISTORICAL_RETURNS"] == ("READY_FRESH" if count >= 22 else "MISSING")
    assert by_requirement["VOLATILITY"] == ("READY_FRESH" if count >= 64 else "MISSING")
    assert by_requirement["INDEX_MOMENTUM"] == ("READY_FRESH" if count >= 200 else "MISSING")
    metrics = compute_etf_deterministic_metrics(key, bars)
    scored = score_etf(EtfRuleEngineInput(instrument_id=key, readiness_statuses=statuses,
        readiness_summary=summary, metrics=metrics, now=NOW))
    factors = {factor.factor: factor.status for factor in scored.factor_results}
    assert factors["PERFORMANCE_MOMENTUM"] == ("SCORED" if count >= 22 else "UNAVAILABLE")
    assert factors["RISK_DRAWDOWN_VOLATILITY"] == ("SCORED" if count >= 2 else "UNAVAILABLE")
    assert factors["COST"] == factors["TRACKING_QUALITY"] == factors["DIVERSIFICATION"] == "UNAVAILABLE"


def _observation(**updates):
    return MarketPriceObservation(instrument_id=UUID(META["globalInstrumentId"]),
        observed_at=NOW - timedelta(days=1), price=Decimal(100), currency="INR",
        provider="YAHOO_FINANCE", source_url="https://finance.yahoo.com/quote/DIVIDEND.NS/history",
        retrieved_at=NOW).model_copy(update=updates)


@pytest.mark.asyncio
@pytest.mark.parametrize("updates,reason", [
    ({"instrument_id": UUID(int=999)}, "OBSERVATION_INSTRUMENT_MISMATCH"),
    ({"currency": "USD"}, "CURRENCY_MISMATCH"),
    ({"observed_at": NOW.replace(tzinfo=None)}, "NAIVE_TIMESTAMP"),
    ({"observed_at": NOW + timedelta(days=1)}, "FUTURE_TIMESTAMP"),
    ({"price": Decimal("NaN")}, "INVALID_CLOSE"),
    ({"price": Decimal(0)}, "INVALID_CLOSE"),
])
async def test_invalid_history_observation_never_reaches_persistence(repo, updates, reason):
    provider = SimpleNamespace(closes=AsyncMock(return_value=[_observation(), _observation(**updates)]))
    row = _canonical_etf()
    result = await acquire_etf_history(repo, row, now=NOW, checkpoint=await checkpoint(repo), provider=provider)
    assert result["status"] == "TECHNICAL_FAILURE"
    assert reason in result["reason"]
    assert repo.persistence.load_daily_market_bars({UUID(row["globalInstrumentId"])}) == []


@pytest.mark.asyncio
async def test_out_of_range_history_is_not_persisted(repo):
    provider = SimpleNamespace(closes=AsyncMock(return_value=[
        _observation(observed_at=NOW - timedelta(days=20)),
        _observation(observed_at=NOW - timedelta(days=3)),
        _observation(observed_at=NOW - timedelta(days=1)),
    ]))
    row = _canonical_etf()
    result = await acquire_etf_history(repo, row, now=NOW, checkpoint=await checkpoint(repo), provider=provider,
        start=NOW - timedelta(days=10), end=NOW - timedelta(days=2))
    assert result["status"] == "SUCCESS_WITH_DATA"
    bars = repo.persistence.load_daily_market_bars({UUID(row["globalInstrumentId"])})
    assert [bar.trading_date for bar in bars] == [(NOW - timedelta(days=3)).date()]


@pytest.mark.asyncio
async def test_stable_provider_error_remains_a_technical_failure(repo):
    provider = SimpleNamespace(closes=AsyncMock(side_effect=HistoricalPriceProviderError("DOWNSTREAM_TIMEOUT")))
    row = _canonical_etf()
    result = await acquire_etf_history(repo, row, now=NOW, checkpoint=await checkpoint(repo), provider=provider)
    assert result == {"status": "TECHNICAL_FAILURE", "provider": "YAHOO_FINANCE",
        "reason": "DOWNSTREAM_TIMEOUT", "observations": 0}
    assert repo.persistence.load_daily_market_bars({UUID(row["globalInstrumentId"])}) == []
