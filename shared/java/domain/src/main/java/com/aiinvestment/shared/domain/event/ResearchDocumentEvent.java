package com.aiinvestment.shared.domain.event;

import java.time.Instant;
import java.util.UUID;

public record ResearchDocumentEvent(
        String eventType,
        int version,
        UUID eventId,
        String correlationId,
        Instant occurredAt,
        UUID documentId,
        UUID instrumentId,
        String sourceType,
        String status
) implements PlatformEvent {
    public static ResearchDocumentEvent discovered(UUID documentId, UUID instrumentId, String sourceType, String correlationId) {
        return event("research.document.discovered", documentId, instrumentId, sourceType, "DISCOVERED", correlationId);
    }

    public static ResearchDocumentEvent fetched(UUID documentId, UUID instrumentId, String sourceType, String correlationId) {
        return event("research.document.fetched", documentId, instrumentId, sourceType, "FETCHED", correlationId);
    }

    public static ResearchDocumentEvent processed(UUID documentId, UUID instrumentId, String sourceType, String correlationId) {
        return event("research.document.processed", documentId, instrumentId, sourceType, "PROCESSED", correlationId);
    }

    private static ResearchDocumentEvent event(String type, UUID documentId, UUID instrumentId, String sourceType, String status, String correlationId) {
        return new ResearchDocumentEvent(type, 1, UUID.randomUUID(), correlationId, Instant.now(), documentId, instrumentId, sourceType, status);
    }
}
