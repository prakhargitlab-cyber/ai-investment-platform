"""Partial acceptance is internal, financial-only, and never a tool parameter."""
import pytest

from app.contracts import McpErrorCode, McpGatewayError
from test_yahoo_finance_mcp import provider, mapping, payload, arguments


@pytest.mark.asyncio
@pytest.mark.parametrize('requirement,capability', [
    ('QUARTERLY_FINANCIALS', 'QUARTERLY_FINANCIALS'), ('GROWTH_FACTS', 'GROWTH_INPUTS'),
    ('BUSINESS_QUALITY_FACTS', 'ANNUAL_FINANCIALS'), ('BALANCE_SHEET_FACTS', 'BALANCE_SHEET')])
async def test_partial_fact_allowed_only_for_explicit_financial_gap_mode(requirement, capability):
    adapter, client = provider(mapping(requirement, capability, 'get_financials', requiredFields=['revenue', 'pat']),
        payload(facts=[{'metric': 'eps', 'value': '0', 'unit': 'INR/share',
                       'periodEnd': '2026-06-30', 'periodType': 'QUARTERLY', 'reportingBasis': 'STANDALONE'}]))
    with pytest.raises(McpGatewayError) as error:
        await adapter.invoke(tool='get_financials', arguments=arguments(requirement), request_id='strict')
    assert error.value.code == McpErrorCode.EXTERNAL_RESULT_INCOMPLETE
    result = await adapter.invoke(tool='get_financials',
        arguments=arguments(requirement, financialGapFill=True), request_id='gap')
    assert len(result['financialFacts']) == 1 and result['financialFacts'][0]['value'] == '0'
    assert all('financialGapFill' not in call[1] for call in client.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize('requirement,capability,region', [
    ('SHAREHOLDING', 'SHAREHOLDING', 'INDIA'), ('VALUATION_INPUTS', 'VALUATION_INPUTS', 'INDIA'),
    ('QUARTERLY_FINANCIALS', 'QUARTERLY_FINANCIALS', 'USA')])
async def test_gap_mode_rejects_unrelated_scope(requirement, capability, region):
    adapter, client = provider(mapping(requirement, capability, 'get_financials', region=region))
    with pytest.raises(McpGatewayError) as error:
        await adapter.invoke(tool='get_financials',
            arguments=arguments(requirement, region=region, financialGapFill=True), request_id='scope')
    assert error.value.code == McpErrorCode.INVALID_ARGUMENT
    assert not client.calls


@pytest.mark.asyncio
async def test_empty_financial_gap_result_is_still_incomplete():
    adapter, _ = provider(mapping('QUARTERLY_FINANCIALS', 'QUARTERLY_FINANCIALS', 'get_financials'))
    with pytest.raises(McpGatewayError) as error:
        await adapter.invoke(tool='get_financials',
            arguments=arguments('QUARTERLY_FINANCIALS', financialGapFill=True), request_id='empty')
    assert error.value.code == McpErrorCode.EXTERNAL_RESULT_INCOMPLETE


def test_internal_http_route_forwards_gap_mode_without_changing_tool_arguments():
    import json
    from starlette.testclient import TestClient
    from app.server import create_container, create_mcp_server
    from app.settings import McpGatewaySettings
    from conftest import FakeApplicationReader, INSTRUMENT_ID, NOW, RecordingAudit
    from test_yahoo_finance_mcp import FakeClient, FakeFactory, PROVIDER_ID
    requirement = 'QUARTERLY_FINANCIALS'
    settings = McpGatewaySettings(AIP_ENVIRONMENT='TEST', AIP_MCP_AUTHENTICATION_TYPE='TEST',
        AIP_MCP_EXTERNAL_PROVIDERS_ENABLED='true', AIP_MCP_YAHOO_ENABLED='true',
        AIP_MCP_YAHOO_TRANSPORT='stdio', AIP_MCP_YAHOO_STDIO_COMMAND='fake-yahoo-mcp',
        AIP_MCP_YAHOO_CAPABILITIES_JSON=json.dumps([mapping(requirement, requirement, 'get_financials')]),
        AIP_MCP_YAHOO_MAX_RETRIES='0', AIP_MCP_EXTERNAL_CALLER_IDENTITIES='research-engine')
    container = create_container(settings, reader=FakeApplicationReader(), audit=RecordingAudit())
    adapter = container.external_registry.get(PROVIDER_ID)
    tool_client = FakeClient(payload(facts=[{'metric': 'eps', 'value': '0', 'unit': 'INR/share',
        'periodEnd': '2026-06-30', 'periodType': 'QUARTERLY', 'reportingBasis': 'STANDALONE'}]),
        tools=('get_financials',))
    adapter.client_factory = FakeFactory(tool_client)
    adapter.clock = lambda: NOW
    body = {**arguments(requirement), 'providerId': PROVIDER_ID, 'financialGapFill': True,
        'authorization': {'authorized': True, 'issuedBy': 'ProviderFallbackPolicy',
            'globalInstrumentId': str(INSTRUMENT_ID), 'requirementId': requirement,
            'permittedProviderIds': [PROVIDER_ID], 'issuedAt': NOW.isoformat()}}
    with TestClient(create_mcp_server(container).streamable_http_app(stateless_http=True, json_response=True)) as client:
        response = client.post('/internal/v1/external-research/acquire', json=body,
            headers={'X-AIP-Service-Identity': 'research-engine'})
    assert response.status_code == 200
    assert len(response.json()['data']['financialFacts']) == 1
    assert 'financialGapFill' not in tool_client.calls[0][1]
