from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
import uvicorn

from app.models import CompanyResearchProfile
from app.persistence import SqliteResearchPersistence
from app.research_readiness import ResearchRequirementStatus
from app.research_readiness_runtime import (
    CapabilityExecutionResult,
    RepositoryResearchReadinessAdapter,
    ResearchReadinessRuntime,
)
from app.repository import ResearchRepository
from app.settings import Settings
from app.yahoo_mcp_acquisition import (
    HttpExternalResearchToolGateway,
    McpFirstResearchCapabilityExecutor,
)
from fake_yahoo import factory
from yahoo_mcp_server.acquisition import YahooAcquisitionService
from yahoo_mcp_server.audit import YahooMcpAudit
from yahoo_mcp_server.server import create_yahoo_mcp_server
from yahoo_mcp_server.settings import YahooMcpSettings


ROOT = Path(__file__).resolve().parents[3]
INSTRUMENT_ID = UUID("22222222-2222-4222-8222-222222222222")


class RecordingYahooAudit(YahooMcpAudit):
    def __init__(self) -> None:
        self.events = []

    def emit(self, event, **values) -> None:
        self.events.append({"event": event, **values})


class RecordingFallback:
    def __init__(self) -> None:
        self.primary_calls = []
        self.secondary_calls = []

    async def execute_primary(self, instrument_id, targets, **kwargs):
        self.primary_calls.append((instrument_id, tuple(targets), kwargs))
        return CapabilityExecutionResult(
            ("REGIONAL_FALLBACK",),
            {},
            tuple(item.requirement_id for item in targets),
        )

    async def execute_approved_fallbacks(self, instrument_id, targets):
        self.secondary_calls.append((instrument_id, tuple(targets)))
        return CapabilityExecutionResult(
            ("REGIONAL_SECONDARY",),
            {},
            tuple(item.requirement_id for item in targets),
        )


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _capability(requirement, capability, tool, **values):
    return {
        "region": "INDIA",
        "requirementId": requirement,
        "capability": capability,
        "tool": tool,
        "state": "SUPPORTED",
        **values,
    }


async def _wait_for_health(url: str, process=None) -> None:
    for _ in range(100):
        if process is not None and process.poll() is not None:
            raise AssertionError(f"gateway exited before health check: {process.returncode}")
        try:
            async with httpx.AsyncClient(timeout=0.2) as client:
                if (await client.get(url)).status_code == 200:
                    return
        except httpx.HTTPError:
            pass
        await asyncio.sleep(0.05)
    raise AssertionError(f"service did not become healthy: {url}")


@asynccontextmanager
async def _actual_stack(scenario: str, capability: dict | None = None):
    yahoo_port, gateway_port = _free_port(), _free_port()
    yahoo_settings = YahooMcpSettings(
        AIP_ENVIRONMENT="TEST",
        AIP_YAHOO_MCP_TRANSPORT="streamable-http",
        AIP_YAHOO_MCP_HOST="127.0.0.1",
        AIP_YAHOO_MCP_PORT=yahoo_port,
        AIP_YAHOO_MCP_ALLOWED_HOSTS="127.0.0.1,127.0.0.1:*",
    )
    audit = RecordingYahooAudit()
    acquisition = YahooAcquisitionService(yahoo_settings, ticker_factory=factory(scenario))
    yahoo_app = create_yahoo_mcp_server(
        yahoo_settings, acquisition=acquisition, audit=audit
    ).streamable_http_app(stateless_http=True, json_response=True)
    yahoo_uvicorn = uvicorn.Server(
        uvicorn.Config(yahoo_app, host="127.0.0.1", port=yahoo_port, log_level="warning")
    )
    yahoo_task = asyncio.create_task(yahoo_uvicorn.serve())
    gateway = None
    try:
        await _wait_for_health(f"http://127.0.0.1:{yahoo_port}/health")
        environment = {
            **os.environ,
            "AIP_ENVIRONMENT": "TEST",
            "AIP_FEATURE_MCP_ENABLED": "true",
            "AIP_MCP_TRANSPORT": "streamable-http",
            "AIP_MCP_HOST": "127.0.0.1",
            "AIP_MCP_PORT": str(gateway_port),
            "AIP_MCP_ALLOWED_HOSTS": "127.0.0.1,127.0.0.1:*",
            "AIP_MCP_EXTERNAL_PROVIDERS_ENABLED": "true",
            "AIP_MCP_EXTERNAL_CALLER_IDENTITIES": "research-engine",
            "AIP_MCP_YAHOO_ENABLED": "true",
            "AIP_MCP_YAHOO_TRANSPORT": "streamable-http",
            "AIP_MCP_YAHOO_ENDPOINT": f"http://127.0.0.1:{yahoo_port}/mcp",
            "AIP_MCP_YAHOO_AUTH_TYPE": "NONE",
            "AIP_MCP_YAHOO_CAPABILITIES_JSON": json.dumps(
                [capability or _capability("LATEST_PRICE", "LATEST_PRICE", "get_quote", maxAgeSeconds=600)]
            ),
            "AIP_MCP_YAHOO_MAX_RETRIES": "0",
        }
        gateway = subprocess.Popen(
            [
                str(ROOT / "ai" / "mcp-gateway" / ".venv" / "Scripts" / "python.exe"),
                "-m",
                "app.main",
                "--transport",
                "streamable-http",
            ],
            cwd=ROOT / "ai" / "mcp-gateway",
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        await _wait_for_health(f"http://127.0.0.1:{gateway_port}/health", gateway)
        yield gateway_port, audit
    finally:
        if gateway is not None and gateway.poll() is None:
            gateway.terminate()
            try:
                gateway.wait(timeout=5)
            except subprocess.TimeoutExpired:
                gateway.kill()
                gateway.wait(timeout=5)
        yahoo_uvicorn.should_exit = True
        await asyncio.wait_for(yahoo_task, timeout=5)


@pytest.mark.asyncio
async def test_targeted_ensure_uses_complete_offline_mcp_chain_and_rereads_readiness():
    yahoo_port, gateway_port = _free_port(), _free_port()
    yahoo_settings = YahooMcpSettings(
        AIP_ENVIRONMENT="TEST",
        AIP_YAHOO_MCP_TRANSPORT="streamable-http",
        AIP_YAHOO_MCP_HOST="127.0.0.1",
        AIP_YAHOO_MCP_PORT=yahoo_port,
        AIP_YAHOO_MCP_ALLOWED_HOSTS="127.0.0.1,127.0.0.1:*",
    )
    audit = RecordingYahooAudit()
    acquisition = YahooAcquisitionService(yahoo_settings, ticker_factory=factory())
    yahoo_app = create_yahoo_mcp_server(
        yahoo_settings, acquisition=acquisition, audit=audit
    ).streamable_http_app(stateless_http=True, json_response=True)
    yahoo_uvicorn = uvicorn.Server(
        uvicorn.Config(
            yahoo_app,
            host="127.0.0.1",
            port=yahoo_port,
            log_level="warning",
            lifespan="on",
        )
    )
    yahoo_task = asyncio.create_task(yahoo_uvicorn.serve())
    gateway = None
    try:
        await _wait_for_health(f"http://127.0.0.1:{yahoo_port}/health")
        capabilities = json.dumps(
            [_capability("LATEST_PRICE", "LATEST_PRICE", "get_quote", maxAgeSeconds=600)]
        )
        environment = {
            **os.environ,
            "AIP_ENVIRONMENT": "TEST",
            "AIP_FEATURE_MCP_ENABLED": "true",
            "AIP_MCP_TRANSPORT": "streamable-http",
            "AIP_MCP_HOST": "127.0.0.1",
            "AIP_MCP_PORT": str(gateway_port),
            "AIP_MCP_ALLOWED_HOSTS": "127.0.0.1,127.0.0.1:*",
            "AIP_MCP_EXTERNAL_PROVIDERS_ENABLED": "true",
            "AIP_MCP_EXTERNAL_CALLER_IDENTITIES": "research-engine",
            "AIP_MCP_YAHOO_ENABLED": "true",
            "AIP_MCP_YAHOO_TRANSPORT": "streamable-http",
            "AIP_MCP_YAHOO_ENDPOINT": f"http://127.0.0.1:{yahoo_port}/mcp",
            "AIP_MCP_YAHOO_AUTH_TYPE": "NONE",
            "AIP_MCP_YAHOO_CAPABILITIES_JSON": capabilities,
            "AIP_MCP_YAHOO_MAX_RETRIES": "0",
        }
        gateway_python = ROOT / "ai" / "mcp-gateway" / ".venv" / "Scripts" / "python.exe"
        gateway = subprocess.Popen(
            [str(gateway_python), "-m", "app.main", "--transport", "streamable-http"],
            cwd=ROOT / "ai" / "mcp-gateway",
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        await _wait_for_health(f"http://127.0.0.1:{gateway_port}/health", gateway)

        repository = ResearchRepository(
            settings=Settings(research_live_enabled=False, research_demo_enabled=False),
            persistence=SqliteResearchPersistence(),
        )
        repository.profiles.append(
            CompanyResearchProfile(
                instrument_id=INSTRUMENT_ID,
                company_id=uuid4(),
                company_name="Hindustan Aeronautics Limited",
                ticker="HAL",
                exchange="NSE",
                mic="XNSE",
                country="IN",
                currency="INR",
                provider_instrument_ids={"YAHOO_FINANCE": "HAL.NS"},
            )
        )
        fallback = RecordingFallback()
        executor = McpFirstResearchCapabilityExecutor(
            fallback,
            repository,
            HttpExternalResearchToolGateway(
                f"http://127.0.0.1:{gateway_port}", 3, "research-engine"
            ),
            enabled=True,
        )
        source = RepositoryResearchReadinessAdapter(repository)
        runtime = ResearchReadinessRuntime(repository, source, executor)

        before = await runtime.read(INSTRUMENT_ID, jurisdiction="INDIA")
        assert before.for_requirement("LATEST_PRICE").status is ResearchRequirementStatus.MISSING
        assert audit.events == []
        ensured = await runtime.ensure(
            INSTRUMENT_ID,
            jurisdiction="INDIA",
            requirement_ids=("LATEST_PRICE",),
            correlation_id="offline-chain-5c",
        )
        after = await runtime.read(INSTRUMENT_ID, jurisdiction="INDIA")

        assert ensured.executed_capabilities == ("YAHOO_FINANCE_MCP:LATEST_PRICE",)
        assert after.for_requirement("LATEST_PRICE").status is ResearchRequirementStatus.READY_FRESH
        assert fallback.primary_calls == [] and fallback.secondary_calls == []
        assert [item["event"] for item in audit.events] == [
            "YAHOO_MCP_REQUEST",
            "YAHOO_MCP_SUCCESS",
        ]
        assert audit.events[0]["request_id"] == "offline-chain-5c"
        records = repository.structured_market_snapshots_for({INSTRUMENT_ID})[INSTRUMENT_ID]
        assert records[-1].provider == "YAHOO_FINANCE_MCP"
        assert records[-1].provider_instrument_id == "HAL.NS"
    finally:
        if gateway is not None and gateway.poll() is None:
            gateway.terminate()
            try:
                gateway.wait(timeout=5)
            except subprocess.TimeoutExpired:
                gateway.kill()
                gateway.wait(timeout=5)
        yahoo_uvicorn.should_exit = True
        await asyncio.wait_for(yahoo_task, timeout=5)


@pytest.mark.asyncio
async def test_unsupported_shareholding_skips_yahoo_and_invokes_regional_fallback_once():
    repository = ResearchRepository(
        settings=Settings(research_live_enabled=False, research_demo_enabled=False),
        persistence=SqliteResearchPersistence(),
    )
    repository.profiles.append(
        CompanyResearchProfile(
            instrument_id=INSTRUMENT_ID,
            company_id=uuid4(),
            company_name="RBL Bank Limited",
            ticker="RBLBANK",
            exchange="NSE",
            mic="XNSE",
            country="IN",
            currency="INR",
            provider_instrument_ids={"YAHOO_FINANCE": "RBLBANK.NS"},
        )
    )

    class UnsupportedGateway:
        calls = 0

        async def acquire_requirement(self, *_args, **_kwargs):
            self.calls += 1
            from app.yahoo_mcp_acquisition import ExternalMcpAcquisitionError

            raise ExternalMcpAcquisitionError("EXTERNAL_CAPABILITY_UNSUPPORTED")

    fallback, gateway = RecordingFallback(), UnsupportedGateway()
    executor = McpFirstResearchCapabilityExecutor(
        fallback, repository, gateway, enabled=True
    )
    readiness = await ResearchReadinessRuntime(
        repository, RepositoryResearchReadinessAdapter(repository), executor
    ).ensure(
        INSTRUMENT_ID,
        jurisdiction="INDIA",
        requirement_ids=("SHAREHOLDING",),
        correlation_id="shareholding-fallback-5c",
    )
    assert gateway.calls == 1
    assert len(fallback.primary_calls) == 1
    assert len(fallback.secondary_calls) == 0
    assert readiness.executed_capabilities == ("REGIONAL_FALLBACK",)


@pytest.mark.asyncio
async def test_first_party_incomplete_result_crosses_mcp_and_invokes_fallback_once():
    async with _actual_stack("EMPTY") as (gateway_port, audit):
        repository = ResearchRepository(
            settings=Settings(research_live_enabled=False, research_demo_enabled=False),
            persistence=SqliteResearchPersistence(),
        )
        repository.profiles.append(
            CompanyResearchProfile(
                instrument_id=INSTRUMENT_ID,
                company_id=uuid4(),
                company_name="Hindustan Aeronautics Limited",
                ticker="HAL",
                exchange="NSE",
                mic="XNSE",
                country="IN",
                currency="INR",
                provider_instrument_ids={"YAHOO_FINANCE": "HAL.NS"},
            )
        )
        fallback = RecordingFallback()
        executor = McpFirstResearchCapabilityExecutor(
            fallback,
            repository,
            HttpExternalResearchToolGateway(
                f"http://127.0.0.1:{gateway_port}", 3, "research-engine"
            ),
            enabled=True,
        )
        result = await ResearchReadinessRuntime(
            repository, RepositoryResearchReadinessAdapter(repository), executor
        ).ensure(
            INSTRUMENT_ID,
            jurisdiction="INDIA",
            requirement_ids=("LATEST_PRICE",),
            correlation_id="offline-incomplete-5c",
        )
        assert len(fallback.primary_calls) == 1
        assert fallback.secondary_calls == []
        assert result.executed_capabilities == ("REGIONAL_FALLBACK",)
        assert [item["event"] for item in audit.events] == [
            "YAHOO_MCP_REQUEST",
            "YAHOO_MCP_FAILED",
        ]


@pytest.mark.asyncio
async def test_first_party_financial_success_persists_and_skips_fallback():
    capability = _capability(
        "BUSINESS_QUALITY_FACTS", "ANNUAL_FINANCIALS", "get_financials"
    )
    async with _actual_stack("SUCCESS", capability) as (gateway_port, audit):
        repository = ResearchRepository(
            settings=Settings(research_live_enabled=False, research_demo_enabled=False),
            persistence=SqliteResearchPersistence(),
        )
        repository.profiles.append(
            CompanyResearchProfile(
                instrument_id=INSTRUMENT_ID,
                company_id=uuid4(),
                company_name="Hindustan Aeronautics Limited",
                ticker="HAL",
                exchange="NSE",
                mic="XNSE",
                country="IN",
                currency="INR",
                provider_instrument_ids={"YAHOO_FINANCE": "HAL.NS"},
            )
        )
        fallback = RecordingFallback()
        runtime = ResearchReadinessRuntime(
            repository,
            RepositoryResearchReadinessAdapter(repository),
            McpFirstResearchCapabilityExecutor(
                fallback,
                repository,
                HttpExternalResearchToolGateway(
                    f"http://127.0.0.1:{gateway_port}", 3, "research-engine"
                ),
                enabled=True,
            ),
        )
        result = await runtime.ensure(
            INSTRUMENT_ID,
            jurisdiction="INDIA",
            requirement_ids=("BUSINESS_QUALITY_FACTS",),
            correlation_id="offline-financial-5c",
        )
        assert result.executed_capabilities == (
            "YAHOO_FINANCE_MCP:BUSINESS_QUALITY_FACTS",
        )
        assert fallback.primary_calls == [] and fallback.secondary_calls == []
        assert len(repository.financial_facts_for(INSTRUMENT_ID)) >= 6
        assert [item["event"] for item in audit.events] == [
            "YAHOO_MCP_REQUEST",
            "YAHOO_MCP_SUCCESS",
        ]
