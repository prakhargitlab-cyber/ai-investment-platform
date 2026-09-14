"""Deterministic recommendation policy, independent of membership and public scoring.

V1 thresholds are deliberately explicit initial policy, not calibrated forecasts.
All prices use the persisted technical snapshot's currency and unadjusted basis.
"""
from copy import deepcopy
from hashlib import sha256
import json
from math import isfinite

VERSION = 'RECOMMENDATION_ENGINE_V1'
BUY_SCORE, STRONG_SCORE, MIN_CONFIDENCE, MIN_COVERAGE = 65, 80, 60, 60
RANGE_KEYS = ('short_entry_low short_entry_high short_target_1 short_target_2 short_invalidation '
              'long_entry_low long_entry_high long_fair_value long_target long_invalidation').split()
AREA_NAMES = {'QUALITY': 'FUNDAMENTAL_BUSINESS_QUALITY', 'QUARTERLY': 'QUARTERLY_EARNINGS_TREND',
              'CATALYST': 'ORDER_BOOK_CAPACITY_CATALYSTS', 'NEWS': 'NEWS_GEOPOLITICAL_EVENTS',
              'GOVERNANCE': 'MANAGEMENT_GOVERNANCE'}


def area_scores(rule):
    raw = {a['area']: a.get('raw_score') for a in rule.get('area_scores', [])}
    return {**raw, **{k: raw.get(v) for k, v in AREA_NAMES.items()}}


def number(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value) else None


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def ranges(snapshot):
    result = dict.fromkeys(RANGE_KEYS)
    technical = snapshot['evidence_state'].get('technical', {})
    p, support, resistance, atr = (number(v) for v in (
        snapshot.get('current_price'), technical.get('support_level'),
        technical.get('resistance_level'), technical.get('atr14')))
    if p and support and resistance and 0 < support <= p < resistance:
        result.update(short_entry_low=support, short_entry_high=min(p, support * 1.03),
                      short_target_1=resistance)
        if atr and atr > 0 and support > atr:
            result.update(short_target_2=resistance + atr, short_invalidation=support - atr)
    # Only explicitly supplied fair-value evidence is usable; never turn a valuation score into a price.
    valuation = snapshot['evidence_state'].get('valuation', {})
    fair, bull, invalidation = (number(valuation.get(k)) for k in ('fair_value', 'bull_target', 'invalidation'))
    if fair and fair > 0:
        result.update(long_entry_low=fair * .8, long_entry_high=fair * .9, long_fair_value=fair)
        if bull and bull >= fair:
            result['long_target'] = bull
        if invalidation and 0 < invalidation < fair * .8:
            result['long_invalidation'] = invalidation
    return {k: round(v, 4) if v is not None else None for k, v in result.items()}


class RecommendationEngineV1:
    def evaluate(self, snapshot, previous=None):
        evidence = snapshot['evidence_state']
        rule = evidence.get('rule', {})
        dimensions = area_scores(rule)
        critical = any(r.get('severity') == 'CRITICAL' for r in rule.get('risk_overrides', []))
        weak = [a for a in ('QUALITY', 'GROWTH', 'BALANCE_SHEET', 'QUARTERLY')
                if dimensions.get(a) is not None and dimensions[a] <= 30]
        governance = dimensions.get('GOVERNANCE')
        thesis_broken = critical or (governance is not None and governance <= 20) or len(weak) >= 2
        score = snapshot.get('opportunity_score')
        qualified = (snapshot['rank_eligible'] and score is not None and score >= BUY_SCORE
                     and snapshot['opportunity_confidence'] >= MIN_CONFIDENCE
                     and snapshot['score_coverage'] >= MIN_COVERAGE and not thesis_broken)
        technical = evidence.get('technical', {}).get('technical_state')
        short_buy = qualified and technical in {'UPTREND', 'BREAKOUT', 'PULLBACK_IN_UPTREND', 'REVERSAL_CANDIDATE'}
        long_buy = qualified and all(dimensions.get(a) is not None and dimensions[a] >= 60
                                    for a in ('QUALITY', 'VALUATION'))
        result = {k: deepcopy(snapshot.get(k)) for k in (
            'global_instrument_id', 'market', 'symbol', 'company_name', 'generated_at', 'rule_engine_version', 'ranker_version',
            'opportunity_score', 'top_positive_reasons', 'top_negative_reasons')}
        result.update(recommendation_engine_version=VERSION, price_at_recommendation=snapshot.get('current_price'),
            confidence=snapshot['opportunity_confidence'], coverage=snapshot['score_coverage'],
            new_investor_action='AVOID' if thesis_broken else ('STRONG_BUY_CANDIDATE' if score >= STRONG_SCORE else 'BUY_CANDIDATE') if qualified else 'WATCH_WAIT',
            existing_holder_action='EXIT_REVIEW' if thesis_broken else 'TOP_UP' if long_buy else 'HOLD_NO_NEW_MONEY' if not qualified else 'HOLD',
            short_term_action='EXIT' if thesis_broken else 'BUY' if short_buy else 'WAIT',
            long_term_action='EXIT_REVIEW' if thesis_broken else 'ACCUMULATE' if long_buy else 'REDUCE' if weak else 'HOLD',
            evidence_snapshot=deepcopy(evidence), **ranges(snapshot))
        reasons = result['top_negative_reasons'] or []
        if any(result[k] is None for k in RANGE_KEYS):
            reasons = [*reasons, 'PRICE_RANGE_EVIDENCE_INSUFFICIENT']
        if thesis_broken:
            reasons = [*reasons, 'LONG_TERM_THESIS_BROKEN']
        result['top_negative_reasons'] = sorted(set(reasons))
        if previous:
            comparison = lifecycle(previous, result, snapshot.get('current_price'), {'review': True})
            result['short_term_action'] = comparison['current_short_action']
            result['long_term_action'] = comparison['current_long_action']
            if comparison['current_long_action'] == 'EXIT_REVIEW':
                result['existing_holder_action'] = 'EXIT_REVIEW'
            elif comparison['current_long_action'] == 'REDUCE' or comparison['current_short_action'] == 'EXIT':
                result['existing_holder_action'] = 'REDUCE'
            elif comparison['current_short_action'] == 'PARTIAL_PROFIT':
                result['existing_holder_action'] = 'PARTIAL_PROFIT'
                if result['long_term_action'] != 'TOP_UP':
                    result['new_investor_action'] = 'WATCH_WAIT'
            result['top_negative_reasons'] = sorted(set(result['top_negative_reasons'] + comparison['lifecycle_reasons']))
        # Cache clocks, completion timestamps and observed price drift are not evidence changes.
        evidence_key = {'references': rule.get('evidence_references', []), 'dimensions': dimensions,
                        'metrics': [a.get('metrics', []) for a in rule.get('area_scores', [])],
                        'risk': rule.get('risk_overrides', []), 'technical_state': technical,
                        'sector_state': evidence.get('sector', {}).get('sector_state'),
                        'missing': evidence.get('missing_inputs', []), 'stale': evidence.get('stale_inputs', [])}
        p = result['price_at_recommendation']
        result['fingerprint'] = digest({
            'instrument': result['global_instrument_id'], 'engine': VERSION,
            'actions': [result[k] for k in ('new_investor_action', 'existing_holder_action', 'short_term_action', 'long_term_action')],
            'reference_bucket': round(p, -1) if p is not None else None,
            'ranges': {k: result[k] for k in RANGE_KEYS}, 'evidence': evidence_key,
            'score_state': [round(result[k] / 5) * 5 if result[k] is not None else None for k in ('opportunity_score', 'confidence', 'coverage')]})
        return result


def lifecycle(anchor, recommendation, price, previous_state=None):
    """Compare with the original trade levels. Never overwrite the historical anchor."""
    reasons = []
    short, long = recommendation['short_term_action'], recommendation['long_term_action']
    ss = ls = 'NEW'
    p = number(price)
    for horizon in ('short', 'long'):
        invalidation = anchor.get(f'{horizon}_invalidation')
        low, high = anchor.get(f'{horizon}_entry_low'), anchor.get(f'{horizon}_entry_high')
        target = anchor.get('short_target_1' if horizon == 'short' else 'long_target')
        state = 'HOLDING' if previous_state else 'NEW'
        if p is not None:
            if invalidation and p <= invalidation:
                state = 'INVALIDATED'
            elif invalidation and p <= invalidation * 1.03:
                state = 'INVALIDATION_APPROACHING'
            elif target and p >= target:
                state = 'TARGET_REACHED'
            elif target and p >= target * .97:
                state = 'TARGET_APPROACHING'
            elif low is not None and high is not None and low <= p <= high:
                state = 'IN_ENTRY_ZONE'
            elif high and high < p <= high * 1.03:
                state = 'ENTRY_APPROACHING'
        if horizon == 'short':
            ss = state
            if state == 'INVALIDATED':
                short = 'EXIT'
                reasons.append('SHORT_INVALIDATION_REACHED')
            elif state == 'TARGET_REACHED':
                ss, short = 'PARTIAL_PROFIT', 'PARTIAL_PROFIT'
                reasons.extend(['TARGET_1_REACHED', 'SHORT_TERM_RISK_REWARD_COMPRESSED'])
                if anchor.get('short_target_2') and p >= anchor['short_target_2'] * .97:
                    reasons.append('TARGET_2_REACHED' if p >= anchor['short_target_2'] else 'TARGET_2_APPROACHING')
            elif (previous_state and short == 'WAIT'
                  and anchor.get('short_term_action') in {'BUY', 'HOLD', 'PARTIAL_PROFIT'}):
                short = 'HOLD'
        else:
            ls = state
            if state == 'INVALIDATED':
                long, ls = 'EXIT_REVIEW', 'INVALIDATED'
            elif state in {'TARGET_REACHED', 'TARGET_APPROACHING'} and long not in {'REDUCE', 'EXIT_REVIEW'}:
                long = 'HOLD'
            elif (previous_state and long == 'ACCUMULATE' and p is not None
                  and anchor.get('price_at_recommendation') and p <= anchor['price_at_recommendation'] * .97):
                long = 'TOP_UP'
    if recommendation['long_term_action'] == 'EXIT_REVIEW':
        long, ls = 'EXIT_REVIEW', 'EXIT_REVIEW'
        reasons.append('LONG_TERM_THESIS_BROKEN')
    elif long == 'REDUCE':
        ls = 'THESIS_WEAKENING'
    if short == 'PARTIAL_PROFIT' and long in {'TOP_UP', 'ACCUMULATE'}:
        long = 'HOLD'
    if recommendation['short_term_action'] == 'EXIT':
        short, ss = 'EXIT', 'EXIT_REVIEW'
    def distance(key):
        return round((anchor[key] / p - 1) * 100, 4) if p and anchor.get(key) else None
    return dict(short_term_state=ss, long_term_state=ls, current_short_action=short,
        current_long_action=long, lifecycle_status=ls if ls in {'EXIT_REVIEW', 'INVALIDATED', 'THESIS_WEAKENING'} else ss,
        price_at_recommendation=anchor.get('price_at_recommendation'), current_price=p,
        short_target_distance_pct=distance('short_target_2'), long_target_distance_pct=distance('long_target'),
        lifecycle_reasons=reasons)
