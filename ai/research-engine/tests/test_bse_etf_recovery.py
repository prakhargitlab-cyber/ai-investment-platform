"""Synthetic canonical metadata and source failures; no BSE trading fixtures."""
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest

from app.etf_acquisition import EtfAcquisitionService, NSE_ETF_LIST_URL
from app.etf_evidence import EtfAcquisitionAttempt, EtfListing, EtfProvenance


NOW = datetime(2026, 11, 4, 12, tzinfo=timezone.utc)
SYMBOLS = ("ABSL10BANK", "ABSLMSCIN", "MSCIADD", "LIQUIDBETA")


def inputs(count=1):
    rows, listings, attempts = [], [], []
    for index in range(count):
        key = UUID(int=index + 1)
        symbol = SYMBOLS[index] if index < len(SYMBOLS) else f"OUTOFSCOPE{index}"
        isin = f"IN{index + 1:010d}"
        # Deliberately invented test identifiers, never candidate authorities.
        rows.append(dict(globalInstrumentId=str(key), primarySymbol=symbol,
            canonicalName="Synthetic ETF", isin=isin, status="ACTIVE", assetType="ETF",
            primaryExchange="NSE", country="IN", currency="INR",
            providerMappings=[dict(provider="NSE", providerSymbol=symbol, status="VERIFIED",
                exchange="NSE", currency="INR", providerInstrumentId=isin,
                resolutionSource="SYNTHETIC_TEST_REFERENCE"),
                dict(provider="BSE", providerSymbol=str(590001 + index), status="VERIFIED",
                    exchange="BSE", currency="INR", resolutionSource="SYNTHETIC_TEST_REFERENCE")]))
        # This simulates an existing canonical store representation. No live
        # official NSE or BSE observation was collected by these unit tests.
        listings.append(EtfListing(instrument_id=key, symbol=symbol, isin=isin,
            name="Synthetic ETF", provenance=EtfProvenance(provider="NSE",
                source_type="NSE_OFFICIAL_ETF_SECURITY_LIST", source_identity="synthetic-nse-list",
                source_url=NSE_ETF_LIST_URL, authority="OFFICIAL_EXCHANGE", retrieved_at=NOW)))
        for metric in ("MARKET_PRICE", "TRADING_VOLUME"):
            attempts.append(EtfAcquisitionAttempt(instrument_id=key, metric=metric, provider="NSE",
                attempted_at=NOW, outcome="TECHNICAL_FAILURE", reason="NSE_ACQUISITION_FAILED:SYNTHETIC_FAILURE"))
    return rows, listings, attempts


def service():
    persistence = Mock()
    value = EtfAcquisitionService(persistence, clock=lambda: NOW)
    value._record_async = AsyncMock(side_effect=AssertionError("Unexpected persistence"))
    return value


def assert_preserved(report, originals):
    reported = [a for item in report["instruments"] for a in item["nse_attempts"]]
    assert reported == [a.model_dump(mode="json") for a in originals]
    assert all(a["provider"] == "BSE" for item in report["instruments"] for a in item["bse_attempts"])


def test_preflight_is_bounded_offline_and_preserves_original_nse_results():
    rows, listings, attempts = inputs(4)
    before = deepcopy((rows, listings, attempts))
    acquisition = service()
    report = acquisition.preflight_bse_recovery(rows, listings=listings, nse_attempts=attempts)
    assert report["status"] == "BLOCKED"
    assert report["candidate_count"] == 4 and report["preflight"] and not report["persisted"]
    assert all(item["reason"] == "BSE_LIVE_SOURCE_CONTRACT_UNVERIFIED" for item in report["instruments"])
    assert_preserved(report, attempts)
    assert (rows, listings, attempts) == before
    acquisition.persistence.assert_not_called()
    assert not acquisition.persistence.mock_calls
    acquisition._record_async.assert_not_awaited()


def test_preflight_shares_a_listing_iterator_across_all_bounded_instruments():
    rows, listings, attempts = inputs(4)
    report = service().preflight_bse_recovery(rows, listings=iter(listings), nse_attempts=attempts)
    assert len(report["instruments"]) == 4
    assert all(item["reason"] == "BSE_LIVE_SOURCE_CONTRACT_UNVERIFIED" for item in report["instruments"])


@pytest.mark.parametrize("count", [0, 6])
@pytest.mark.asyncio
async def test_size_limit_before_source_or_persistence(count):
    rows, listings, attempts = inputs(count)
    acquisition, provider = service(), SimpleNamespace(provider_name="BSE", collect=AsyncMock())
    with pytest.raises(ValueError, match="ETF_CANDIDATE_LIMIT_OR_DUPLICATES"):
        acquisition.preflight_bse_recovery(rows, listings=listings, nse_attempts=attempts)
    with pytest.raises(ValueError, match="ETF_CANDIDATE_LIMIT_OR_DUPLICATES"):
        await acquisition.acquire_bse_recovery(rows, listings=listings, nse_attempts=attempts, provider=provider)
    provider.collect.assert_not_awaited()
    acquisition._record_async.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_canonical_ids_block_before_source():
    rows, listings, attempts = inputs()
    acquisition, provider = service(), SimpleNamespace(provider_name="BSE", collect=AsyncMock())
    with pytest.raises(ValueError, match="ETF_CANDIDATE_LIMIT_OR_DUPLICATES"):
        await acquisition.acquire_bse_recovery(rows * 2, listings=listings, nse_attempts=attempts, provider=provider)
    provider.collect.assert_not_awaited()


@pytest.mark.asyncio
async def test_source_failure_preserves_nse_and_never_persists_or_leaks_error():
    rows, listings, attempts = inputs()
    acquisition = service()
    provider = SimpleNamespace(provider_name="BSE", collect=AsyncMock(side_effect=TimeoutError("secret=private")))
    report = await acquisition.acquire_bse_recovery(rows, listings=listings, nse_attempts=attempts, provider=provider)
    assert report["status"] == "BLOCKED" and not report["persisted"]
    assert report["instruments"][0]["reason"] == "BSE_SOURCE_FAILURE"
    assert "private" not in str(report)
    assert_preserved(report, attempts)
    provider.collect.assert_awaited_once()
    acquisition._record_async.assert_not_awaited()
    assert not acquisition.persistence.mock_calls


@pytest.mark.asyncio
async def test_default_live_adapter_is_explicitly_unimplemented():
    rows, listings, attempts = inputs()
    acquisition = service()
    report = await acquisition.acquire_bse_recovery(rows, listings=listings, nse_attempts=attempts)
    diagnostic, = report["instruments"][0]["bse_attempts"]
    assert diagnostic["outcome"] == "NOT_IMPLEMENTED"
    assert diagnostic["reason"] == "BSE_LIVE_SOURCE_CONTRACT_UNVERIFIED"
    assert diagnostic["evidence_ids"] == []
    assert_preserved(report, attempts)
    acquisition._record_async.assert_not_awaited()


@pytest.mark.asyncio
async def test_supplied_source_cannot_bypass_live_contract_gate():
    rows, listings, attempts = inputs()
    acquisition = service()
    provider = SimpleNamespace(provider_name="BSE", collect=AsyncMock(return_value=["unverified synthetic data"]))
    report = await acquisition.acquire_bse_recovery(rows, listings=listings, nse_attempts=attempts, provider=provider)
    assert report["instruments"][0]["reason"] == "BSE_LIVE_SOURCE_CONTRACT_UNVERIFIED"
    assert not report["persisted"]
    acquisition._record_async.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["mapping", "listing", "nse_conflict", "demo"])
async def test_failed_identity_validation_never_calls_source_or_persistence(invalid):
    rows, listings, attempts = inputs()
    if invalid == "mapping":
        rows[0]["providerMappings"] = rows[0]["providerMappings"][:1]
    elif invalid == "listing":
        listings[0] = listings[0].model_copy(update={"isin": "IN0000000099"})
    elif invalid == "nse_conflict":
        attempts[0] = attempts[0].model_copy(update={"reason": "NSE_ACQUISITION_FAILED:NSE_METADATA_IDENTITY_CONFLICT:ISIN"})
    else:
        listings[0].provenance.source_mode = "DEMO"
    acquisition, provider = service(), SimpleNamespace(provider_name="BSE", collect=AsyncMock())
    report = await acquisition.acquire_bse_recovery(rows, listings=listings, nse_attempts=attempts, provider=provider)
    assert report["status"] == "BLOCKED" and not report["persisted"]
    assert_preserved(report, attempts)
    provider.collect.assert_not_awaited()
    acquisition._record_async.assert_not_awaited()
    assert not acquisition.persistence.mock_calls


@pytest.mark.asyncio
async def test_existing_successful_nse_pair_does_not_attempt_bse():
    rows, listings, attempts = inputs()
    attempts = [a.model_copy(update={"outcome": "SUCCESS_WITH_DATA", "evidence_ids": [a.metric]}) for a in attempts]
    acquisition, provider = service(), SimpleNamespace(provider_name="BSE", collect=AsyncMock())
    report = await acquisition.acquire_bse_recovery(rows, listings=listings, nse_attempts=attempts, provider=provider)
    assert report["status"] == "NOT_REQUIRED"
    assert report["instruments"][0]["bse_attempts"] == []
    assert_preserved(report, attempts)
    provider.collect.assert_not_awaited()


@pytest.mark.parametrize("invalid", ["missing", "ambiguous", "other_provider", "other_id"])
def test_original_nse_attempt_scope_is_required(invalid):
    rows, listings, attempts = inputs()
    if invalid == "missing": attempts.pop()
    if invalid == "ambiguous": attempts.append(attempts[0])
    if invalid == "other_provider": attempts[0] = attempts[0].model_copy(update={"provider": "BSE"})
    if invalid == "other_id": attempts[0] = attempts[0].model_copy(update={"instrument_id": UUID(int=99)})
    with pytest.raises(ValueError, match="BSE_RECOVERY_REQUIRES"):
        service().preflight_bse_recovery(rows, listings=listings, nse_attempts=attempts)


@pytest.mark.asyncio
async def test_five_is_the_hard_bound_and_liquidbeta_stays_blocked():
    rows, listings, attempts = inputs(5)
    acquisition = service()
    provider = SimpleNamespace(provider_name="BSE", collect=AsyncMock(side_effect=OSError("synthetic outage")))
    report = await acquisition.acquire_bse_recovery(rows, listings=listings, nse_attempts=attempts, provider=provider)
    assert report["candidate_count"] == 5 and report["status"] == "BLOCKED"
    assert provider.collect.await_count == 4
    assert report["instruments"][4]["reason"] == "BSE_UNSUPPORTED_CANONICAL_ETF"
    assert all(item["status"] == "BLOCKED" for item in report["instruments"])
    assert next(item for item in report["instruments"] if item["symbol"] == "LIQUIDBETA")["status"] == "BLOCKED"
    assert_preserved(report, attempts)
    acquisition._record_async.assert_not_awaited()
