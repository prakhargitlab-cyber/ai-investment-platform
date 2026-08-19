package com.aiinvestment.shared.domain.event;

import java.time.Instant;
import java.util.UUID;

public record ResearchCompanyUpdatedEvent(
        String eventType,
        int version,
        UUID eventId,
        String correlationId,
        Instant occurredAt,
        UUID instrumentId,
        UUID companyId
) implements PlatformEvent {
    public ResearchCompanyUpdatedEvent(UUID eventId, String correlationId, Instant occurredAt, UUID instrumentId, UUID companyId) {
        this("research.company.updated", 1, eventId, correlationId, occurredAt, instrumentId, companyId);
    }
}
