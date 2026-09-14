"""Regression tests for global presentation vs. readiness provider-mapping trust alignment.

Canonical rule:
    globalInstrumentId -> verified provider mapping -> provider symbol

`read_global_company_state` (presentation/summary) projects through
`_read_company_state_impl` -> `_resolve_profile` -> `_hydrate_verified_exchange_mappings`,
which RETAINS previously-verified provider mappings (it only adds trusted mappings
from the instrument and never removes stale ones).  The readiness path
(`register_global_profile_metadata` -> `_refresh_profile_from_global_instrument`)
REPLACE`S` the mapping set with verified-only entries via `_trusted_provider_mapping`.

These projections must agree, so `_read_company_state_impl` now re-aligns the global
(provider-less) instrument through `_refresh_profile_from_global_instrument`.  Portfolio
positions always carry a broker `provider`, so the `if not instrument.get("provider")`
guard leaves the portfolio retain path -- and broker-provenance identity -- untouched.
"""
import asyncio
from uuid import UUID, uuid4

from app.portfolio_orchestration import PortfolioResearchOrchestrator
from app.repository import ResearchRepository
from app.settings import Settings


def _global_instrument_payload(global_id: UUID, yahoo_status: str = "VERIFIED") -> dict:
    """A portfolio-service global-instrument master payload (no broker `provider`)."""
    return {
        "globalInstrumentId": str(global_id),
        "canonicalName": "Venus Pipes And Fittings Limited",
        "isin": "INE000V01010",
        "assetType": "EQUITY",
        "country": "IN",
        "currency": "INR",
        "primaryExchange": "NSE",
        "primarySymbol": "VENUSPIPES",
        "providerMappings": [
            {"provider": "NSE", "providerSymbol": "VENUSPIPES", "status": "VERIFIED", "exchange": "NSE"},
            {"provider": "YAHOO_FINANCE", "providerSymbol": "VENUSPIPES.NS", "status": yahoo_status, "exchange": "NSE"},
        ],
    }


def _no_yahoo_payload(global_id: UUID) -> dict:
    """Global instrument whose master mappings contain no Yahoo entry at all."""
    return {
        "globalInstrumentId": str(global_id),
        "canonicalName": "No Yahoo Company Limited",
        "isin": "INE000N01010",
        "assetType": "EQUITY",
        "country": "IN",
        "currency": "INR",
        "primaryExchange": "NSE",
        "primarySymbol": "NOYAHOO",
        "providerMappings": [
            {"provider": "NSE", "providerSymbol": "NOYAHOO", "status": "VERIFIED", "exchange": "NSE"},
        ],
    }


class _OfflineClient:
    """Asserts the offline global presentation path performs no portfolio-service HTTP."""

    async def get(self, *args, **kwargs):
        raise AssertionError("read_global_company_state(metadata=) must not perform HTTP")

    async def post(self, *args, **kwargs):
        raise AssertionError("read_global_company_state(metadata=) must not perform HTTP")


def _orchestrator(repo: ResearchRepository) -> PortfolioResearchOrchestrator:
    settings = Settings(
        research_demo_enabled=False,
        structured_provider_enabled=False,
        portfolio_service_base_url="http://portfolio-service",
    )
    return PortfolioResearchOrchestrator(repo, settings, client=_OfflineClient())


def test_global_presentation_keeps_verified_yahoo_mapping() -> None:
    global_id = uuid4()
    repo = ResearchRepository(settings=Settings(research_demo_enabled=False))
    orchestrator = _orchestrator(repo)
    payload = _global_instrument_payload(global_id, yahoo_status="VERIFIED")

    company = asyncio.run(orchestrator.read_global_company_state(global_id, metadata=payload))
    assert company.verified_provider_mappings == {
        "NSE": "VENUSPIPES",
        "YAHOO_FINANCE": "VENUSPIPES.NS",
    }


def test_global_presentation_drops_drifted_unverified_yahoo_mapping_from_summary_and_readiness() -> None:
    global_id = uuid4()
    repo = ResearchRepository(settings=Settings(research_demo_enabled=False))
    orchestrator = _orchestrator(repo)
    verified = _global_instrument_payload(global_id, yahoo_status="VERIFIED")
    drifted = _global_instrument_payload(global_id, yahoo_status="INVALID")

    # Seed a process-local profile whose Yahoo mapping was VERIFIED.
    assert orchestrator.register_global_profile_metadata(global_id, verified) is True
    assert repo.profile(global_id).provider_instrument_ids == {
        "NSE": "VENUSPIPES",
        "YAHOO_FINANCE": "VENUSPIPES.NS",
    }

    # The instrument now reports the Yahoo mapping as INVALID.  The presentation
    # projection (read_global_company_state) must not retain the stale, now-unverified
    # Yahoo mapping as verified -- it must agree with the readiness projection.
    company = asyncio.run(orchestrator.read_global_company_state(global_id, metadata=drifted))
    assert "YAHOO_FINANCE" not in company.verified_provider_mappings
    assert company.verified_provider_mappings == {"NSE": "VENUSPIPES"}

    # The readiness projection agrees after its own refresh.
    assert orchestrator.register_global_profile_metadata(global_id, drifted) is True
    assert "YAHOO_FINANCE" not in repo.profile(global_id).provider_instrument_ids


def test_global_presentation_never_guesses_a_yahoo_symbol() -> None:
    global_id = uuid4()
    repo = ResearchRepository(settings=Settings(research_demo_enabled=False))
    orchestrator = _orchestrator(repo)
    payload = _no_yahoo_payload(global_id)

    company = asyncio.run(orchestrator.read_global_company_state(global_id, metadata=payload))
    assert "YAHOO_FINANCE" not in company.verified_provider_mappings
    assert company.verified_provider_mappings == {"NSE": "NOYAHOO"}
