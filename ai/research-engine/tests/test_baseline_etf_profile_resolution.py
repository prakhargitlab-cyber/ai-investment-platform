"""Regression tests: baseline (and deep) acquisition must resolve whichever
profile store actually holds an instrument after hydration -- CompanyResearchProfile
(repository.profiles) or EtfResearchProfile (repository.etf_profiles) -- instead of
assuming repository.profiles unconditionally.

register_global_profile_metadata() routes ETFs exclusively into
repository.etf_profiles.  Before this fix, baseline acquisition's unconditional
repository.profile(key) call raised an unhandled StopIteration for any
successfully-hydrated ETF, surfacing as a generic
"BASELINE_ACQUISITION_FAILED"/exception=StopIteration failure instead of the
candidate proceeding normally.

See also: app/global_opportunity_orchestration.py's _baseline_jurisdiction(),
which widens jurisdiction_for_profile() to accept an EtfResearchProfile (no
`country` field, unlike CompanyResearchProfile).
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from app.global_opportunity_orchestration import _baseline_jurisdiction
from test_global_opportunity_baseline import setup_acquisition, _baseline_ready_count
from test_global_scanner import NOW


def _etf_profile(instrument_id, *, exchange="NSE"):
    """A minimal EtfResearchProfile-shaped stand-in: only instrument_id and
    exchange are read by the baseline acquisition path being tested (identity
    check + _baseline_jurisdiction), so a SimpleNamespace with exactly those
    fields exercises the real contract without depending on EtfResearchProfile's
    full constructor requirements."""
    return SimpleNamespace(instrument_id=instrument_id, exchange=exchange)


@pytest.mark.asyncio
async def test_hydrated_etf_proceeds_through_baseline_without_stopiteration(monkeypatch):
    """hydration succeeds -> ETF lands in etf_profiles (not profiles) ->
    baseline acquisition must resolve it via repository.etf_profile() and
    proceed normally: no StopIteration, no BASELINE_ACQUISITION_FAILED, no
    PROFILE_IDENTITY_MISMATCH -- the candidate is baseline-ready like any other
    eligible instrument."""
    service, rows, pairs, store, tracker = setup_acquisition(monkeypatch, count=3)
    etf_key = UUID(int=1)
    real_profile = service.repository.profile

    def profile_missing_for_etf(key):
        if key == etf_key:
            raise StopIteration
        return real_profile(key)

    def etf_profile_present(key):
        if key == etf_key:
            return _etf_profile(key)
        raise StopIteration

    service.repository.profile = profile_missing_for_etf
    service.repository.etf_profile = etf_profile_present

    result = await service.run(rows, as_of=NOW, shortlist_limit=25, top_n=4)

    # No failure diagnostic of any kind for the ETF candidate.
    etf_failures = [d for d in result.diagnostics
                    if d.global_instrument_id == etf_key and d.failure_reason]
    assert etf_failures == [], f"expected no failure diagnostic for the ETF, got {etf_failures}"
    # All 3 candidates (including the ETF) reached baseline readiness.
    assert _baseline_ready_count(tracker) == 3
    assert result.baseline_ready_count == 3
    assert result.baseline_incomplete_count == 0


@pytest.mark.asyncio
async def test_hydration_success_with_no_profile_in_either_store_is_explicit_truthful_failure(monkeypatch):
    """The genuine anomaly case: profile_hydrator reports success but neither
    repository.profile() nor repository.etf_profile() can find the instrument
    afterward.  This must surface as the explicit, truthful
    "PROFILE_MISSING_AFTER_HYDRATION" reason -- never an unhandled
    StopIteration, never silently mis-labeled PROFILE_IDENTITY_MISMATCH (which
    means "found under the wrong id", not "not found anywhere") -- and must not
    abort the other candidates' baseline acquisition."""
    service, rows, pairs, store, tracker = setup_acquisition(monkeypatch, count=3)
    missing_key = UUID(int=1)
    real_profile = service.repository.profile

    def profile_missing(key):
        if key == missing_key:
            raise StopIteration
        return real_profile(key)

    def etf_profile_also_missing(key):
        raise StopIteration

    service.repository.profile = profile_missing
    service.repository.etf_profile = etf_profile_also_missing

    result = await service.run(rows, as_of=NOW, shortlist_limit=25, top_n=4)

    diag = [d for d in result.diagnostics
            if d.global_instrument_id == missing_key
            and d.failure_reason == "PROFILE_MISSING_AFTER_HYDRATION"]
    assert diag, f"expected a PROFILE_MISSING_AFTER_HYDRATION diagnostic, got {result.diagnostics}"
    # The other 2 candidates were not aborted by this one's anomaly.
    assert result.baseline_ready_count == 2
    assert result.baseline_incomplete_count == 1


def test_profile_missing_after_hydration_is_retryable_not_terminal():
    """PROFILE_MISSING_AFTER_HYDRATION is a genuine, unexpected anomaly (the
    hydrator claimed success but nothing is findable) rather than a structural
    identity conflict -- it must default to RETRYABLE_FAILURE (matching
    BASELINE_ACQUISITION_FAILED's existing technical-failure semantics), not
    the TERMINAL_OUTCOME state used for PROFILE_HYDRATION_FAILED /
    PROFILE_IDENTITY_MISMATCH."""
    from app.cycle_checkpoint import CandidateState, DISPOSITION_STATE, state_for_disposition
    assert DISPOSITION_STATE["PROFILE_MISSING_AFTER_HYDRATION"] == CandidateState.RETRYABLE_FAILURE
    assert state_for_disposition("PROFILE_MISSING_AFTER_HYDRATION") == CandidateState.RETRYABLE_FAILURE


def test_baseline_jurisdiction_handles_etf_profile_without_country_field():
    """_baseline_jurisdiction must not crash on an EtfResearchProfile, which
    (unlike CompanyResearchProfile) has no `country` attribute at all."""
    nse_etf = _etf_profile(UUID(int=1), exchange="NSE")
    assert _baseline_jurisdiction(nse_etf) == "INDIA"

    unknown_exchange_etf = _etf_profile(UUID(int=2), exchange="XYZ")
    assert _baseline_jurisdiction(unknown_exchange_etf) == "GLOBAL"


def test_baseline_jurisdiction_matches_jurisdiction_for_profile_for_company_profiles():
    """For a real CompanyResearchProfile (which does have `country`),
    _baseline_jurisdiction must agree exactly with the original
    jurisdiction_for_profile -- this widening changes nothing for the existing,
    non-ETF path."""
    from uuid import uuid4
    from app.models import CompanyResearchProfile
    from app.research_readiness_runtime import jurisdiction_for_profile

    for country, exchange, expected in (
        ("IN", "NSE", "INDIA"),
        ("US", "NASDAQ", "USA"),
        ("DE", "XETRA", "EUROPE"),
        ("JP", "TSE", "GLOBAL"),
    ):
        profile = CompanyResearchProfile(
            instrument_id=uuid4(), company_id=uuid4(), company_name="Test Co",
            ticker="TST", exchange=exchange, mic="XXXX", country=country, currency="USD")
        assert _baseline_jurisdiction(profile) == jurisdiction_for_profile(profile) == expected
