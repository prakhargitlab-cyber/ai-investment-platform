"""Pure composition of Stage B and STOCK_RULE_ENGINE_V1; no acquisition or writes.

V1 weights are retained, but PRICE_TECHNICAL (7) and SECTOR_MACRO (3)
are replaced by Stage-B technical and exact-date sector evidence. V1 overall,
quality, opportunity, risk, preScore and stageBScore are NOT added again.

Score = sum(available weight * score) / available weight.
Coverage = 100 * available weight / applicable weight.
Confidence = sum(available weight * source confidence) / applicable weight:
equivalently covered-weight confidence multiplied by coverage. Each retained
V1 area uses V1 confidence; Stage-B dimensions use their own snapshot confidence.
NOT_APPLICABLE removes an area from both denominators; missing retains its
coverage obligation. Zero is evidence. PARTIAL V1 areas remain usable, with
readiness reflected by V1 confidence. Stale/conflicting optional areas are omitted.

Existing V1 full-analysis eligibility and overrides gate ranking. Additionally,
stale/conflicting valuation, quality, balance sheet, quarterly or governance
evidence, and stale/conflicting current stock price evidence suppress eligibility.
Scores remain diagnostic when suppressed; callers must filter rankEligible.
No numeric risk penalty is applied: that would count V1 resilience areas twice.

Reason thresholds: >=65 SUPPORT, <=40 WEAK. Missing/stale/conflicting dimensions
have explicit codes. Top reasons are lexically ordered, with eligibility gates
first among negatives; at most six each. All gate reasons are also exposed.
Sort uses rounded output values, missing scores last, and UUID text ascending.
"""
from __future__ import annotations

from math import isfinite
from types import MappingProxyType
from typing import TYPE_CHECKING, Iterable, Mapping
from uuid import UUID

from app.models import ResearchBaseModel

if TYPE_CHECKING:
    from app.global_scanner import StageBCandidate
    from app.stock_rule_engine import StockRuleEngineResult

RANKER_VERSION = 'GLOBAL_OPPORTUNITY_RANKER_V1'
# Frozen V1 composition, deliberately independent of future engine versions.
WEIGHTS = MappingProxyType(dict(VALUATION=18, FUNDAMENTAL_BUSINESS_QUALITY=16,
    GROWTH=14, BALANCE_SHEET=9, QUARTERLY_EARNINGS_TREND=9,
    ORDER_BOOK_CAPACITY_CATALYSTS=8, NEWS_GEOPOLITICAL_EVENTS=7,
    SHAREHOLDING=4, MANAGEMENT_GOVERNANCE=5, TECHNICAL=7, SECTOR=3))
CRITICAL_AREAS = frozenset({'VALUATION', 'FUNDAMENTAL_BUSINESS_QUALITY',
    'BALANCE_SHEET', 'QUARTERLY_EARNINGS_TREND', 'MANAGEMENT_GOVERNANCE'})


class GlobalOpportunityResult(ResearchBaseModel):
    global_instrument_id: UUID
    opportunity_score: float | None
    opportunity_confidence: float
    score_coverage: float
    rank_eligible: bool
    rule_engine_score: float | None
    technical_score: float | None
    technical_state: str
    sector_score: float | None
    sector_state: str
    risk_score: float | None
    top_positive_reasons: list[str]
    top_negative_reasons: list[str]
    eligibility_reasons: list[str]
    ranker_version: str = RANKER_VERSION


def _score(value):
    if value is None:
        return None
    value = float(value)
    if not isfinite(value) or not 0 <= value <= 100:
        raise ValueError('INVALID_RANKER_SCORE')
    return value


class GlobalOpportunityRanker:
    def score(self, candidate: StageBCandidate, rule: StockRuleEngineResult | None) -> GlobalOpportunityResult:
        key = candidate.global_instrument_id
        technical, sector = candidate.technical_feature_snapshot, candidate.sector_relative_strength_snapshot
        if technical.global_instrument_id != key or sector.global_instrument_id != key or (rule and rule.global_instrument_id != key):
            raise ValueError('RANKER_IDENTITY_MISMATCH')
        if rule and rule.rule_engine_version != 'STOCK_RULE_ENGINE_V1':
            raise ValueError('UNSUPPORTED_RULE_ENGINE_VERSION')
        if candidate.feature_version != 'GLOBAL_STAGE_B_V1':
            raise ValueError('UNSUPPORTED_STAGE_B_VERSION')
        positives, negatives, gates = set(), set(), set()
        applicable_weight = sum(WEIGHTS.values())
        values = {}
        areas = {}
        if rule:
            for area in rule.area_scores:
                if area.area in areas:
                    raise ValueError('DUPLICATE_RULE_AREA')
                areas[area.area] = area
            if rule.partial or not rule.eligibility.full_analysis_allowed or rule.overall_score is None:
                gates.add('V1_ANALYSIS_NOT_ELIGIBLE')
            for override in rule.risk_overrides:
                if override.effect == 'BLOCK_BUY' or override.severity == 'CRITICAL':
                    gates.add('V1_RISK_OVERRIDE:' + override.code)
        else:
            gates.add('V1_RESULT_MISSING')
        for name, weight in WEIGHTS.items():
            if name in {'TECHNICAL', 'SECTOR'}:
                continue
            area = areas.get(name)
            if area and (not area.applicable or area.status == 'NOT_APPLICABLE'):
                applicable_weight -= weight
                continue
            if area and area.status in {'READY_STALE', 'CONFLICTING'}:
                code = ('STALE:' if area.status == 'READY_STALE' else 'CONFLICTING:') + name
                negatives.add(code)
                if name in CRITICAL_AREAS:
                    gates.add('CRITICAL_' + code)
            elif area and area.status in {'READY_FRESH', 'PARTIAL'} and area.raw_score is not None:
                values[name] = (_score(area.raw_score), _score(rule.confidence_score))
            else:
                negatives.add('MISSING:' + name)

        if 'PRICE_HISTORY' in technical.stale_inputs or 'STOCK_HISTORY' in sector.stale_inputs:
            gates.add('CRITICAL_STALE_PRICE')
        if ('CONFLICTING' in technical.feature_states.values()
                or 'CONFLICTING_STOCK_PRICE' in sector.missing_inputs):
            gates.add('CRITICAL_CONFLICTING_PRICE')
        # Snapshots are authoritative; reject inconsistent duplicated summary fields.
        if candidate.technical_score != technical.technical_score or candidate.sector_score != sector.relative_strength_score:
            raise ValueError('STAGE_B_SCORE_MISMATCH')
        for name, value, confidence, usable in (
            ('TECHNICAL', candidate.technical_score, technical.confidence,
             not {'CRITICAL_STALE_PRICE', 'CRITICAL_CONFLICTING_PRICE'} & gates),
            ('SECTOR', candidate.sector_score, sector.confidence,
             sector.sector_state != 'INSUFFICIENT_DATA' and not {'CRITICAL_STALE_PRICE', 'CRITICAL_CONFLICTING_PRICE'} & gates)):
            if usable and value is not None:
                values[name] = (_score(value), _score(confidence))
            else:
                negatives.add('UNAVAILABLE:' + name)
        available_weight = sum(WEIGHTS[name] for name in values)
        for name, (value, _) in values.items():
            if value >= 65:
                positives.add('SUPPORT:' + name)
            elif value <= 40:
                negatives.add('WEAK:' + name)
        if not available_weight:
            gates.add('NO_SCORABLE_EVIDENCE')
        opportunity = sum(WEIGHTS[n]*v for n,(v,_) in values.items()) / available_weight if available_weight else None
        confidence = sum(WEIGHTS[n]*c for n,(_,c) in values.items()) / applicable_weight
        return GlobalOpportunityResult(global_instrument_id=key,
            opportunity_score=round(opportunity, 8) if opportunity is not None else None,
            opportunity_confidence=round(confidence, 8),
            score_coverage=round(100*available_weight/applicable_weight, 8), rank_eligible=not gates,
            rule_engine_score=_score(rule.overall_score) if rule else None,
            technical_score=_score(candidate.technical_score), technical_state=technical.technical_state,
            sector_score=_score(candidate.sector_score), sector_state=sector.sector_state,
            risk_score=_score(rule.risk_score) if rule else None,
            top_positive_reasons=sorted(positives)[:6],
            top_negative_reasons=(sorted(gates) + sorted(negatives - gates))[:6],
            eligibility_reasons=sorted(gates))

    def rank(self, candidates: Iterable[StageBCandidate], rules: Mapping[UUID, StockRuleEngineResult]) -> list[GlobalOpportunityResult]:
        results, seen = [], set()
        for candidate in candidates:
            key = candidate.global_instrument_id
            if key in seen:
                raise ValueError('DUPLICATE_RANKER_INSTRUMENT')
            seen.add(key)
            results.append(self.score(candidate, rules.get(key)))
        def descending(value):
            return -value if value is not None else float('inf')
        return sorted(results, key=lambda r: (descending(r.opportunity_score),
            -r.opportunity_confidence, -r.score_coverage, descending(r.rule_engine_score), str(r.global_instrument_id)))
