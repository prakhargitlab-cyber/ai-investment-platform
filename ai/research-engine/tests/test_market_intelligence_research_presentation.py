from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

import app.main as main
from app.models import PortfolioResearchCompany
from app.portfolio_orchestration import PortfolioResearchOrchestrator


@pytest.mark.asyncio
async def test_global_company_state_reuses_global_identity_without_portfolio_position_or_provider():
    instrument_id = uuid4()
    observed = {}
    metadata = {
        "canonicalName": "Graphite India Ltd.",
        "primarySymbol": "GRAPHITE",
        "primaryExchange": "NSE",
        "country": "IN",
        "currency": "INR",
        "assetType": "EQUITY",
        "providerMappings": [],
    }

    async def global_metadata(value, **kwargs):
        observed["metadata"] = (value, kwargs)
        return metadata

    async def read_state(instrument):
        observed["instrument"] = instrument
        return PortfolioResearchCompany(
            instrument_id=instrument_id,
            company_name="Graphite India Ltd.",
            status="RESOLVED_RESEARCH_AVAILABLE",
        )

    harness = SimpleNamespace(
        global_instrument_metadata=global_metadata,
        _read_company_state=read_state,
    )
    result = await PortfolioResearchOrchestrator.read_global_company_state(
        harness,
        instrument_id,
        correlation_id="presentation-test",
        identity_headers={"X-AIP-User-Id": "user"},
    )

    assert result.instrument_id == instrument_id
    assert observed["metadata"][0] == instrument_id
    assert observed["instrument"]["instrumentId"] == str(instrument_id)
    assert observed["instrument"]["globalInstrumentId"] == str(instrument_id)
    assert "quantity" not in observed["instrument"]
    assert "portfolioId" not in observed["instrument"]


@pytest.mark.asyncio
async def test_presentation_route_validates_canonical_region_and_passes_identity(monkeypatch):
    instrument_id = uuid4()
    observed = {}

    async def metadata(value, **kwargs):
        observed["metadata"] = (value, kwargs)
        return {
            "canonicalName": "Graphite India Ltd.",
            "primarySymbol": "GRAPHITE",
            "primaryExchange": "NSE",
            "country": "IN",
            "currency": "INR",
            "assetType": "EQUITY",
            "providerMappings": [],
        }

    async def presentation(value, **kwargs):
        observed["presentation"] = (value, kwargs)
        return PortfolioResearchCompany(
            instrument_id=instrument_id,
            company_name="Graphite India Ltd.",
            status="RESOLVED_RESEARCH_AVAILABLE",
        )

    monkeypatch.setattr(main.portfolio_orchestrator, "global_instrument_metadata", metadata)
    monkeypatch.setattr(main.portfolio_orchestrator, "read_global_company_state", presentation)
    result = await main.global_company_research_presentation(
        instrument_id,
        region="INDIA",
        x_correlation_id="presentation-route",
        x_aip_user_id="user",
        x_aip_user_issuer="gateway",
        x_aip_user_subject="subject",
    )

    assert result.instrument_id == instrument_id
    assert observed["presentation"][1]["metadata"]["country"] == "IN"
    assert observed["presentation"][1]["identity_headers"]["X-AIP-User-Id"] == "user"

    with pytest.raises(HTTPException) as mismatch:
        await main.global_company_research_presentation(
            instrument_id,
            region="EUROPE",
            x_aip_user_id="user",
            x_aip_user_issuer="gateway",
            x_aip_user_subject="subject",
        )
    assert mismatch.value.status_code == 409
    assert mismatch.value.detail == "INSTRUMENT_REGION_MISMATCH"


@pytest.mark.asyncio
async def test_presentation_route_requires_authentication(monkeypatch):
    monkeypatch.setattr(
        main.portfolio_orchestrator,
        "global_instrument_metadata",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("auth is checked first")),
    )
    with pytest.raises(HTTPException) as denied:
        await main.global_company_research_presentation(
            uuid4(),
            region="INDIA",
            x_aip_user_id=None,
            x_aip_user_issuer=None,
            x_aip_user_subject=None,
        )
    assert denied.value.status_code == 401
