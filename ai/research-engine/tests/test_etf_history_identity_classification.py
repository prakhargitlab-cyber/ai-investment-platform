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
from datetime import datetime, timezone
from uuid import UUID

import pytest

from app.etf_history import acquire_etf_history
from app.historical_market_data import HistoricalPriceIdentityConflict, HistoricalPriceProviderError


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
