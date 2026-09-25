"""Regression tests: global search identity must agree with click-through
resolution identity ("SEARCH_VISIBLE" must imply "RESEARCH_RESOLVABLE").

Contract under test:
    An ACTIVE NSE equity that is findable through the global search box
    (exact ticker, exact company name, partial company name, exact ISIN)
    must resolve the *same* globalInstrumentId through the company/research
    click-through path (readiness, presentation, events, documents, summary)
    -- without requiring Yahoo mapping/coverage, SearXNG, Nifty membership,
    a market-cap threshold, or a liquidity threshold.

Root cause fixed here (app/portfolio_orchestration.py, _resolve_profile):
    _resolve_profile() -- the "general portfolio resolver" used by the
    presentation/events/documents/summary click-through paths (via
    _read_company_state_impl / restore_global_profile) -- matched profiles
    with a single pass that checked provider+providerInstrumentId, exact
    instrument_id, ISIN, and ticker+exchange *per profile, in list-iteration
    order*, returning on the first match. This let a weaker match (ISIN or
    ticker+exchange) against an *earlier-iterated, differently-keyed*
    profile -- e.g. one already registered from a portfolio position under a
    legacy/broker-specific identity -- shadow a *later-iterated* profile
    that was an exact instrument_id match for the canonical global search
    result. register_global_profile_metadata() (the readiness path) already
    avoided this by matching on instrument_id alone; this fix gives
    _resolve_profile() the same discipline: an exact instrument_id match is
    now found in a dedicated first pass across the *whole* profile list,
    before any fuzzy criterion runs at all. No NSE-VERIFIED-only trust
    rule, fuzzy-matching threshold, or universe-eligibility criterion is
    changed; only match *priority* is corrected.

Nothing here hard-codes AWHCL: the regression scenario (K) is built from a
synthetic company and reproduces the mechanism generically.
"""
from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest

from app.models import CompanyResearchProfile
from app.portfolio_orchestration import (
    PortfolioResearchOrchestrator,
    _global_master_instrument,
    _normalize_search_item,
    _search_item_matches,
    _search_rank,
)
from app.repository import ResearchRepository
from app.sector_performance import belongs_to_region
from app.settings import Settings


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


def _payload(global_id: UUID, *, company_name: str, isin: str, symbol: str, nse_status: str = "VERIFIED",
             extra_mappings: list[dict] | None = None) -> dict:
    """A portfolio-service single-instrument GET payload (GlobalInstrumentResponse shape)."""
    mappings = [{"provider": "NSE", "providerSymbol": symbol, "status": nse_status, "exchange": "NSE"}]
    mappings.extend(extra_mappings or [])
    return {
        "globalInstrumentId": str(global_id),
        "canonicalName": company_name,
        "isin": isin,
        "assetType": "EQUITY",
        "country": "IN",
        "currency": "INR",
        "primaryExchange": "NSE",
        "primarySymbol": symbol,
        "status": "ACTIVE",
        "providerMappings": mappings,
    }


def _search_universe_item(global_id: UUID, *, company_name: str, isin: str, symbol: str,
                           nse_status: str = "VERIFIED") -> dict:
    """A portfolio-service enumerate()/search-universe item (InstrumentUniverseItem shape)."""
    return {
        "globalInstrumentId": str(global_id),
        "canonicalName": company_name,
        "ticker": symbol,
        "exchange": "NSE",
        "country": "IN",
        "currency": "INR",
        "status": "ACTIVE",
        "assetType": "EQUITY",
        "isin": isin,
        "providerMappings": [{"provider": "NSE", "providerSymbol": symbol, "status": nse_status, "exchange": "NSE"}],
    }


# --------------------------------------------------------------------------
# A-D: the four required global-search query modes
# --------------------------------------------------------------------------

def test_A_exact_ticker_search_finds_active_nse_equity() -> None:
    gid = uuid4()
    item = _normalize_search_item(_search_universe_item(
        gid, company_name="Ridgeline Components Limited", isin="INE900R01010", symbol="RIDGELINE",
    ))
    assert _search_item_matches(item, "ridgeline")
    assert belongs_to_region(item, "INDIA")
    assert _search_rank(item, "ridgeline") == 0


def test_B_exact_company_name_search_finds_active_nse_equity() -> None:
    gid = uuid4()
    item = _normalize_search_item(_search_universe_item(
        gid, company_name="Ridgeline Components Limited", isin="INE900R01010", symbol="RIDGELINE",
    ))
    assert _search_item_matches(item, "ridgeline components limited")


def test_C_partial_company_name_search_finds_active_nse_equity() -> None:
    gid = uuid4()
    item = _normalize_search_item(_search_universe_item(
        gid, company_name="Ridgeline Components Limited", isin="INE900R01010", symbol="RIDGELINE",
    ))
    assert _search_item_matches(item, "ridgeline compon")


def test_D_exact_isin_search_finds_active_nse_equity() -> None:
    gid = uuid4()
    item = _normalize_search_item(_search_universe_item(
        gid, company_name="Ridgeline Components Limited", isin="INE900R01010", symbol="RIDGELINE",
    ))
    assert _search_item_matches(item, "ine900r01010")
    assert _search_rank(item, "ine900r01010") == 0


# --------------------------------------------------------------------------
# E: search result click-through resolves the *same* globalInstrumentId
# --------------------------------------------------------------------------

def test_E_click_through_resolves_same_global_instrument_id() -> None:
    orchestrator, repo = _orchestrator()
    gid = uuid4()
    payload = _payload(gid, company_name="Ridgeline Components Limited", isin="INE900R01010", symbol="RIDGELINE")

    # Readiness path.
    assert orchestrator.register_global_profile_metadata(gid, payload) is True
    assert repo.profile(gid).instrument_id == gid

    # Presentation path (a second, independent click-through call for the
    # same search result).
    company = asyncio.run(orchestrator.read_global_company_state(gid, metadata=payload))
    assert company.instrument_id == gid


# --------------------------------------------------------------------------
# F: resolution never requires Yahoo mapping/coverage (no-Yahoo small/micro-cap)
# --------------------------------------------------------------------------

def test_F_no_yahoo_small_cap_resolves_without_yahoo_mapping() -> None:
    orchestrator, repo = _orchestrator()
    gid = uuid4()
    payload = _payload(gid, company_name="Microcap Fasteners Limited", isin="INE901M01010", symbol="MICROFAST")
    assert "YAHOO_FINANCE" not in {m["provider"] for m in payload["providerMappings"]}

    assert orchestrator.register_global_profile_metadata(gid, payload) is True
    profile = repo.profile(gid)
    assert profile.instrument_id == gid
    assert "YAHOO_FINANCE" not in profile.provider_instrument_ids

    company = asyncio.run(orchestrator.read_global_company_state(gid, metadata=payload))
    assert company.instrument_id == gid
    assert company.status != "COMPANY_NOT_RESOLVED"


# --------------------------------------------------------------------------
# G: an unrelated company is never returned for a query that does not match it
# --------------------------------------------------------------------------

def test_G_wrong_company_is_not_matched_by_search() -> None:
    gid = uuid4()
    item = _normalize_search_item(_search_universe_item(
        gid, company_name="Ridgeline Components Limited", isin="INE900R01010", symbol="RIDGELINE",
    ))
    assert not _search_item_matches(item, "totally unrelated corp")
    assert not _search_item_matches(item, "ine999z99999")


# --------------------------------------------------------------------------
# H: Gate-1 diagnostic reason codes are preserved (COMPANY_NOT_RESOLVED:<REASON>)
# --------------------------------------------------------------------------

def test_H_gate1_diagnostic_reason_is_populated_on_rejection() -> None:
    orchestrator, repo = _orchestrator()
    gid = uuid4()
    payload = _payload(gid, company_name="Ridgeline Components Limited", isin="INE900R01010", symbol="RIDGELINE")
    payload["primarySymbol"] = None
    payload["providerMappings"] = []  # no primarySymbol and no trusted mapping fallback -> MISSING_TICKER
    reason: dict = {}
    assert orchestrator.register_global_profile_metadata(gid, payload, reason_out=reason) is False
    assert reason.get("reason") == "MISSING_TICKER"


# --------------------------------------------------------------------------
# I: exact-ISIN priority regression lock
# --------------------------------------------------------------------------

def test_I_exact_isin_priority_regression() -> None:
    gid = uuid4()
    item = _normalize_search_item(_search_universe_item(
        gid, company_name="Ridgeline Components Limited", isin="INE900R01010", symbol="RIDGELINE",
    ))
    assert _search_rank(item, "ine900r01010") == 0
    assert _search_rank(item, "ine900r01010") < _search_rank(item, "ridge")


# --------------------------------------------------------------------------
# J: VERIFIED-NSE-only trust regression lock through the presentation
# (_resolve_profile) path specifically -- proves the match-priority fix did
# not loosen NSE identity trust.
# --------------------------------------------------------------------------

def test_J_verified_nse_trust_regression_still_enforced_through_resolve_profile() -> None:
    orchestrator, repo = _orchestrator()
    gid = uuid4()
    payload = _payload(gid, company_name="Ridgeline Components Limited", isin="INE900R01010", symbol="RIDGELINE",
                        nse_status="RESOLVED")
    company = asyncio.run(orchestrator.read_global_company_state(gid, metadata=payload))
    assert company.instrument_id == gid
    profile = repo.profile(gid)
    assert "NSE" not in profile.provider_instrument_ids


# --------------------------------------------------------------------------
# K: generic AWHCL-like regression -- a search-visible canonical instrument
# must not be shadowed by a *different*, earlier-iterated, portfolio-
# specific profile that merely shares its ISIN. No company here is named
# AWHCL; the scenario is built from a synthetic company to prove the fix is
# generic, not a special case.
# --------------------------------------------------------------------------

def test_K_generic_search_visible_instrument_is_not_shadowed_by_a_stale_portfolio_profile() -> None:
    orchestrator, repo = _orchestrator()
    shared_isin = "INE902S01010"

    # A pre-existing profile registered earlier from a portfolio holding,
    # under a *different*, legacy/broker-specific instrument_id, that
    # happens to share the canonical instrument's ISIN (e.g. the position
    # was resolved before the global instrument was ever canonicalized).
    stale_id = uuid4()
    stale = CompanyResearchProfile(
        instrument_id=stale_id, company_id=uuid4(), company_name="Summit Cable Works Limited",
        isin=shared_isin, ticker="SUMMITCABLE", exchange="NSE", mic="NSE", country="IN", currency="INR",
        provider_instrument_ids={"LEGACYBROKER": "broker-internal-id-42"},
    )
    repo.profiles.append(stale)  # appended first -> iterates before the canonical profile below

    # The canonical global instrument for the same company, already
    # registered under its own globalInstrumentId (e.g. by an earlier
    # readiness call for the same search result).
    canonical_id = uuid4()
    payload = _payload(canonical_id, company_name="Summit Cable Works Limited", isin=shared_isin, symbol="SUMMITCABLE")
    assert orchestrator.register_global_profile_metadata(canonical_id, payload) is True
    assert repo.profile(canonical_id).instrument_id == canonical_id
    assert repo.profiles.index(stale) < len(repo.profiles) - 1 or repo.profiles[0] is stale

    # A second, independent click-through call for the *same* search
    # result (presentation/events/documents/summary all share this path)
    # must resolve to the canonical profile -- not the stale one, even
    # though the stale one iterates first and matches via ISIN.
    company = asyncio.run(orchestrator.read_global_company_state(canonical_id, metadata=payload))
    assert company.instrument_id == canonical_id
    assert company.instrument_id != stale_id
    assert company.company_name == "Summit Cable Works Limited"


# --------------------------------------------------------------------------
# L: readiness and presentation paths agree for a brand-new canonical
# instrument with no pre-existing profile of any kind (baseline sanity --
# the two independent click-through calls a search-result click fires in
# parallel must never diverge).
# --------------------------------------------------------------------------

def test_L_readiness_and_presentation_paths_agree_on_the_same_global_instrument_id() -> None:
    orchestrator, repo = _orchestrator()
    gid = uuid4()
    payload = _payload(gid, company_name="Northbank Logistics Limited", isin="INE903N01010", symbol="NORTHBANK")

    assert orchestrator.register_global_profile_metadata(gid, payload) is True
    readiness_profile = repo.profile(gid)

    company = asyncio.run(orchestrator.read_global_company_state(gid, metadata=payload))

    assert readiness_profile.instrument_id == gid
    assert company.instrument_id == gid
    assert len([p for p in repo.profiles if p.instrument_id == gid]) == 1
