from __future__ import annotations

import asyncio
import threading
from time import monotonic

import httpx
import pytest
from mcp import Client

from fake_yahoo import FakeYahooTicker
from test_first_party_server import arguments, call_tool, server, settings
from yahoo_mcp_server.acquisition import YahooAcquisitionService, YahooMcpServiceError
from yahoo_mcp_server.contracts import IdentityInput
from yahoo_mcp_server.server import create_yahoo_mcp_server


TOOLS = ("get_quote", "get_financials", "get_price_history")


class BlockingYahoo:
    """An upstream call whose physical lifetime is controlled by the test."""

    def __init__(self, capacity, *, fail=False):
        self.capacity = capacity
        self.fail = fail
        self.loop = asyncio.get_running_loop()
        self.started = asyncio.Event()
        self.drained = asyncio.Event()
        self.release = threading.Event()
        self.lock = threading.Lock()
        self.active = self.peak = self.calls = 0
        self.threads = set()

    def block(self):
        with self.lock:
            self.active += 1
            self.calls += 1
            self.peak = max(self.peak, self.active)
            self.threads.add(threading.get_ident())
            if self.active == self.capacity:
                self.loop.call_soon_threadsafe(self.started.set)
        try:
            if not self.release.wait(5):
                raise AssertionError("Test did not release the blocking Yahoo operation")
            if self.fail:
                raise ConnectionError("sensitive upstream detail")
        finally:
            with self.lock:
                self.active -= 1
                if self.active == 0:
                    self.loop.call_soon_threadsafe(self.drained.set)

    def ticker(self, symbol):
        owner = self

        class Ticker(FakeYahooTicker):
            @property
            def info(self):
                owner.block()
                return super().info

            def history(self, **kwargs):
                owner.block()
                return super().history(**kwargs)

        return Ticker(symbol)


def load_server(blocker, *, timeout="0.3"):
    config = settings(
        AIP_YAHOO_MCP_MAX_CONCURRENCY=str(blocker.capacity),
        AIP_YAHOO_MCP_UPSTREAM_TIMEOUT_SECONDS=timeout,
    )
    source = YahooAcquisitionService(config, ticker_factory=blocker.ticker)
    return source, create_yahoo_mcp_server(config, acquisition=source)


async def probe(client):
    started = monotonic()
    health, ready = await asyncio.wait_for(asyncio.gather(
        client.get("/health"), client.get("/health/ready"),
    ), 0.5)
    assert monotonic() - started < 0.5
    assert health.status_code == ready.status_code == 200
    assert health.json()["status"] == "ok"
    assert ready.json()["status"] == "ready"
    assert ready.json()["upstreamRequired"] is False


@pytest.mark.parametrize("tool", TOOLS)
async def test_health_stays_responsive_and_timeout_contract_survives_blocking_yahoo(tool):
    blocker = BlockingYahoo(2)
    source, instance = load_server(blocker)
    app = instance.streamable_http_app(stateless_http=True, json_response=True)
    requests = []
    try:
        async with Client(instance) as mcp, httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost",
        ) as http:
            requests = [asyncio.create_task(mcp.call_tool(tool, arguments())) for _ in range(2)]
            await asyncio.wait_for(blocker.started.wait(), 1)
            assert blocker.active == 2
            assert threading.get_ident() not in blocker.threads
            await probe(http)
            results = await asyncio.wait_for(asyncio.gather(*requests), 1)
            assert all(result.is_error for result in results)
            assert all("YAHOO_MCP_UPSTREAM_TIMEOUT" in str(result.content) for result in results)
            # The request has ended, but the physical upstream work has not.
            assert blocker.active == 2
            await probe(http)
    finally:
        blocker.release.set()
        await asyncio.gather(*requests, return_exceptions=True)
        await asyncio.wait_for(blocker.drained.wait(), 1)
        await source.close()


async def test_repeated_mixed_timeouts_cannot_exceed_worker_limit_and_capacity_recovers():
    blocker = BlockingYahoo(2)
    source, instance = load_server(blocker, timeout="0.15")
    try:
        async with Client(instance) as client:
            for _ in range(2):
                # Includes callers queued for capacity as well as running calls.
                results = await asyncio.wait_for(asyncio.gather(*(
                    client.call_tool(tool, arguments()) for tool in TOOLS
                )), 1)
                assert all(result.is_error for result in results)
                assert all("YAHOO_MCP_UPSTREAM_TIMEOUT" in str(result.content) for result in results)
                assert blocker.active == blocker.peak == blocker.calls == 2
            blocker.release.set()
            await asyncio.wait_for(blocker.drained.wait(), 1)
            result = await client.call_tool("get_price_history", arguments())
            assert not result.is_error
            assert len(result.structured_content["prices"]) == 2
            assert blocker.peak == 2
    finally:
        blocker.release.set()
        await source.close()


@pytest.mark.parametrize("tool", TOOLS)
async def test_upstream_failures_keep_the_existing_safe_tool_error(tool):
    result = await call_tool(server("UPSTREAM_FAILURE"), tool)
    assert result.is_error
    assert "YAHOO_MCP_UPSTREAM_UNAVAILABLE" in str(result.content)
    assert "sensitive" not in str(result.content)


async def test_caller_cancellation_keeps_slot_until_worker_finishes_and_observes_late_error():
    blocker = BlockingYahoo(1, fail=True)
    source, _ = load_server(blocker, timeout="0.15")
    identity = IdentityInput(**arguments())
    unobserved = []
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: unobserved.append(context))
    task = asyncio.create_task(source.snapshot(identity))
    try:
        await asyncio.wait_for(blocker.started.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(YahooMcpServiceError, match="YAHOO_MCP_UPSTREAM_TIMEOUT"):
            await source.closes(identity, lookback_days=400)
        assert blocker.active == blocker.peak == blocker.calls == 1
        blocker.release.set()
        # Slot release follows completion and consumption of the late exception.
        await asyncio.wait_for(source._concurrency.acquire(), 1)
        source._concurrency.release()
        assert not unobserved
    finally:
        blocker.release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await source.close()
        loop.set_exception_handler(previous_handler)
