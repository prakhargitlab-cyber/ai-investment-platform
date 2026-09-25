"""Regression tests: NSE identity must be populated only from a VERIFIED mapping.

Contract under test (app/portfolio_orchestration.py):
    provider_instrument_ids["NSE"] must be populated only from a provider
    mapping whose status is exactly "VERIFIED". A "RESOLVED" NSE mapping --
    an acceptable provisional trust level for every OTHER provider via the
    existing _trusted_provider_mapping() -- must never become an
    authoritative NSE identity, because Java's own InstrumentMasterService
    does not treat RESOLVED as equivalent to VERIFIED/reusable NSE identity
    either (see the diagnosis: RESOLVED is assigned to lower-confidence,
    non-NSE-specific matches and is excluded from Java's own REUSABLE list).

    This is strictly downstream of, and independent from, the exact-ISIN
    priority fix in app/structured_market.py (Gate 2, Yahoo re-verification).
    That fix is untouched here; its own regression suite
    (tests/test_trusted_nse_candidate_isin_priority.py) is re-run unmodified
    as part of this change's validation, not duplicated in this file.
"""
import asyncio
from uuid import UUID, uuid4

import httpx
import pytest

from app.models import CompanyResearchProfile
from app.portfolio_orchestration import (
    PortfolioResearchOrchestrator,
    _global_master_instrument,
    _hydrate_verified_exchange_mappings,
    _trusted_mapping_for_provider,
    _trusted_nse_provider_mapping,
    _trusted_provider_mapping,
)
from app.repository import ResearchRepository
from app.settings import Settings
from app.source_discovery import OfficialFilingDiscovery, OfficialNseShareholdingDiscovery


def _payload(global_id: UUID, *, nse_status: str | None = "VERIFIED", nse_symbol: str = "EXAMPLE",
             extra_mappings: list[dict] | None = None) -> dict:
    mappings = []
    if nse_status is not None:
        mappings.append({"provider": "NSE", "providerSymbol": nse_symbol, "status": nse_status, "exchange": "NSE"})
    mappings.extend(extra_mappings or [])
    return {
        "globalInstrumentId": str(global_id),
        "canonicalName": "Example Components Limited",
        "isin": "INE000A01010",
        "assetType": "EQUITY",
        "country": "IN",
        "currency": "INR",
        "primaryExchange": "NSE",
        "primarySymbol": nse_symbol,
        "providerMappings": mappings,
    }


class _OfflineClient:
    """Asserts these tests never perform portfolio-service HTTP (metadata is always pre-supplied)."""

    async def get(self, *args, **kwargs):
        raise AssertionError("this test must not perform portfolio-service HTTP")

    async def post(self, *args, **kwargs):
        raise AssertionError("this test must not perform portfolio-service HTTP")


def _orchestrator() -> tuple[PortfolioResearchOrchestrator, ResearchRepository]:
    repo = ResearchRepository(settings=Settings(research_demo_enabled=False))
    settings = Settings(research_demo_enabled=False, structured_provider_enabled=False,
                         portfolio_service_base_url="http://portfolio-service")
    return PortfolioResearchOrchestrator(repo, settings, client=_OfflineClient()), repo


def _fail_transport(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"must never call NSE for an unverified mapping: {request.url}")


# --------------------------------------------------------------------------
# A-D: unit-level trust predicate contract
# --------------------------------------------------------------------------

def test_A_nse_verified_mapping_is_trusted() -> None:
    mapping = {"provider": "NSE", "providerSymbol": "EXAMPLE", "status": "VERIFIED"}
    assert _trusted_nse_provider_mapping(mapping) is True
    assert _trusted_mapping_for_provider(mapping, "NSE") is True


def test_B_nse_resolved_mapping_is_not_trusted_but_generic_predicate_is_unchanged() -> None:
    mapping = {"provider": "NSE", "providerSymbol": "EXAMPLE", "status": "RESOLVED"}
    assert _trusted_nse_provider_mapping(mapping) is False
    assert _trusted_mapping_for_provider(mapping, "NSE") is False
    # RESOLVED remains trusted for the *generic* (non-NSE) predicate -- this
    # proves the tightening is NSE-scoped, not a change to shared semantics.
    assert _trusted_provider_mapping(mapping) is True


def test_C_nse_rejected_mapping_is_not_trusted() -> None:
    mapping = {"provider": "NSE", "providerSymbol": "EXAMPLE", "status": "REJECTED"}
    assert _trusted_nse_provider_mapping(mapping) is False


@pytest.mark.parametrize("status", [None, "", "PENDING", "FAILED", "UNKNOWN"])
def test_D_nse_mapping_without_verified_status_is_not_trusted(status) -> None:
    mapping = {"provider": "NSE", "providerSymbol": "EXAMPLE"}
    if status is not None:
        mapping["status"] = status
    assert _trusted_nse_provider_mapping(mapping) is False


def test_D_nse_verified_but_broker_import_identity_remains_excluded() -> None:
    # The pre-existing broker-import exclusion still applies on top of the
    # stricter VERIFIED-only bar.
    mapping = {"provider": "NSE", "providerSymbol": "EXAMPLE", "status": "VERIFIED",
               "resolutionSource": "BROKER_IMPORT_IDENTITY"}
    assert _trusted_nse_provider_mapping(mapping) is False


# --------------------------------------------------------------------------
# E: other providers are unaffected
# --------------------------------------------------------------------------

@pytest.mark.parametrize("provider", ["BSE", "YAHOO_FINANCE"])
def test_E_other_provider_resolved_mapping_behavior_is_unchanged(provider) -> None:
    mapping = {"provider": provider, "providerSymbol": "EXAMPLE", "status": "RESOLVED"}
    assert _trusted_mapping_for_provider(mapping, provider) is True
    assert _trusted_mapping_for_provider(mapping, provider) == _trusted_provider_mapping(mapping)


def test_E_global_master_instrument_keeps_resolved_yahoo_mapping() -> None:
    global_id = uuid4()
    payload = _payload(global_id, nse_status="VERIFIED", extra_mappings=[
        {"provider": "YAHOO_FINANCE", "providerSymbol": "EXAMPLE.NS", "status": "RESOLVED", "exchange": "NSE"},
    ])
    instrument = _global_master_instrument(payload, global_id)
    assert instrument.get("nseSymbol") == "EXAMPLE"
    assert instrument.get("structuredProviderTicker") == "EXAMPLE.NS"


def test_E_registration_keeps_resolved_bse_mapping() -> None:
    orchestrator, repo = _orchestrator()
    global_id = uuid4()
    payload = _payload(global_id, nse_status="VERIFIED", extra_mappings=[
        {"provider": "BSE", "providerSymbol": "500001", "status": "RESOLVED", "exchange": "BSE"},
    ])
    assert orchestrator.register_global_profile_metadata(global_id, payload) is True
    profile = repo.profile(global_id)
    assert profile.provider_instrument_ids.get("BSE") == "500001"
    assert profile.provider_instrument_ids.get("NSE") == "EXAMPLE"


# --------------------------------------------------------------------------
# A/B/C end-to-end through registration (register_global_profile_metadata,
# which exercises _register_equity_profile_from_instrument on first
# registration and _refresh_profile_from_global_instrument on every call)
# --------------------------------------------------------------------------

def test_A_verified_nse_mapping_populates_profile_on_registration() -> None:
    orchestrator, repo = _orchestrator()
    global_id = uuid4()
    payload = _payload(global_id, nse_status="VERIFIED")
    assert orchestrator.register_global_profile_metadata(global_id, payload) is True
    assert repo.profile(global_id).provider_instrument_ids.get("NSE") == "EXAMPLE"


def test_B_resolved_nse_mapping_does_not_populate_profile_on_registration() -> None:
    orchestrator, repo = _orchestrator()
    global_id = uuid4()
    payload = _payload(global_id, nse_status="RESOLVED")
    assert orchestrator.register_global_profile_metadata(global_id, payload) is True
    profile = repo.profile(global_id)
    assert "NSE" not in profile.provider_instrument_ids
    # F (creation variant): other identity fields still register normally --
    # this must not become a new COMPANY_NOT_RESOLVED path.
    assert profile.company_name == "Example Components Limited"
    assert profile.isin == "INE000A01010"


def test_C_rejected_nse_mapping_does_not_populate_profile_on_registration() -> None:
    orchestrator, repo = _orchestrator()
    global_id = uuid4()
    payload = _payload(global_id, nse_status="REJECTED")
    assert orchestrator.register_global_profile_metadata(global_id, payload) is True
    assert "NSE" not in repo.profile(global_id).provider_instrument_ids


# --------------------------------------------------------------------------
# F: a stale, previously-VERIFIED NSE symbol is removed on rehydration, not
# retained, once fresh data shows no VERIFIED NSE mapping
# --------------------------------------------------------------------------

def test_F_hydrate_removes_stale_nse_symbol_when_fresh_data_shows_no_verified_mapping() -> None:
    profile = CompanyResearchProfile(
        instrument_id=uuid4(), company_id=uuid4(), company_name="Example Components Limited",
        isin="INE000A01010", ticker="EXAMPLE", exchange="NSE", mic="XNSE", country="IN", currency="INR",
        provider_instrument_ids={"NSE": "OLD_SYMBOL", "YAHOO_FINANCE": "OLD_SYMBOL.NS"},
    )
    fresh = {"providerMappings": [{"provider": "NSE", "providerSymbol": "OLD_SYMBOL", "status": "RESOLVED"}]}
    result = _hydrate_verified_exchange_mappings(profile, fresh)
    assert "NSE" not in result.provider_instrument_ids
    # Other providers keep the existing add/overwrite-only behavior --
    # nothing here should touch YAHOO_FINANCE.
    assert result.provider_instrument_ids.get("YAHOO_FINANCE") == "OLD_SYMBOL.NS"


def test_F_hydrate_does_not_wipe_nse_when_no_fresh_mapping_data_is_supplied_at_all() -> None:
    # Distinguishes "fresh data was checked and shows no VERIFIED NSE
    # mapping" from "no provider-mapping data was supplied to check at all"
    # -- the latter must not silently wipe a previously-good NSE identity.
    profile = CompanyResearchProfile(
        instrument_id=uuid4(), company_id=uuid4(), company_name="Example Components Limited",
        isin="INE000A01010", ticker="EXAMPLE", exchange="NSE", mic="XNSE", country="IN", currency="INR",
        provider_instrument_ids={"NSE": "OLD_SYMBOL"},
    )
    partial = {"bseSymbol": "500001"}  # no "providerMappings" key at all
    result = _hydrate_verified_exchange_mappings(profile, partial)
    assert result.provider_instrument_ids.get("NSE") == "OLD_SYMBOL"


def test_F_end_to_end_downgrade_removes_stale_symbol_via_registration() -> None:
    orchestrator, repo = _orchestrator()
    global_id = uuid4()
    orchestrator.register_global_profile_metadata(global_id, _payload(global_id, nse_status="VERIFIED", nse_symbol="AAA"))
    assert repo.profile(global_id).provider_instrument_ids.get("NSE") == "AAA"

    orchestrator.register_global_profile_metadata(global_id, _payload(global_id, nse_status="RESOLVED", nse_symbol="AAA"))
    assert "NSE" not in repo.profile(global_id).provider_instrument_ids


def test_F_end_to_end_downgrade_removes_stale_symbol_via_global_presentation_read() -> None:
    # Exercises the _read_company_state_impl -> _resolve_profile ->
    # _hydrate_verified_exchange_mappings path specifically (the incremental,
    # reuse-a-profile path this fix changes), as distinct from the
    # full-replace register_global_profile_metadata path above.
    #
    # NOTE ON SCOPE: this asserts against the underlying
    # CompanyResearchProfile.provider_instrument_ids -- the field that gates
    # OfficialFilingDiscovery/OfficialNseShareholdingDiscovery, and the only
    # field this task's contract governs -- not against
    # PortfolioResearchCompany.verified_provider_mappings (a presentation-only
    # field built by the separate _listing_identity() helper, which merges in
    # a second, independent read of the raw providerMappings using the
    # unchanged generic VERIFIED/RESOLVED predicate). _listing_identity() is
    # deliberately left untouched: it does not feed official NSE discovery,
    # and changing it would touch a frontend-facing response field, which is
    # out of scope for this fix. That leaves a known, deliberate
    # inconsistency -- the displayed "NSE" listing symbol can still reflect a
    # RESOLVED mapping even though discovery no longer uses it -- reported
    # separately rather than silently patched here.
    orchestrator, repo = _orchestrator()
    global_id = uuid4()
    orchestrator.register_global_profile_metadata(global_id, _payload(global_id, nse_status="VERIFIED", nse_symbol="AAA"))
    assert repo.profile(global_id).provider_instrument_ids.get("NSE") == "AAA"

    downgraded = _payload(global_id, nse_status="RESOLVED", nse_symbol="AAA")
    asyncio.run(orchestrator.read_global_company_state(global_id, metadata=downgraded))
    assert "NSE" not in repo.profile(global_id).provider_instrument_ids


# --------------------------------------------------------------------------
# G: symbol changes deterministically follow the currently VERIFIED mapping
# --------------------------------------------------------------------------

def test_G_hydrate_switches_deterministically_between_two_verified_symbols() -> None:
    profile = CompanyResearchProfile(
        instrument_id=uuid4(), company_id=uuid4(), company_name="Example Components Limited",
        isin="INE000A01010", ticker="AAA", exchange="NSE", mic="XNSE", country="IN", currency="INR",
        provider_instrument_ids={"NSE": "AAA"},
    )
    fresh = {"nseSymbol": "BBB", "providerMappings": [{"provider": "NSE", "providerSymbol": "BBB", "status": "VERIFIED"}]}
    result = _hydrate_verified_exchange_mappings(profile, fresh)
    assert result.provider_instrument_ids.get("NSE") == "BBB"


def test_G_end_to_end_symbol_change_across_two_registrations() -> None:
    orchestrator, repo = _orchestrator()
    global_id = uuid4()
    orchestrator.register_global_profile_metadata(global_id, _payload(global_id, nse_status="VERIFIED", nse_symbol="AAA"))
    assert repo.profile(global_id).provider_instrument_ids.get("NSE") == "AAA"

    orchestrator.register_global_profile_metadata(global_id, _payload(global_id, nse_status="VERIFIED", nse_symbol="BBB"))
    assert repo.profile(global_id).provider_instrument_ids.get("NSE") == "BBB"


# --------------------------------------------------------------------------
# H/I: OfficialFilingDiscovery / OfficialNseShareholdingDiscovery only ever
# see an NSE symbol that came from a VERIFIED mapping
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_H_official_filing_discovery_receives_symbol_only_for_verified_mapping() -> None:
    orchestrator, repo = _orchestrator()
    global_id = uuid4()
    orchestrator.register_global_profile_metadata(global_id, _payload(global_id, nse_status="VERIFIED", nse_symbol="VERIFIED_SYM"))
    profile = repo.profile(global_id)
    rows = [{"an_dt": "10-Aug-2026 13:52:28", "desc": "Financial results", "attchmntText": "Financial results",
             "attchmntFile": "https://nsearchives.nseindia.com/corporate/results.pdf"}]
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=rows, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await OfficialFilingDiscovery(client).discover(profile, {"FINANCIAL_RESULTS"}, set())
    finally:
        await client.aclose()
    assert len(result) == 1
    assert calls[0].url.params["symbol"] == "VERIFIED_SYM"


@pytest.mark.asyncio
async def test_H_official_filing_discovery_gets_no_symbol_and_no_results_for_resolved_only_mapping() -> None:
    orchestrator, repo = _orchestrator()
    global_id = uuid4()
    orchestrator.register_global_profile_metadata(global_id, _payload(global_id, nse_status="RESOLVED", nse_symbol="RESOLVED_SYM"))
    profile = repo.profile(global_id)
    assert "NSE" not in profile.provider_instrument_ids
    client = httpx.AsyncClient(transport=httpx.MockTransport(_fail_transport))
    try:
        result = await OfficialFilingDiscovery(client).discover(profile, {"FINANCIAL_RESULTS"}, set())
    finally:
        await client.aclose()
    assert result == []  # never even reached NSE -- no unverified symbol was sent


@pytest.mark.asyncio
async def test_I_official_shareholding_discovery_receives_symbol_only_for_verified_mapping() -> None:
    orchestrator, repo = _orchestrator()
    global_id = uuid4()
    orchestrator.register_global_profile_metadata(global_id, _payload(global_id, nse_status="VERIFIED", nse_symbol="VERIFIED_SYM"))
    profile = repo.profile(global_id)
    rows = [{"recordId": "1", "symbol": "VERIFIED_SYM", "date": "30-JUN-2026", "submissionDate": "20-JUL-2026",
             "xbrl": "https://nsearchives.nseindia.com/corporate/xbrl/SHP_1.xml",
             "pr_and_prgrp": "42.50", "public_val": "57.50", "employeeTrusts": "0"}]
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=rows, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        snapshots = await OfficialNseShareholdingDiscovery(client).discover(profile)
    finally:
        await client.aclose()
    assert len(snapshots) == 1
    assert calls[0].url.params["symbol"] == "VERIFIED_SYM"


@pytest.mark.asyncio
async def test_I_official_shareholding_discovery_gets_no_symbol_and_no_results_for_resolved_only_mapping() -> None:
    orchestrator, repo = _orchestrator()
    global_id = uuid4()
    orchestrator.register_global_profile_metadata(global_id, _payload(global_id, nse_status="RESOLVED", nse_symbol="RESOLVED_SYM"))
    profile = repo.profile(global_id)
    client = httpx.AsyncClient(transport=httpx.MockTransport(_fail_transport))
    try:
        snapshots = await OfficialNseShareholdingDiscovery(client).discover(profile)
    finally:
        await client.aclose()
    assert snapshots == []


# --------------------------------------------------------------------------
# J: a RESOLVED NSE mapping cannot trigger discovery even with a plausible,
# self-consistent matching ticker/company name
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_J_plausible_matching_ticker_and_name_do_not_rescue_a_resolved_nse_mapping() -> None:
    orchestrator, repo = _orchestrator()
    global_id = uuid4()
    payload = _payload(global_id, nse_status="RESOLVED", nse_symbol="EXAMPLE")
    payload["canonicalName"] = "Example Components Limited"
    payload["primarySymbol"] = "EXAMPLE"
    orchestrator.register_global_profile_metadata(global_id, payload)
    profile = repo.profile(global_id)
    # The ticker/company name are entirely plausible and self-consistent --
    # only the mapping status disqualifies it.
    assert profile.ticker == "EXAMPLE"
    assert profile.company_name == "Example Components Limited"
    assert "NSE" not in profile.provider_instrument_ids
    client = httpx.AsyncClient(transport=httpx.MockTransport(_fail_transport))
    try:
        filings = await OfficialFilingDiscovery(client).discover(profile, {"FINANCIAL_RESULTS"}, set())
        snapshots = await OfficialNseShareholdingDiscovery(client).discover(profile)
    finally:
        await client.aclose()
    assert filings == []
    assert snapshots == []
