"""Focused tests for honest, additive ETF evidence-readiness diagnostics.

These counts never upgrade screening opportunities into buy recommendations.
"""
from app.etf_screening import recommendation_evidence_gaps


def test_verified_facts_are_counted_without_fabricating_missing_values():
    candidates = [
        {
            'rank': 1, 'recommendation': 'OPPORTUNITY',
            'screening_metrics': {
                'expense_ratio': {'quality': 'VALID', 'value': '0.2'},
                'aum': {'quality': 'UNAVAILABLE', 'value': None},
                'tracking_error': {'quality': 'VALID', 'value': '0.1'},
                'tracking_difference': {'quality': 'VALID', 'value': '0.1'},
                'bid_ask_spread': {'quality': 'VALID', 'value': '0.05'},
            },
            'entry_assessment': {'classification': 'FAVORABLE_ENTRY_ZONE'},
        },
        {
            'rank': 2, 'recommendation': 'STRONG_OPPORTUNITY',
            'screening_metrics': {
                'expense_ratio': {'quality': 'UNAVAILABLE', 'value': None},
                'aum': {'quality': 'VALID', 'value': '1000000'},
                'bid_ask_spread': {'quality': 'VALID', 'value': None},
            },
            'entry_assessment': {'classification': 'INSUFFICIENT_EVIDENCE'},
        },
        {
            'rank': None, 'recommendation': 'INSUFFICIENT_DATA',
            'screening_metrics': {},
        },
    ]
    result = recommendation_evidence_gaps(candidates)
    assert result['screening_opportunities'] == 2
    assert result['entry_assessed'] == 1
    assert result['entry_insufficient_evidence'] == 1
    assert result['missing_verified_metrics'] == {
        'expense_ratio': 1,
        'aum': 1,
        'tracking_error': 1,
        'tracking_difference': 1,
        'bid_ask_spread': 1,
    }
    assert [c['recommendation'] for c in candidates] == [
        'OPPORTUNITY', 'STRONG_OPPORTUNITY', 'INSUFFICIENT_DATA']


def test_no_ranked_opportunities_has_zero_gaps():
    result = recommendation_evidence_gaps([
        {'rank': None, 'recommendation': 'OPPORTUNITY', 'screening_metrics': {}},
        {'rank': None, 'recommendation': 'AVOID'},
    ])
    assert result['screening_opportunities'] == 0
    assert result['entry_assessed'] == result['entry_insufficient_evidence'] == 0
    assert all(value == 0 for value in result['missing_verified_metrics'].values())
