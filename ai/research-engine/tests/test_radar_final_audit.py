"""Provider-free regressions for the live Radar audit; no company exceptions."""
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest

from app.failure_taxonomy import TECHNICAL_RETRYABLE, classify_reason, classify_requirement_failures
from app.global_opportunity_orchestration import GlobalOpportunityOrchestrator
from app.models import EntityResolution, ShareholdingCategory, ShareholdingSnapshotValue
from app.repository import ResearchRepository, _TRUSTED_NSE_PROFILE_IDENTITY
from app.research_applicability import shareholding_input_coverage
from app.research_fetching import FetchError
from app.stock_rule_engine import AreaScoreStatus, StockRuleEngineV1
import test_stock_rule_engine as sre
from test_full_research_state_contract import _run_stage2, _unscorable_rule, REQ
from test_official_nse_financial_parsing import _profile, _trusted_source
from test_pdf_structure_reuse import _prepare_kwargs


@pytest.mark.parametrize("reason", ["ACQUISITION_NOT_DUE", "RULE_AREA_UNSCORABLE"])
def test_scheduler_or_scorer_gap_is_not_authoritative_absence(reason):
    assert classify_reason(reason) == TECHNICAL_RETRYABLE


@pytest.mark.asyncio
@pytest.mark.parametrize("prior", ["ACQUISITION_NOT_DUE", "DISCOVERY_NO_CANDIDATES", "NETWORK_TIMEOUT"])
async def test_ready_but_unscorable_stays_retryable_and_preserves_prior_reason(monkeypatch, prior):
    result, calls = await _run_stage2(monkeypatch, _unscorable_rule(), failures={REQ: prior})
    diagnostic = result.diagnostics[0]
    assert diagnostic.failure_class == TECHNICAL_RETRYABLE
    assert diagnostic.acquisition_failures[REQ] == prior + "|RULE_AREA_UNSCORABLE"
    assert not diagnostic.rank_eligible and result.top_n == []
    assert result.deep_technical_failure_count == result.deep_readiness_failed_count == 1
    assert result.deep_repair_attempted_count >= 1 and len(calls) >= 2
    assert len(result.diagnostics) == 1


@pytest.mark.parametrize("scenario", ["ownership_without_promoter_or_history", "negative_valuation_only"])
def test_real_scorer_input_gap_blocks_ranking_without_claiming_absence(scenario):
    if scenario == "ownership_without_promoter_or_history":
        snapshot = sre._shareholding()[-1].model_copy(update={"values": [
            ShareholdingSnapshotValue(category=ShareholdingCategory.FII_FPI, percentage=Decimal("24")),
            ShareholdingSnapshotValue(category=ShareholdingCategory.DII, percentage=Decimal("36")),
            ShareholdingSnapshotValue(category=ShareholdingCategory.PUBLIC_RETAIL, percentage=Decimal("20")),
        ]})
        assert "PROMOTER_INSTITUTIONAL_PUBLIC_CATEGORIES" in shareholding_input_coverage(snapshot)
        assert "PROMOTER_PLEDGE" not in shareholding_input_coverage(snapshot)
        value = sre._inputs(shareholding=(snapshot,))
        area_id, requirement = sre.RuleEngineArea.SHAREHOLDING, "SHAREHOLDING"
    else:
        structured = sre._structured(trailingPE=None, forwardPE=None, pegRatio=None,
                                     freeCashFlow=None, priceToBook=Decimal("-8"), evToEbitda=Decimal("-300"))
        value = sre._inputs(structured=(structured,), facts=(), prices=())
        area_id, requirement = sre.RuleEngineArea.VALUATION, "VALUATION_INPUTS"
    result = StockRuleEngineV1().evaluate(value, allow_partial=False)
    area = sre._area(result, area_id)
    assert area.status == AreaScoreStatus.UNSCORABLE and area.raw_score is None
    assert not result.eligibility.full_analysis_allowed
    service = SimpleNamespace(readiness=None)
    failures = GlobalOpportunityOrchestrator._unscorable_failures(service, sre.INSTRUMENT_ID, [requirement], {})
    assert classify_requirement_failures(failures, [requirement]) == TECHNICAL_RETRYABLE


def _preparer():
    # Pure preparation: no repository initialization, providers or persistence.
    repo = object.__new__(ResearchRepository)
    repo._resolver = Mock()
    return repo


def test_verified_nse_binding_skips_discarded_universe_resolution():
    profile = _profile()
    source = _trusted_source(profile)
    repo = _preparer()
    assert repo._has_trusted_nse_profile_identity(profile, source, profile)
    repo._resolver.resolve.side_effect = AssertionError("unnecessary universe scan")
    document = repo._prepare_ingested_document(**_prepare_kwargs(
        body="Official issuer result with supported company disclosure text.", content_type="text/plain",
        expected_profile=profile, trusted_profile_identity=_TRUSTED_NSE_PROFILE_IDENTITY))
    assert document.instrument_id == profile.instrument_id and document.company_id == profile.company_id
    assert document.entity_resolution_confidence == .99
    repo._resolver.resolve.assert_not_called()


@pytest.mark.parametrize("field,value", [
    ("instrument_id", UUID(int=123)), ("company_id", UUID(int=456)),
    ("official_nse_profile_symbol", "DIFFERENT"), ("discovery_method", "SEARCH_DISCOVERY"),
])
def test_unverified_source_still_requires_resolution_and_relevance(field, value):
    profile = _profile()
    source = replace(_trusted_source(profile), **{field: value})
    repo = _preparer()
    assert not repo._has_trusted_nse_profile_identity(profile, source, profile)
    repo._resolver.resolve.return_value = EntityResolution(instrument_id=UUID(int=900), company_id=None, confidence=.99)
    with pytest.raises(FetchError):
        repo._prepare_ingested_document(**_prepare_kwargs(
            body="Unrelated source text that must still undergo relevance validation.", content_type="text/plain",
            expected_profile=profile, trusted_profile_identity=None))
    repo._resolver.resolve.assert_called_once()
