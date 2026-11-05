"""Synthetic persisted-evidence doubles; no provider, database or cycle execution."""
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import timedelta
from decimal import Decimal
import json
from types import SimpleNamespace

import pytest

from app.etf_evidence import EtfFact, EtfListing, EtfMetric
from app.etf_readiness import evaluate_etf_readiness
from app.etf_screening import (enrich_candidate, readiness_payload, recommendation_evidence_gaps,
                               screening_evidence)
from app.etf_rule_engine import score_etf
from test_etf_rule_engine import TestScoreEtfFullEvidence as _EvidenceFixture, ID, NOW, _flat_history, provenance


def evidence_fixture():
    full = _EvidenceFixture()._full_input()
    facts = [full.market_price_fact, full.trading_volume_fact, full.bid_ask_spread_fact,
             full.tracking_error_fact, full.expense_ratio_fact,
             EtfFact(instrument_id=ID, metric=EtfMetric.AUM, value=Decimal('1000000000'), unit='INR',
                     as_of_date=NOW.date(), provenance=provenance()),
             EtfFact(instrument_id=ID, metric=EtfMetric.TRACKING_DIFFERENCE, value=Decimal('0.1'),
                     unit='PERCENT', as_of_date=NOW.date(), provenance=provenance())]
    full = replace(full, aum_fact=facts[-2], tracking_difference_fact=facts[-1])
    for fact in facts[:3]:
        fact.provenance = provenance(source_metadata={'exchange': 'NSE', 'isin': 'IN0000000009'})
    selected = {str(f.metric): f for f in facts}
    selected['NAV'] = full.nav
    store = SimpleNamespace(
        etf_facts=lambda key, metric=None: [f for f in facts if metric is None or f.metric == metric],
        etf_navs=lambda key: [full.nav], etf_holdings=lambda key: [full.holdings],
        etf_attempts=lambda key: [], load_daily_market_bars=lambda keys: _flat_history(),
        etf_listings=lambda: [EtfListing(instrument_id=ID, isin='IN0000000009', symbol='SYNTHETICETF',
            name='Synthetic fixture ETF', provenance=provenance())])
    return store, selected, full


def candidate_for(store, full):
    statuses, summary = evaluate_etf_readiness(store, ID, now=NOW)
    return {'global_instrument_id': str(ID), 'symbol': 'SYNTHETICETF', 'disposition': 'EVALUATED',
            'reason': 'EVALUATED', 'rank': 1, 'recommendation': 'STRONG_OPPORTUNITY', 'risk_gates': [],
            'research_readiness': readiness_payload(statuses, summary),
            'rule_engine_result': json.loads(json.dumps(asdict(score_etf(full)), default=str))}


def present(store, selected, full):
    candidate = candidate_for(store, full)
    candidate.update(screening_evidence(store, ID, selected, candidate, now=NOW))
    enrich_candidate(candidate, now=NOW)
    return candidate


def test_same_session_verified_inputs_have_distinct_research_and_entry_assessments():
    store, selected, full = evidence_fixture()
    candidate = present(store, selected, full)
    assert candidate['research_readiness']['summary']['ready_for_analysis'] is True
    assert candidate['entry_assessment']['classification'] == 'FAVORABLE_ENTRY_ZONE'
    assert candidate['entry_inputs']['verified'] is True
    assert candidate['evidence_completeness']['available'] == 5
    assert candidate['score_basis']['status'] == 'COMPLETE'
    assert candidate['actionable_recommendation']['is_actionable'] is True
    assert candidate['entry_assessment']['price_band'] is None


@pytest.mark.parametrize('metric,mutation', [
    ('TRADING_VOLUME', 'previous-session'), ('BID_ASK_SPREAD', 'previous-session'),
    ('NAV', 'previous-session'), ('BID_ASK_SPREAD', 'different-provider'),
    ('BID_ASK_SPREAD', 'different-exchange'), ('BID_ASK_SPREAD', 'different-security'),
    ('BID_ASK_SPREAD', 'different-currency'), ('TRADING_VOLUME', 'zero'),
    ('TRADING_VOLUME', 'demo'), ('NAV', 'future-retrieval'), ('NAV', 'future-date'),
    ('BID_ASK_SPREAD', 'unverified-host'), ('BID_ASK_SPREAD', 'unverified-authority'),
    ('NAV', 'secondary-nav'),
])
def test_incompatible_entry_inputs_never_create_an_entry_signal(metric, mutation):
    store, selected, full = evidence_fixture()
    fact = selected[metric]
    if mutation == 'previous-session':
        setattr(fact, 'nav_date' if metric == 'NAV' else 'as_of_date', NOW.date()-timedelta(days=1))
    elif mutation == 'different-provider':
        fact.provenance.provider = 'YAHOO_FINANCE'
    elif mutation == 'different-exchange':
        fact.provenance.source_metadata['exchange'] = 'BSE'
    elif mutation == 'different-security':
        fact.provenance.source_metadata['isin'] = 'IN9999999999'
    elif mutation == 'different-currency':
        fact.unit = 'USD'
    elif mutation == 'zero':
        fact.value = Decimal(0)
    elif mutation == 'demo':
        fact.provenance.source_mode = 'DEMO'
    elif mutation == 'future-retrieval':
        fact.provenance.retrieved_at = NOW + timedelta(seconds=1)
    elif mutation == 'unverified-host':
        fact.provenance.source_url = 'https://synthetic.invalid/quote'
    elif mutation == 'unverified-authority':
        fact.provenance.authority = 'SECONDARY'
    elif mutation == 'secondary-nav':
        fact.provenance.authority = 'SECONDARY'
    else:
        fact.nav_date = NOW.date()+timedelta(days=1)
    candidate = present(store, selected, full)
    assert candidate['entry_inputs']['current'] is False
    assert candidate['entry_assessment']['classification'] == 'INSUFFICIENT_EVIDENCE'
    assert candidate['actionable_recommendation']['is_actionable'] is False


def test_readiness_session_failure_is_preserved_in_missing_evidence_not_hidden():
    store, selected, full = evidence_fixture()
    selected['TRADING_VOLUME'].as_of_date -= timedelta(days=1)
    candidate = present(store, selected, full)
    assert candidate['research_readiness']['status'] == 'NOT_READY'
    mandatory = [s for s in candidate['research_readiness']['requirements'] if s['role'] == 'MANDATORY']
    assert {s['reason'] for s in mandatory if s['status'] == 'TECHNICAL_FAILURE'} == {'ETF_MARKET_PAIR_SESSION_MISMATCH'}
    assert {'MARKET_PRICE', 'TRADING_VOLUME'} <= set(candidate['missing_data'])
    assert candidate['actionable_recommendation']['is_actionable'] is False


def test_missing_fund_evidence_marks_existing_score_preliminary_without_changing_it():
    store, selected, full = evidence_fixture()
    candidate = candidate_for(store, full)
    score_before = deepcopy(candidate['rule_engine_result'])
    selected.pop('AUM')
    candidate.update(screening_evidence(store, ID, selected, candidate, now=NOW))
    enrich_candidate(candidate, now=NOW)
    assert candidate['rule_engine_result'] == score_before
    assert candidate['recommendation'] == 'STRONG_OPPORTUNITY'
    assert candidate['score_basis']['status'] == 'PRELIMINARY'
    assert candidate['evidence_completeness']['missing_metrics'] == ['aum']
    assert candidate['actionable_recommendation']['is_actionable'] is False


@pytest.mark.parametrize('disposition', ['TECHNICAL_FAILURE', 'INSUFFICIENT_DATA', 'INELIGIBLE'])
def test_unscored_candidates_do_not_claim_risks_were_evaluated(disposition):
    candidate = {'disposition': disposition, 'reason': 'SYNTHETIC_FAILURE', 'rule_engine_result': None}
    enrich_candidate(candidate, now=NOW)
    assert candidate['risk_gates'] is None
    assert candidate['risk_assessment']['status'] == 'NOT_EVALUATED'
    assert candidate['risk_assessment']['triggered_gates'] is None
    assert candidate['research_readiness']['observed'] is False
    assert candidate['research_readiness']['requirements'] is None
    assert candidate['score_basis']['available_weight_percent'] is None
    assert candidate['entry_assessment']['classification'] == 'INSUFFICIENT_EVIDENCE'


def test_assessed_entry_is_not_counted_as_favorable_when_wait_or_extended():
    candidates = [{'recommendation': 'OPPORTUNITY', 'rank': 1,
                   'entry_assessment': {'classification': value}} for value in
                  ('FAVORABLE_ENTRY_ZONE', 'NEUTRAL_WAIT', 'EXTENDED_AVOID_CHASING', 'INSUFFICIENT_EVIDENCE')]
    result = recommendation_evidence_gaps(candidates)
    assert result['entry_assessed'] == 3
    assert result['entry_favorable'] == 1
    assert result['entry_insufficient_evidence'] == 1


def test_canonical_fund_name_and_readiness_survive_deterministic_cycle_serialization():
    from app.etf_opportunity_cycle import run_etf_radar_cycle, etf_cycle_result_to_dict
    store, _, _ = evidence_fixture()
    rows = [{'globalInstrumentId':str(ID),'canonicalName':'Canonical synthetic fund name',
             'symbol':'SYNTHETICETF','assetType':'ETF','exchange':'NSE','status':'ACTIVE'}]
    result = etf_cycle_result_to_dict(run_etf_radar_cycle(rows,store,now=NOW))
    candidate = result['candidates'][0]
    assert candidate['fund_name'] == 'Canonical synthetic fund name'
    assert candidate['research_readiness']['summary']['ready_for_analysis'] is True
    assert len(candidate['research_readiness']['requirements']) > 3
    assert candidate['score_basis']['method'] == 'AVAILABLE_FACTOR_WEIGHT_NORMALIZATION'
