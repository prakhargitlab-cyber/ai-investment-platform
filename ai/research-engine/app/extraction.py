from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal

from app.models import (
    EventImpact,
    ReliabilityLevel,
    ResearchDocument,
    ResearchEvent,
    ResearchEventType,
    SourceType,
    TimeHorizon,
)
from app.normalization import normalize_numbers


class ResearchEventExtractor:
    def extract(self, document: ResearchDocument) -> list[ResearchEvent]:
        raise NotImplementedError


class RuleBasedEventExtractor(ResearchEventExtractor):
    def extract(self, document: ResearchDocument) -> list[ResearchEvent]:
        if not document.instrument_id or not document.company_id or not document.normalized_text:
            return []
        text = document.normalized_text
        lower = text.lower()
        events: list[ResearchEvent] = []
        if any(term in lower for term in ["new order", "order worth", "contract worth", "framework agreement", "purchase order"]):
            events.append(self._event(document, ResearchEventType.NEW_ORDER, "Order or contract announcement", EventImpact.POSITIVE, TimeHorizon.MEDIUM_TERM))
        if "backlog" in lower:
            impact = EventImpact.POSITIVE if any(term in lower for term in ["increase", "grew", "growth", "+"]) else EventImpact.NEUTRAL
            events.append(self._event(document, ResearchEventType.ORDER_BACKLOG_CHANGE, "Order backlog update", impact, TimeHorizon.SHORT_TERM))
        if any(term in lower for term in ["capex", "capital expenditure", "investment of", "invests", "will invest"]):
            events.append(self._event(document, ResearchEventType.CAPEX, "CAPEX or investment announcement", EventImpact.UNCERTAIN, TimeHorizon.LONG_TERM))
        if any(term in lower for term in ["capacity expansion", "expand capacity", "production capacity", "mw"]):
            events.append(self._event(document, ResearchEventType.CAPACITY_EXPANSION, "Capacity expansion signal", EventImpact.POSITIVE, TimeHorizon.LONG_TERM))
        if any(term in lower for term in ["new facility", "new factory", "factory expansion", "new plant"]):
            events.append(self._event(document, ResearchEventType.NEW_FACILITY, "Facility expansion signal", EventImpact.POSITIVE, TimeHorizon.LONG_TERM))
        if any(term in lower for term in ["new customer", "customer win", "selected by"]):
            events.append(self._event(document, ResearchEventType.NEW_CUSTOMER, "Customer win signal", EventImpact.POSITIVE, TimeHorizon.MEDIUM_TERM))
        if any(term in lower for term in ["guidance raised", "raises guidance", "increased guidance"]):
            events.append(self._event(document, ResearchEventType.GUIDANCE_RAISED, "Guidance raised", EventImpact.POSITIVE, TimeHorizon.SHORT_TERM))
        if any(term in lower for term in ["guidance lowered", "cuts guidance", "lowered guidance", "withdraws guidance"]):
            events.append(self._event(document, ResearchEventType.GUIDANCE_LOWERED, "Guidance lowered", EventImpact.NEGATIVE, TimeHorizon.SHORT_TERM))
        if any(term in lower for term in ["cancelled", "canceled", "delay", "delayed", "customer loss"]):
            events.append(self._event(document, ResearchEventType.OTHER, "Negative execution signal", EventImpact.NEGATIVE, TimeHorizon.SHORT_TERM))
        if "annual report" in lower:
            events.append(self._event(document, ResearchEventType.ANNUAL_REPORT, "Annual report published", EventImpact.NEUTRAL, TimeHorizon.UNKNOWN))
        if "earnings" in lower or "quarterly results" in lower:
            events.append(self._event(document, ResearchEventType.EARNINGS_RELEASE, "Earnings release", EventImpact.NEUTRAL, TimeHorizon.IMMEDIATE))
        return _deduplicate_events(events)

    def _event(
        self,
        document: ResearchDocument,
        event_type: ResearchEventType,
        title: str,
        impact: EventImpact,
        horizon: TimeHorizon,
    ) -> ResearchEvent:
        assert document.instrument_id is not None
        assert document.company_id is not None
        text = document.normalized_text or ""
        numbers = normalize_numbers(text)
        money = next((n for n in numbers if n.currency), None)
        pct = next((n for n in numbers if n.unit == "PERCENT"), None)
        capacity = next((n for n in numbers if n.unit in {"MW", "GW", "UNITS"}), None)
        customer = _extract_customer(text)
        confidence = _confidence(document.reliability_level, document.entity_resolution_confidence, bool(numbers), document.published_at is not None)
        return ResearchEvent(
            instrument_id=document.instrument_id,
            company_id=document.company_id,
            event_type=event_type,
            event_date=document.published_at,
            detected_at=datetime.now(timezone.utc),
            title=title,
            summary=_summary(text),
            source_document_id=document.document_id,
            source_url=document.canonical_url,
            source_type=document.source_type,
            reliability=document.reliability_level,
            confidence=confidence,
            impact=impact,
            time_horizon=horizon,
            currency=money.currency if money else None,
            monetary_value=money.value if money else None,
            monetary_original=money.original if money else None,
            percentage_value=pct.value if pct else None,
            percentage_original=pct.original if pct else None,
            customer=customer,
            counterparty=customer,
            capacity_value=capacity.value if capacity else None,
            capacity_unit=capacity.unit if capacity else None,
            raw_evidence_reference=_evidence(text, event_type),
        )


class LLMEventExtractor(ResearchEventExtractor):
    def extract(self, document: ResearchDocument) -> list[ResearchEvent]:
        return []


def _confidence(reliability: ReliabilityLevel, entity_confidence: float, has_number: bool, has_date: bool) -> float:
    reliability_score = {
        ReliabilityLevel.LEVEL_A: 0.95,
        ReliabilityLevel.LEVEL_B: 0.85,
        ReliabilityLevel.LEVEL_C: 0.70,
        ReliabilityLevel.LEVEL_D: 0.55,
        ReliabilityLevel.LEVEL_E: 0.35,
    }[reliability]
    score = reliability_score * 0.45 + entity_confidence * 0.35 + (0.10 if has_number else 0.0) + (0.10 if has_date else 0.0)
    return float(min(1.0, round(score, 4)))


def _summary(text: str) -> str:
    return text[:240].strip()


def _evidence(text: str, event_type: ResearchEventType) -> str:
    sentences = re.split(r"(?<=[.!?])\s+", text)
    keywords = event_type.value.lower().split("_")
    for sentence in sentences:
        if any(keyword in sentence.lower() for keyword in keywords):
            return sentence[:500]
    return text[:500]


def _extract_customer(text: str) -> str | None:
    match = re.search(r"(?:with|from|by|for)\s+([A-Z][A-Za-z0-9&.\- ]{2,60})(?:\.|,| for | to | worth | valued)", text)
    return match.group(1).strip() if match else None


def _deduplicate_events(events: list[ResearchEvent]) -> list[ResearchEvent]:
    seen: set[tuple[ResearchEventType, Decimal | None, str | None]] = set()
    unique: list[ResearchEvent] = []
    for event in events:
        key = (event.event_type, event.monetary_value, event.customer)
        if key not in seen:
            seen.add(key)
            unique.append(event)
    return unique
