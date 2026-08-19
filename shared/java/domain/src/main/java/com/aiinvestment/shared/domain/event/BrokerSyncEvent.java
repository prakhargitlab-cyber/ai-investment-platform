package com.aiinvestment.shared.domain.event;

import java.time.Instant;
import java.util.UUID;

public record BrokerSyncEvent(
        String eventType,
        int version,
        UUID eventId,
        String correlationId,
        Instant occurredAt,
        UUID connectionId,
        UUID portfolioId,
        String errorCode
) implements PlatformEvent {
    public static BrokerSyncEvent started(UUID connectionId, UUID portfolioId, String correlationId) {
        return event("broker.sync.started", connectionId, portfolioId, correlationId, null);
    }

    public static BrokerSyncEvent completed(UUID connectionId, UUID portfolioId, String correlationId) {
        return event("broker.sync.completed", connectionId, portfolioId, correlationId, null);
    }

    public static BrokerSyncEvent failed(UUID connectionId, UUID portfolioId, String correlationId, String errorCode) {
        return event("broker.sync.failed", connectionId, portfolioId, correlationId, errorCode);
    }

    private static BrokerSyncEvent event(String type, UUID connectionId, UUID portfolioId, String correlationId, String errorCode) {
        return new BrokerSyncEvent(type, 1, UUID.randomUUID(), correlationId, Instant.now(), connectionId, portfolioId, errorCode);
    }
}
