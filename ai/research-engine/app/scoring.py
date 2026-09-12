from __future__ import annotations

import math
from datetime import datetime, timezone

from app.models import CatalystScore, CategoryEvidence, EventImpact, EvidenceState, ResearchEvent, ResearchEventType


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
    ResearchEventType.GUIDANCE_CUT: 16,
    ResearchEventType.GUIDANCE_MAINTAINED: 4,
    ResearchEventType.ORDER_CANCELLED: 16,
    ResearchEventType.PROJECT_DELAY: 10,
    ResearchEventType.CUSTOMER_LOSS: 12,
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

CATEGORY_EVENT_TYPES: dict[str, set[ResearchEventType]] = {
    "Growth": {
        ResearchEventType.GEOGRAPHIC_EXPANSION,
        ResearchEventType.PARTNERSHIP,
        ResearchEventType.PRODUCT_LAUNCH,
    },
    "Orders & Backlog": {
        ResearchEventType.NEW_ORDER,
        ResearchEventType.ORDER_BACKLOG_CHANGE,
        ResearchEventType.MAJOR_CONTRACT,
        ResearchEventType.GOVERNMENT_CONTRACT,
        ResearchEventType.ORDER_CANCELLED,
    },
    "CAPEX & Capacity": {
        ResearchEventType.CAPEX,
        ResearchEventType.CAPACITY_EXPANSION,
        ResearchEventType.NEW_FACILITY,
        ResearchEventType.FACTORY_EXPANSION,
        ResearchEventType.PROJECT_DELAY,
    },
    "Customers": {
        ResearchEventType.NEW_CUSTOMER,
        ResearchEventType.CUSTOMER_EXPANSION,
        ResearchEventType.MAJOR_CUSTOMER,
        ResearchEventType.CUSTOMER_LOSS,
    },
    "Guidance": {
        ResearchEventType.GUIDANCE_RAISED,
        ResearchEventType.GUIDANCE_LOWERED,
        ResearchEventType.GUIDANCE_CUT,
        ResearchEventType.GUIDANCE_MAINTAINED,
        ResearchEventType.REVENUE_GUIDANCE,
        ResearchEventType.MARGIN_GUIDANCE,
    },
    "FINANCIAL_RESULTS": {
        ResearchEventType.EARNINGS_RELEASE,
        ResearchEventType.ANNUAL_REPORT,
    },
    "GUIDANCE": {
        ResearchEventType.GUIDANCE_RAISED,
        ResearchEventType.GUIDANCE_LOWERED,
        ResearchEventType.GUIDANCE_CUT,
        ResearchEventType.GUIDANCE_MAINTAINED,
        ResearchEventType.REVENUE_GUIDANCE,
        ResearchEventType.MARGIN_GUIDANCE,
    },
    "ORDERS_BACKLOG": {
        ResearchEventType.NEW_ORDER,
        ResearchEventType.ORDER_BACKLOG_CHANGE,
        ResearchEventType.MAJOR_CONTRACT,
        ResearchEventType.GOVERNMENT_CONTRACT,
        ResearchEventType.ORDER_CANCELLED,
    },
    "CONTRACTS": {
        ResearchEventType.NEW_ORDER,
        ResearchEventType.MAJOR_CONTRACT,
        ResearchEventType.GOVERNMENT_CONTRACT,
    },
    "CAPEX": {
        ResearchEventType.CAPEX,
        ResearchEventType.CAPACITY_EXPANSION,
        ResearchEventType.NEW_FACILITY,
        ResearchEventType.FACTORY_EXPANSION,
        ResearchEventType.PROJECT_DELAY,
    },
    "NEW_FACILITIES": {
        ResearchEventType.NEW_FACILITY,
        ResearchEventType.FACTORY_EXPANSION,
        ResearchEventType.CAPACITY_EXPANSION,
    },
    "ACQUISITIONS": {
        ResearchEventType.ACQUISITION,
    },
    "CLIENTS": {
        ResearchEventType.NEW_CUSTOMER,
        ResearchEventType.CUSTOMER_EXPANSION,
        ResearchEventType.MAJOR_CUSTOMER,
        ResearchEventType.CUSTOMER_LOSS,
    },
    "PRODUCTS": {
        ResearchEventType.PRODUCT_LAUNCH,
    },
    "MANAGEMENT": {
        ResearchEventType.MANAGEMENT_CHANGE,
    },
    "ANALYST_OPINION": set(),
    "ANALYST_TARGETS": set(),
    "INSTITUTIONAL_ACTIVITY": set(),
    "VALUATION": set(),
    "RISKS": {
        ResearchEventType.PROJECT_DELAY,
        ResearchEventType.ORDER_CANCELLED,
        ResearchEventType.CUSTOMER_LOSS,
        ResearchEventType.DEBT_CHANGE,
        ResearchEventType.REGULATORY_EVENT,
    },
    "CATALYSTS": {
        ResearchEventType.NEW_ORDER,
        ResearchEventType.ORDER_BACKLOG_CHANGE,
        ResearchEventType.MAJOR_CONTRACT,
        ResearchEventType.CAPEX,
        ResearchEventType.NEW_FACILITY,
        ResearchEventType.ACQUISITION,
        ResearchEventType.PRODUCT_LAUNCH,
        ResearchEventType.GUIDANCE_RAISED,
        ResearchEventType.GUIDANCE_CUT,
    },
}

LEGACY_BUCKETS = {
    "New Orders": "Orders & Backlog",
    "CAPEX": "CAPEX & Capacity",
    "Capacity Expansion": "CAPEX & Capacity",
    "New Customers": "Customers",
    "Guidance": "Guidance",
}


# These are read-model identities, not refresh scheduling keys.  The scorer
# retains its existing detailed categories for refresh logic; callers that
# render coverage collapse aliases into this one deterministic vocabulary.
CATEGORY_READ_MODEL_ALIASES = {
    "Growth": "GROWTH",
    "GROWTH": "GROWTH",
    "PRODUCTS": "GROWTH",
    "Orders & Backlog": "ORDERS_BACKLOG",
    "ORDERS_BACKLOG": "ORDERS_BACKLOG",
    "CONTRACTS": "ORDERS_BACKLOG",
    "CAPEX & Capacity": "CAPEX",
    "CAPEX": "CAPEX",
    "NEW_FACILITIES": "CAPEX",
    "Customers": "CLIENTS",
    "CLIENTS": "CLIENTS",
    "Guidance": "GUIDANCE",
    "GUIDANCE": "GUIDANCE",
}


def canonical_read_model_score(score: CatalystScore) -> CatalystScore:
    """Collapse scoring aliases and retain the actual events behind a score."""
    groups: dict[str, list[CategoryEvidence]] = {}
    for category, evidence in score.category_evidence.items():
        groups.setdefault(CATEGORY_READ_MODEL_ALIASES.get(category, category), []).append(evidence)
    evidence_by_category: dict[str, CategoryEvidence] = {}
    for category, entries in groups.items():
        events_by_id = {}
        for entry in entries:
            for event in entry.supporting_events:
                events_by_id.setdefault(event.event_id, event)
        events = list(events_by_id.values())
        selected = next((entry for entry in entries if entry.score is not None), entries[0])
        evidence_by_category[category] = _read_model_category_evidence(category, events, selected)
    return score.model_copy(update={
        "buckets": {category: evidence.score for category, evidence in evidence_by_category.items()},
        "category_evidence": evidence_by_category,
    })


def _read_model_category_evidence(
    category: str,
    events: list[ResearchEvent],
    selected: CategoryEvidence,
) -> CategoryEvidence:
    if not events:
        return CategoryEvidence(category=category, status=EvidenceState.NO_EVIDENCE, score=None)
    has_positive = any(event.impact in {EventImpact.POSITIVE, EventImpact.STRONG_POSITIVE} for event in events)
    has_negative = any(event.impact in {EventImpact.NEGATIVE, EventImpact.STRONG_NEGATIVE} for event in events)
    status = EvidenceState.MIXED_EVIDENCE if has_positive and has_negative else (
        EvidenceState.NEGATIVE_EVIDENCE if has_negative else (
            EvidenceState.POSITIVE_EVIDENCE if has_positive else EvidenceState.NEUTRAL_EVIDENCE
        )
    )
    source_identities = {
        event.independence_key or str(event.source_document_id) or str(event.event_id)
        for event in events
    }
    return CategoryEvidence(
        category=category,
        status=status,
        score=selected.score,
        event_count=len(events),
        source_count=len(source_identities),
        independent_source_count=len(source_identities),
        has_conflict=has_positive and has_negative,
        supporting_events=events,
    )


class CatalystScorer:
    def __init__(self, weights: dict[ResearchEventType, float] | None = None, half_life_days: float = 365.0):
        self.weights = weights or DEFAULT_WEIGHTS
        self.half_life_days = half_life_days

    def score(self, instrument_id, events: list[ResearchEvent]) -> CatalystScore:
        # Overall score is derived from validated evidence events only. There is
        # deliberately NO 50.0 neutral baseline: an instrument with no eligible
        # evidence events scores 0, per CatalystScore.aggregation_rule, which
        # states NO_EVIDENCE categories are omitted and are not treated as 50.
        total = 0.0
        confidence_sum = 0.0
        category_contributions = {category: 0.0 for category in CATEGORY_EVENT_TYPES}
        category_events: dict[str, list[ResearchEvent]] = {category: [] for category in CATEGORY_EVENT_TYPES}
        for event in events:
            contribution = self._contribution(event)
            total += contribution
            confidence_sum += event.confidence
            for category, event_types in CATEGORY_EVENT_TYPES.items():
                if event.event_type in event_types:
                    category_contributions[category] += contribution
                    category_events[category].append(event)
                    break
        category_evidence = {
            category: self._category_evidence(category, category_events[category], category_contributions[category])
            for category in CATEGORY_EVENT_TYPES
        }
        bucket_scores: dict[str, int | None] = {
            category: evidence.score for category, evidence in category_evidence.items()
        }
        for legacy_name, category in LEGACY_BUCKETS.items():
            bucket_scores[legacy_name] = category_evidence[category].score
        research_confidence = int(round((confidence_sum / len(events)) * 100)) if events else 0
        return CatalystScore(
            instrument_id=instrument_id,
            overall_score=int(max(0, min(100, round(total)))),
            buckets=bucket_scores,
            category_evidence=category_evidence,
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

    def _category_evidence(self, category: str, events: list[ResearchEvent], contribution: float) -> CategoryEvidence:
        if not events:
            return CategoryEvidence(category=category, status=EvidenceState.NO_EVIDENCE, score=None)
        score = int(max(0, min(100, 50 + contribution)))
        has_positive = any(event.impact in {EventImpact.POSITIVE, EventImpact.STRONG_POSITIVE} for event in events)
        has_negative = any(event.impact in {EventImpact.NEGATIVE, EventImpact.STRONG_NEGATIVE} for event in events)
        status = EvidenceState.NEUTRAL_EVIDENCE
        if has_positive and has_negative:
            status = EvidenceState.MIXED_EVIDENCE
        elif has_negative:
            status = EvidenceState.NEGATIVE_EVIDENCE
        elif has_positive:
            status = EvidenceState.POSITIVE_EVIDENCE
        independent_keys = {
            event.independence_key or str(event.source_document_id)
            for event in events
            if event.independence_key or event.source_document_id
        }
        return CategoryEvidence(
            category=category,
            status=status,
            score=score,
            event_count=len(events),
            source_count=len({event.source_document_id for event in events}),
            independent_source_count=len(independent_keys),
            has_conflict=has_positive and has_negative,
            supporting_events=events,
        )
