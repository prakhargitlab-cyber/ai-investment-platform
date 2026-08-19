package com.aiinvestment.shared.domain.event;

import java.time.Instant;
import java.util.UUID;

public record PortfolioUpdatedEvent(String eventType, int version, UUID eventId, String correlationId, Instant occurredAt,
                                    UUID portfolioId, UUID userId) implements PlatformEvent {
    public PortfolioUpdatedEvent(UUID eventId, String correlationId, Instant occurredAt, UUID portfolioId, UUID userId) {
        this("portfolio.updated", 1, eventId, correlationId, occurredAt, portfolioId, userId);
    }
}
