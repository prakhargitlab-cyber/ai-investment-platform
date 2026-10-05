"""Interface fixtures only: no unverified NSE endpoint/client is instantiated."""
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.research_readiness_runtime import ExistingResearchCapabilityExecutor
from test_di15_financial_authority_upgrade import _repo, _facts
from test_research_readiness_runtime import _target


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["complete", "partial", "failure", "invalid_identity", "invalid_source"])
async def test_optional_official_structured_provider_only_avoids_pdf_when_actual_evidence_suffices(state):
    repo, profile = _repo()
    facts = _facts(repo, official=True)
    if state == "partial":
        facts = facts[:1]
    if state == "invalid_identity":
        from uuid import uuid4
        facts = [replace(fact, key=replace(fact.key, instrument_id=uuid4())) for fact in facts]
    if state == "invalid_source":
        facts = [replace(fact, value=fact.value.model_copy(update={"source_url": "https://untrusted.test/results"})) for fact in facts]
    provider = SimpleNamespace(provider_name="NSE", collect=AsyncMock(return_value=facts))
    if state == "failure":
        provider.collect.side_effect = TimeoutError()
    repo.refresh_targeted_categories = AsyncMock()
    executor = ExistingResearchCapabilityExecutor(repo, SimpleNamespace(), None, official_financial_provider=provider)
    await executor.execute_primary(profile.instrument_id, [_target("QUARTERLY_FINANCIALS")],
        jurisdiction="INDIA", correlation_id=None, identity_headers=None)
    provider.collect.assert_awaited_once_with(profile)
    if state == "complete":
        repo.refresh_targeted_categories.assert_not_awaited()
        assert repo.financial_facts_for(profile.instrument_id)
    else:
        repo.refresh_targeted_categories.assert_awaited_once()
        assert repo.refresh_targeted_categories.call_args.args[1] == {"FINANCIAL_RESULTS"}
    if state in {"failure", "invalid_identity", "invalid_source"}:
        assert repo.financial_facts_for(profile.instrument_id) == []


def test_official_structured_feed_disabled_without_injection():
    executor = ExistingResearchCapabilityExecutor(SimpleNamespace(), SimpleNamespace(), None)
    assert executor.official_financial_provider is None
