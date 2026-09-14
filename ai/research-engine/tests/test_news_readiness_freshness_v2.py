from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import pytest

from app.research_readiness import ResearchReadinessService, ResearchRequirementStatus, FreshnessPolicyRegistry
from app.research_readiness_runtime import RepositoryResearchReadinessAdapter
from app.valuation_evidence import materialize_valuation
from test_research_readiness_runtime import DurableRepositoryFixture, _profile, _fact
from test_stock_rule_engine import _structured, _prices
from test_news_intelligence_v2 import NOW, run, outcome


@pytest.mark.parametrize('period,status', [('2026-03-31','READY_STALE'),('2026-06-30','READY_FRESH')])
def test_tmcv_mandatory_balance_dates_not_newer_supporting_income(period,status):
    profile=_profile(); repo=DurableRepositoryFixture(profile,complete=False)
    repo.facts=[_fact(profile,'total_debt','100',period,'QUARTERLY'),
        _fact(profile,'total_equity','200',period,'QUARTERLY'),
        _fact(profile,'finance_cost','10','2026-06-30','QUARTERLY')]
    adapter=RepositoryResearchReadinessAdapter(repo)
    result=ResearchReadinessService(adapter).assess(profile.instrument_id,jurisdiction='INDIA',now=NOW)
    balance=result.for_requirement('BALANCE_SHEET_FACTS')
    assert balance.status==status
    assert balance.as_of.date().isoformat()==period
    policy=FreshnessPolicyRegistry.default().get('QUARTERLY_FINANCIALS')
    assert policy.maximum_age==timedelta(days=120)
    if status=='READY_STALE':
        assert balance.missing_reason=='FRESHNESS_POLICY_EXPIRED:DEBT,EQUITY'
    else:
        assert balance.age==NOW-datetime(2026,6,30,tzinfo=timezone.utc)


def test_release_aware_anchor_explicit_validity_not_retrieval():
    from test_research_readiness import evidence
    policy=FreshnessPolicyRegistry.default().get('QUARTERLY_FINANCIALS')
    e=replace(evidence('BALANCE_SHEET_FACTS'),as_of=datetime(2026,6,30,tzinfo=timezone.utc),
        retrieved_at=NOW,published_at=datetime(2026,8,10,tzinfo=timezone.utc))
    assert policy.evidence_time(e)==e.as_of
    assert policy.is_fresh(e,NOW)
    assert not policy.is_fresh(e,NOW+timedelta(days=60))
    assert policy.is_fresh(replace(e,valid_until=NOW+timedelta(days=61)),NOW+timedelta(days=60))


def valuation_fixture():
    record=_structured(trailingEps=Decimal('10'),bookValue=Decimal('25'))
    for key in ('trailingEps','bookValue'):
        record.snapshot.facts[key]=record.snapshot.facts[key].model_copy(update={
            'as_of_date':NOW-timedelta(days=75),'retrieved_at':NOW-timedelta(days=7)})
    price=_prices(1)[0].model_copy(update={'price':Decimal('200'),'observed_at':NOW,'retrieved_at':NOW})
    return record,price


def test_price_recomputes_ratios_using_non_daily_basis():
    record,price=valuation_fixture()
    values=materialize_valuation([record],[price],now=NOW)
    assert values['trailingPE'].value==Decimal('20')
    assert values['priceToBook'].value==Decimal('8')
    next_price=price.model_copy(update={'price':Decimal('210'),'observed_at':NOW+timedelta(days=1),'retrieved_at':NOW+timedelta(days=1)})
    assert materialize_valuation([record],[next_price],now=NOW+timedelta(days=1))['trailingPE'].value==21
    assert values['trailingPE'].as_of_date==NOW
    assert 'basisAsOf=' in values['trailingPE'].calculation_basis


@pytest.mark.parametrize('problem',['currency','future','expired_basis','conflicting_price'])
def test_valuation_rejects_incoherent_evidence(problem):
    record,price=valuation_fixture(); prices=[price]
    if problem=='currency': record.currency='USD'
    if problem=='future': price.observed_at=NOW+timedelta(days=1)
    if problem=='expired_basis':
        for k in ('trailingEps','bookValue'): record.snapshot.facts[k].as_of_date=NOW-timedelta(days=121)
    if problem=='conflicting_price': prices.append(price.model_copy(update={'provider':'NSE','price':Decimal('300')}))
    assert materialize_valuation([record],prices,now=NOW)=={}


def test_stale_price_does_not_get_current_timestamp():
    record,price=valuation_fixture()
    price.observed_at=NOW-timedelta(days=8)
    values=materialize_valuation([record],[price],now=NOW)
    assert values['trailingPE'].as_of_date==price.observed_at


@pytest.mark.parametrize('days_after,expected',[(0,'READY_FRESH'),(1,'READY_FRESH'),(5,'READY_STALE')])
def test_valuation_readiness_overnight_and_stale_price(days_after,expected):
    record,price=valuation_fixture()
    profile=_profile().model_copy(update={'instrument_id':record.instrument_id})
    repo=DurableRepositoryFixture(profile,complete=False)
    # Keep only the actual reusable basis; a provider ratio need not refresh daily.
    record.snapshot.facts={k:v for k,v in record.snapshot.facts.items() if k in {'trailingEps','bookValue'}}
    evaluation=NOW+timedelta(days=days_after)
    if days_after<=1:
        price.observed_at=evaluation; price.retrieved_at=evaluation
    repo.structured=[record]; repo.observations=[price]
    adapter=RepositoryResearchReadinessAdapter(repo); adapter._evaluation_times[profile.instrument_id]=evaluation
    result=ResearchReadinessService(adapter).assess(profile.instrument_id,jurisdiction='INDIA',now=evaluation)
    assert result.for_requirement('VALUATION_INPUTS').status==expected


@pytest.mark.parametrize('provider_state,expected,coverage',[('SUCCESS_EMPTY','READY_FRESH',100),('FAILED','FAILED',0),('PARTIAL','PARTIAL',0)])
def test_persisted_search_coverage_readiness_without_provider(provider_state,expected,coverage):
    profile=_profile(); repo=DurableRepositoryFixture(profile,complete=False)
    search=run([outcome(state=provider_state)]).model_copy(update={'instrument_id':profile.instrument_id})
    repo.news_records_for=lambda *a,**k:[search]
    adapter=RepositoryResearchReadinessAdapter(repo); adapter._evaluation_times[profile.instrument_id]=NOW
    result=ResearchReadinessService(adapter).assess(profile.instrument_id,jurisdiction='INDIA',now=NOW)
    news=result.for_requirement('CURRENT_NEWS')
    assert news.status==expected
    assert news.coverage_pct==coverage
    assert not news.mandatory
    assert repo.provider_calls==0
