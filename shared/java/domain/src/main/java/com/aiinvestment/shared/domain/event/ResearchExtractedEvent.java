package com.aiinvestment.shared.domain.event;

import java.time.Instant;
import java.util.UUID;

public record ResearchExtractedEvent(
        String eventType,
        int version,
        UUID eventId,
        String correlationId,
        Instant occurredAt,
        UUID researchEventId,
        UUID instrumentId,
        UUID sourceDocumentId,
        String researchEventType,
        String impact,
        double confidence,
        String rejectionCode
) implements PlatformEvent {
    public static ResearchExtractedEvent extracted(UUID researchEventId, UUID instrumentId, UUID sourceDocumentId,
                                                   String researchEventType, String impact, double confidence,
                                                   String correlationId) {
        return new ResearchExtractedEvent("research.event.extracted", 1, UUID.randomUUID(), correlationId, Instant.now(),
                researchEventId, instrumentId, sourceDocumentId, researchEventType, impact, confidence, null);
    }

    public static ResearchExtractedEvent rejected(UUID instrumentId, UUID sourceDocumentId, String rejectionCode, String correlationId) {
        return new ResearchExtractedEvent("research.event.rejected", 1, UUID.randomUUID(), correlationId, Instant.now(),
                null, instrumentId, sourceDocumentId, null, null, 0.0, rejectionCode);
    }
}
