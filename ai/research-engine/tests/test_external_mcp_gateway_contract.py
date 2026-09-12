from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from app.research_readiness import ProviderFallbackPolicy, ResearchRequirementStatus


INSTRUMENT_ID = UUID("11111111-1111-4111-8111-111111111111")


def test_fallback_policy_issues_narrow_serializable_mcp_authorization() -> None:
    issued_at = datetime(2026, 9, 11, 10, 0, tzinfo=timezone.utc)
    authorization = ProviderFallbackPolicy().authorize_external_tool(
        global_instrument_id=INSTRUMENT_ID,
        requirement_id="CURRENT_NEWS",
        status=ResearchRequirementStatus.MISSING,
        confidence=None,
        permitted_provider_ids=("FUTURE_INDIA_MCP",),
        now=issued_at,
    )

    assert authorization is not None
    assert authorization.as_gateway_payload() == {
        "authorized": True,
        "issuedBy": "ProviderFallbackPolicy",
        "globalInstrumentId": str(INSTRUMENT_ID),
        "requirementId": "CURRENT_NEWS",
        "permittedProviderIds": ["FUTURE_INDIA_MCP"],
        "issuedAt": issued_at.isoformat(),
    }


def test_fallback_policy_does_not_authorize_ready_high_confidence_requirement() -> None:
    authorization = ProviderFallbackPolicy().authorize_external_tool(
        global_instrument_id=INSTRUMENT_ID,
        requirement_id="CURRENT_NEWS",
        status=ResearchRequirementStatus.READY_FRESH,
        confidence=0.99,
        permitted_provider_ids=("FUTURE_INDIA_MCP",),
    )

    assert authorization is None
