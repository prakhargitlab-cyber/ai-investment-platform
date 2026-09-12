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
            events.append(self._event(document, ResearchEventType.NEW_ORDER, "Order or contract announcement", EventImpact.POSITIVE, TimeHorizon.MEDIUM_TERM, ["new order", "order worth", "contract worth", "framework agreement", "purchase order"]))
        if any(term in lower for term in ["backlog", "order intake", "order book"]):
            evidence = _evidence(text, ["backlog", "order intake", "order book"])
            impact = EventImpact.POSITIVE if any(term in evidence.lower() for term in ["increase", "grew", "growth", "+", "up "]) else EventImpact.NEUTRAL
            events.append(self._event(document, ResearchEventType.ORDER_BACKLOG_CHANGE, "Order backlog update", impact, TimeHorizon.SHORT_TERM, ["backlog", "order intake", "order book"]))
        if any(term in lower for term in ["capex", "capital expenditure", "investment of", "invests", "will invest"]):
            events.append(self._event(document, ResearchEventType.CAPEX, "CAPEX or investment announcement", EventImpact.UNCERTAIN, TimeHorizon.LONG_TERM, ["capex", "capital expenditure", "investment of", "invests", "will invest"]))
        if any(term in lower for term in ["capacity expansion", "expand capacity", "production capacity", "mw"]):
            events.append(self._event(document, ResearchEventType.CAPACITY_EXPANSION, "Capacity expansion signal", EventImpact.POSITIVE, TimeHorizon.LONG_TERM, ["capacity expansion", "expand capacity", "production capacity", "volume ramp", "ramping up production"]))
        if any(term in lower for term in ["new facility", "new factory", "factory expansion", "new plant"]):
            events.append(self._event(document, ResearchEventType.NEW_FACILITY, "Facility expansion signal", EventImpact.POSITIVE, TimeHorizon.LONG_TERM, ["new facility", "new factory", "factory expansion", "new plant"]))
        if any(term in lower for term in ["geographic expansion", "market expansion", "revenue growth", "demand growth", "product growth", "new product ramp"]):
            events.append(self._event(document, ResearchEventType.GEOGRAPHIC_EXPANSION, "Growth expansion signal", EventImpact.POSITIVE, TimeHorizon.MEDIUM_TERM, ["geographic expansion", "market expansion", "revenue growth", "demand growth", "product growth", "new product ramp"]))
        if any(term in lower for term in ["new customer", "customer win", "selected by"]):
            events.append(self._event(document, ResearchEventType.NEW_CUSTOMER, "Customer win signal", EventImpact.POSITIVE, TimeHorizon.MEDIUM_TERM, ["new customer", "customer win", "selected by"]))
        if any(term in lower for term in ["guidance raised", "raises guidance", "increased guidance"]):
            events.append(self._event(document, ResearchEventType.GUIDANCE_RAISED, "Guidance raised", EventImpact.POSITIVE, TimeHorizon.SHORT_TERM, ["guidance raised", "raises guidance", "increased guidance", "raised full-year"]))
        if any(term in lower for term in ["guidance confirmed", "confirms guidance", "maintained revenue guidance", "guidance maintained", "guidance for the full year"]):
            events.append(self._event(document, ResearchEventType.GUIDANCE_MAINTAINED, "Guidance maintained", EventImpact.NEUTRAL, TimeHorizon.SHORT_TERM, ["guidance confirmed", "confirms guidance", "maintained revenue guidance", "guidance maintained", "guidance for the full year"]))
        if any(term in lower for term in ["guidance lowered", "cuts guidance", "lowered guidance", "withdraws guidance", "guidance cut"]):
            events.append(self._event(document, ResearchEventType.GUIDANCE_CUT, "Guidance cut", EventImpact.NEGATIVE, TimeHorizon.SHORT_TERM, ["guidance lowered", "cuts guidance", "lowered guidance", "withdraws guidance", "guidance cut"]))
        if any(term in lower for term in ["cancelled order", "canceled order", "order cancelled", "order canceled", "contract cancelled", "contract canceled"]):
            events.append(self._event(document, ResearchEventType.ORDER_CANCELLED, "Order cancellation", EventImpact.NEGATIVE, TimeHorizon.SHORT_TERM, ["cancelled order", "canceled order", "order cancelled", "order canceled", "contract cancelled", "contract canceled"]))
        if any(term in lower for term in ["project delay", "project delayed", "factory ramp was delayed", "ramp was delayed", "delayed by"]):
            events.append(self._event(document, ResearchEventType.PROJECT_DELAY, "Project delay", EventImpact.NEGATIVE, TimeHorizon.SHORT_TERM, ["project delay", "project delayed", "factory ramp was delayed", "ramp was delayed", "delayed by"]))
        if "customer loss" in lower or "lost customer" in lower:
            events.append(self._event(document, ResearchEventType.CUSTOMER_LOSS, "Customer loss", EventImpact.NEGATIVE, TimeHorizon.SHORT_TERM, ["customer loss", "lost customer"]))
        if _is_annual_report(document, text):
            events.append(self._event(document, ResearchEventType.ANNUAL_REPORT, "Annual report published", EventImpact.NEUTRAL, TimeHorizon.UNKNOWN, ["annual report"]))
        if any(term in lower for term in ["earnings", "quarterly results", "half year results", "half-year results", "h1 results", "q2 results"]):
            events.append(self._event(document, ResearchEventType.EARNINGS_RELEASE, "Financial results release", EventImpact.NEUTRAL, TimeHorizon.IMMEDIATE, ["earnings", "quarterly results", "half year results", "half-year results", "h1 results", "q2 results"]))
        return _deduplicate_events(events)

    def _event(
        self,
        document: ResearchDocument,
        event_type: ResearchEventType,
        title: str,
        impact: EventImpact,
        horizon: TimeHorizon,
        evidence_terms: list[str],
    ) -> ResearchEvent:
        assert document.instrument_id is not None
        assert document.company_id is not None
        text = document.normalized_text or ""
        evidence = _evidence(text, evidence_terms)
        numbers = normalize_numbers(evidence)
        money = _relevant_money(numbers, event_type, evidence)
        pct = _relevant_percent(numbers, event_type, evidence)
        capacity = _relevant_capacity(numbers, event_type, evidence)
        customer = _extract_customer(evidence)
        counterparty = customer if _event_has_counterparty(event_type) else None
        confidence = _confidence(document.reliability_level, document.entity_resolution_confidence, bool(numbers), document.published_at is not None)
        return ResearchEvent(
            instrument_id=document.instrument_id,
            company_id=document.company_id,
            event_type=event_type,
            event_date=document.published_at,
            detected_at=datetime.now(timezone.utc),
            title=title,
            summary=_summary(evidence),
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
            counterparty=counterparty,
            capacity_value=capacity.value if capacity else None,
            capacity_unit=capacity.unit if capacity else None,
            raw_evidence_reference=evidence,
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


def _evidence(text: str, terms: list[str]) -> str:
    sentences = re.split(r"(?<=[.!?])\s+", text)
    matches: list[tuple[int, str]] = []
    for index, sentence in enumerate(sentences):
        lower = sentence.lower()
        if _is_boilerplate_sentence(sentence):
            continue
        if any(term in lower for term in terms):
            matches.append((index, sentence))
    if not matches:
        return ""
    if any("guidance" in term for term in terms):
        pure_guidance = [
            (index, sentence)
            for index, sentence in matches
            if "guidance" in sentence.lower()
            and not any(term in sentence.lower() for term in ["order intake", "backlog", "order book", "new order"])
        ]
        if pure_guidance:
            return pure_guidance[0][1][:500]
    if any(term in {"earnings", "quarterly results", "half year results", "half-year results", "h1 results", "q2 results"} for term in terms):
        financial_result = [
            (index, sentence)
            for index, sentence in matches
            if not any(term in sentence.lower() for term in ["order intake", "backlog", "order book", "new order"])
        ]
        if financial_result:
            return financial_result[0][1][:500]
        result_heading = [
            (index, sentence)
            for index, sentence in matches
            if any(term in sentence.lower() for term in ["q2 results", "half year results", "half-year results", "h1 results"])
        ]
        if result_heading:
            return _result_clause(result_heading[0][1])[:500]
    if any(term in {"capacity expansion", "expand capacity", "production capacity", "volume ramp", "ramping up production"} for term in terms):
        concrete_capacity = [
            (index, sentence)
            for index, sentence in matches
            if any(term in sentence.lower() for term in ["production capacity", "capacity expansion", "expand capacity", "ramping up production"])
            and not any(term in sentence.lower() for term in ["order intake", "backlog", "order book", "new order"])
        ]
        if concrete_capacity:
            return concrete_capacity[0][1][:500]
    for index, sentence in matches:
        if any(term in {"order intake", "backlog"} for term in terms):
            prior_heading = sentences[index - 1] if index > 0 and len(sentences[index - 1]) < 180 else ""
            combined = f"{prior_heading} {sentence}".strip()
            return combined[:500]
        return sentence[:500]
    return matches[0][1][:500]


def _has_article_phrase(text: str, phrases: list[str]) -> bool:
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        lower = sentence.lower()
        if any(phrase in lower for phrase in phrases):
            return True
    return False


def _is_annual_report(document: ResearchDocument, text: str) -> bool:
    title = (document.title or "").lower()
    url = (document.canonical_url or "").lower()
    if "annual report" in title or "annual-report" in url or "annual_reports" in url:
        return True
    article_sentences = [sentence for sentence in re.split(r"(?<=[.!?])\s+", text) if not _is_boilerplate_sentence(sentence)]
    for sentence in article_sentences[:5]:
        lower = sentence.lower()
        if "annual report" in lower and any(term in lower for term in ["published", "released", "available", "report for fiscal"]):
            return True
    return False


def _is_boilerplate_sentence(sentence: str) -> bool:
    lower = sentence.lower()
    boilerplate_markers = [
        "the issuer is solely responsible",
        "key word(s):",
        "cet/cest",
        "eqs news",
        "dissemination of",
        "end of inside information",
        "contact investor relations",
        "forward-looking statements",
        "this could result from a variety of factors",
        "actual results may differ",
        "juli 2026 | finance news",
    ]
    return any(marker in lower for marker in boilerplate_markers)


def _result_clause(sentence: str) -> str:
    parts = [part.strip() for part in re.split(r"\s*/\s*", sentence) if part.strip()]
    result_parts = [
        part
        for part in parts
        if any(term in part.lower() for term in ["q2 results", "half year results", "half-year results", "h1 results"])
    ]
    return " / ".join(result_parts) if result_parts else sentence


def _relevant_money(numbers, event_type: ResearchEventType, evidence: str):
    if event_type in {
        ResearchEventType.NEW_ORDER,
        ResearchEventType.ORDER_BACKLOG_CHANGE,
        ResearchEventType.CAPEX,
        ResearchEventType.INVESTMENT,
        ResearchEventType.GUIDANCE_RAISED,
        ResearchEventType.GUIDANCE_CUT,
    }:
        return next((n for n in numbers if n.currency), None)
    return None


def _relevant_percent(numbers, event_type: ResearchEventType, evidence: str):
    if event_type in {
        ResearchEventType.NEW_ORDER,
        ResearchEventType.ORDER_BACKLOG_CHANGE,
        ResearchEventType.GUIDANCE_RAISED,
        ResearchEventType.GUIDANCE_CUT,
    }:
        return next((n for n in numbers if n.unit == "PERCENT"), None)
    if event_type == ResearchEventType.EARNINGS_RELEASE and any(
        term in evidence.lower() for term in ["revenue", "sales", "ebit", "ebitda", "margin", "profit", "cash flow", "cash-flow"]
    ):
        return next((n for n in numbers if n.unit == "PERCENT"), None)
    return None


def _relevant_capacity(numbers, event_type: ResearchEventType, evidence: str):
    if event_type in {ResearchEventType.CAPACITY_EXPANSION, ResearchEventType.NEW_FACILITY, ResearchEventType.FACTORY_EXPANSION}:
        return next((n for n in numbers if n.unit in {"MW", "GW", "UNITS"}), None)
    return None


def _extract_customer(text: str) -> str | None:
    match = re.search(r"(?:with|from|by|for|selected by)\s+([A-Z][A-Za-z0-9&.\- ]{2,80})(?:\.|,| for | to | worth | valued| as )", text)
    if not match:
        return None
    candidate = match.group(1).strip()
    return candidate if _valid_organization_candidate(candidate) else None


def _event_has_counterparty(event_type: ResearchEventType) -> bool:
    return event_type in {
        ResearchEventType.NEW_ORDER,
        ResearchEventType.MAJOR_CONTRACT,
        ResearchEventType.GOVERNMENT_CONTRACT,
        ResearchEventType.NEW_CUSTOMER,
        ResearchEventType.CUSTOMER_EXPANSION,
        ResearchEventType.MAJOR_CUSTOMER,
        ResearchEventType.PARTNERSHIP,
        ResearchEventType.ACQUISITION,
    }


def _valid_organization_candidate(candidate: str) -> bool:
    candidate = candidate.strip(" ,;:-")
    lower = candidate.lower()
    if re.search(r"\b(?:eur|usd|inr|million|billion|mn|bn|crore|lakh|percent|yoy|q[1-4]|h[1-2]|fy|fiscal)\b|[%\d]", lower):
        return False
    blocked = {
        "a customer",
        "a leading customer",
        "a leading optoelectronics customer",
        "the company",
        "management",
        "a year earlier",
        "the full year",
        "all customers",
        "its suppliers",
        "requested delivery dates",
    }
    if lower in blocked:
        return False
    if any(
        phrase in lower
        for phrase in [
            "a year earlier",
            "from eur",
            "from usd",
            "compared with",
            "quarter",
            "fiscal year",
            "full year",
            "order intake",
            "guidance",
            "shipment",
            "delivery",
            "cash-flow",
        ]
    ):
        return False
    if lower.startswith(("a ", "an ", "the ", "its ", "their ", "all ")):
        return False
    if len(candidate.split()) > 6:
        return False
    return bool(re.search(r"\b(?:AG|SE|Inc|Ltd|Limited|Corporation|Corp|GmbH|NV|S\.A\.|PLC|Group)\b", candidate))


def _deduplicate_events(events: list[ResearchEvent]) -> list[ResearchEvent]:
    seen: set[tuple[ResearchEventType, Decimal | None, str | None]] = set()
    unique: list[ResearchEvent] = []
    for event in events:
        key = (event.event_type, event.monetary_value, event.customer)
        if key not in seen:
            seen.add(key)
            unique.append(event)
    return unique
