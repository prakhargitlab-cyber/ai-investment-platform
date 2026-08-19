from __future__ import annotations

from uuid import UUID

from app.models import PlatformEvent, ResearchDocument, ResearchEvent


def document_event(event_type: str, document: ResearchDocument, correlation_id: str | None = None) -> PlatformEvent:
    return PlatformEvent(
        event_type=event_type,
        correlation_id=correlation_id,
        payload={
            "documentId": str(document.document_id),
            "instrumentId": str(document.instrument_id) if document.instrument_id else None,
            "sourceType": document.source_type,
            "status": document.status,
            "canonicalUrl": document.canonical_url,
        },
    )


def research_event_extracted(event: ResearchEvent, correlation_id: str | None = None) -> PlatformEvent:
    return PlatformEvent(
        event_type="research.event.extracted",
        correlation_id=correlation_id,
        payload={
            "eventId": str(event.event_id),
            "instrumentId": str(event.instrument_id),
            "eventType": event.event_type,
            "impact": event.impact,
            "confidence": event.confidence,
            "sourceDocumentId": str(event.source_document_id),
        },
    )


def company_updated(instrument_id: UUID, correlation_id: str | None = None) -> PlatformEvent:
    return PlatformEvent(
        event_type="research.company.updated",
        correlation_id=correlation_id,
        payload={"instrumentId": str(instrument_id)},
    )
