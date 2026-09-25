from dataclasses import replace
import pytest
from app.research_readiness import ResearchRefreshPlanner, ResearchRequirementStatus, ResearchRequirementRegistry
from app.stock_rule_engine import StockRuleEngineEligibilityPolicy, StockRuleEngineV1
from app.global_opportunity_ranker import GlobalOpportunityRanker
from test_stock_rule_engine import _readiness, _inputs
from test_global_opportunity_ranker import inputs


@pytest.mark.parametrize('status', [ResearchRequirementStatus.MISSING, ResearchRequirementStatus.FAILED, ResearchRequirementStatus.PARTIAL])
def test_current_news_gap_is_never_fabricated_as_a_score(status):
    # Full-research contract: a current-news gap is never simulated as a metric.
    # Whether the check was never run (MISSING) or failed at the technical /
    # provider layer (FAILED / PARTIAL), the NEWS rule-engine area is
    # UNSCORABLE (raw_score None, no fabricated event/metric).
    readiness = _readiness({'CURRENT_NEWS': status})
    news = StockRuleEngineV1()._news(replace(_inputs(events=[]), readiness=readiness))
    assert news.raw_score is None and news.metrics == []


def test_current_news_never_blocks_by_itself_in_any_state():
    # Updated contract: CURRENT_NEWS is optional contextual research. Its
    # acquisition/readiness state -- including never-attempted (MISSING), a
    # technical/provider failure (FAILED / PARTIAL), or any other truthful
    # state -- must never by itself suppress full_analysis_allowed, block the
    # Rule Engine, or suppress rank_eligible.
    for status in (
        ResearchRequirementStatus.MISSING,
        ResearchRequirementStatus.FAILED,
        ResearchRequirementStatus.PARTIAL,
    ):
        readiness = _readiness({'CURRENT_NEWS': status})
        eligibility = StockRuleEngineEligibilityPolicy().evaluate(readiness)
        assert eligibility.full_analysis_allowed
        assert eligibility.blocking_requirements == []
        # Default fixture events/facts (not overridden to an empty events
        # tuple), so only CURRENT_NEWS varies here: an empty events tuple
        # separately makes the unrelated ORDER_BOOK_CAPACITY_CATALYSTS area
        # UNSCORABLE, which still blocks and is not what this proves.
        result = StockRuleEngineV1().evaluate(replace(_inputs(), readiness=readiness), allow_partial=False)
        assert result.eligibility.full_analysis_allowed and not result.partial
        assert result.overall_score is not None
        assert 'CURRENT_NEWS' not in result.eligibility.blocking_requirements
        candidate, rule = inputs()
        scored = GlobalOpportunityRanker().score(candidate, result.model_copy(update={
            'global_instrument_id': rule.global_instrument_id}))
        assert scored.rank_eligible
        assert 'CURRENT_NEWS' not in scored.eligibility_reasons


def test_mandatory_news_is_planned_automatically_and_explicit_request_works():
    readiness = _readiness({'CURRENT_NEWS': ResearchRequirementStatus.MISSING, 'LATEST_PRICE': ResearchRequirementStatus.MISSING})
    planner = ResearchRefreshPlanner()
    automatic = planner.plan(readiness, jurisdiction='INDIA', requirement_ids=None, include_non_mandatory=True)
    assert 'CURRENT_NEWS' in {t.requirement_id for t in automatic.targets}
    assert 'LATEST_PRICE' in {t.requirement_id for t in automatic.targets}
    explicit = planner.plan(readiness, jurisdiction='INDIA', requirement_ids=['CURRENT_NEWS'], include_non_mandatory=True)
    assert [t.requirement_id for t in explicit.targets] == ['CURRENT_NEWS']
    assert not StockRuleEngineEligibilityPolicy().evaluate(readiness).full_analysis_allowed
