from unittest.mock import AsyncMock

import httpx2
import pytest

from app.contracts import McpErrorCode, McpGatewayError
from test_yahoo_finance_mcp import mapping, provider


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [httpx2.ReadTimeout("private detail"), httpx2.ConnectTimeout("private detail")])
async def test_mcp_transport_timeout_keeps_timeout_classification(error):
    adapter, client = provider(mapping("LATEST_PRICE", "LATEST_PRICE", "get_quote"))
    client.call_tool = AsyncMock(side_effect=error)
    with pytest.raises(McpGatewayError) as caught:
        await adapter._call("get_quote", {}, "history-timeout-test")
    assert caught.value.code == McpErrorCode.DOWNSTREAM_TIMEOUT
    assert client.call_tool.await_count == 1  # Configured retry ceiling remains zero.
