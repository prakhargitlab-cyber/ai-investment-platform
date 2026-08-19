from __future__ import annotations

import math
from datetime import datetime, timezone

from app.models import CatalystScore, EventImpact, ResearchEvent, ResearchEventType


DEFAULT_WEIGHTS: dict[ResearchEventType, float] = {
    ResearchEventType.NEW_ORDER: 16,
    ResearchEventType.ORDER_BACKLOG_CHANGE: 14,
    ResearchEventType.NEW_CUSTOMER: 10,
    ResearchEventType.MAJOR_CONTRACT: 14,
    ResearchEventType.GOVERNMENT_CONTRACT: 12,
    ResearchEventType.CAPEX: 8,
    ResearchEventType.FACTORY_EXPANSION: 10,
    ResearchEventType.CAPACITY_EXPANSION: 12,
    ResearchEventType.NEW_FACILITY: 10,
    ResearchEventType.GEOGRAPHIC_EXPANSION: 8,
    ResearchEventType.PARTNERSHIP: 6,
    ResearchEventType.PRODUCT_LAUNCH: 8,
    ResearchEventType.GUIDANCE_RAISED: 14,
    ResearchEventType.GUIDANCE_LOWERED: 16,
    ResearchEventType.DEBT_CHANGE: 8,
    ResearchEventType.REGULATORY_EVENT: 8,
    ResearchEventType.OTHER: 4,
}

IMPACT_MULTIPLIER = {
    EventImpact.STRONG_POSITIVE: 1.0,
    EventImpact.POSITIVE: 0.65,
    EventImpact.NEUTRAL: 0.0,
    EventImpact.UNCERTAIN: 0.15,
    EventImpact.NEGATIVE: -0.65,
    EventImpact.STRONG_NEGATIVE: -1.0,
}


class CatalystScorer:
    def __init__(self, weights: dict[ResearchEventType, float] | None = None, half_life_days: float = 365.0):
        self.weights = weights or DEFAULT_WEIGHTS
        self.half_life_days = half_life_days

    def score(self, instrument_id, events: list[ResearchEvent]) -> CatalystScore:
        buckets = {
            "New Orders": 0.0,
            "CAPEX": 0.0,
            "Capacity Expansion": 0.0,
            "New Customers": 0.0,
            "Guidance": 0.0,
        }
        total = 50.0
        confidence_sum = 0.0
        for event in events:
            contribution = self._contribution(event)
            total += contribution
            confidence_sum += event.confidence
            if event.event_type in {ResearchEventType.NEW_ORDER, ResearchEventType.MAJOR_CONTRACT, ResearchEventType.GOVERNMENT_CONTRACT, ResearchEventType.ORDER_BACKLOG_CHANGE}:
                buckets["New Orders"] += max(contribution, 0)
            elif event.event_type == ResearchEventType.CAPEX:
                buckets["CAPEX"] += max(contribution, 0)
            elif event.event_type in {ResearchEventType.CAPACITY_EXPANSION, ResearchEventType.NEW_FACILITY, ResearchEventType.FACTORY_EXPANSION}:
                buckets["Capacity Expansion"] += max(contribution, 0)
            elif event.event_type in {ResearchEventType.NEW_CUSTOMER, ResearchEventType.CUSTOMER_EXPANSION, ResearchEventType.MAJOR_CUSTOMER}:
                buckets["New Customers"] += max(contribution, 0)
            elif event.event_type in {ResearchEventType.GUIDANCE_RAISED, ResearchEventType.GUIDANCE_LOWERED, ResearchEventType.REVENUE_GUIDANCE, ResearchEventType.MARGIN_GUIDANCE}:
                buckets["Guidance"] += max(contribution, 0)
        bucket_scores = {key: int(max(0, min(100, 50 + value))) for key, value in buckets.items()}
        research_confidence = int(round((confidence_sum / len(events)) * 100)) if events else 0
        return CatalystScore(
            instrument_id=instrument_id,
            overall_score=int(max(0, min(100, round(total)))),
            buckets=bucket_scores,
            research_confidence=research_confidence,
        )

    def _contribution(self, event: ResearchEvent) -> float:
        weight = self.weights.get(event.event_type, 3.0)
        impact = IMPACT_MULTIPLIER[event.impact]
        return weight * impact * event.confidence * self._decay(event)

    def _decay(self, event: ResearchEvent) -> float:
        event_date = event.event_date or event.detected_at
        age_days = max((datetime.now(timezone.utc) - event_date).days, 0)
        return math.pow(0.5, age_days / self.half_life_days)
