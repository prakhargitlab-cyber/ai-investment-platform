package com.aiinvestment.shared.domain.event;

import com.aiinvestment.shared.domain.broker.BrokerConnectionState;
import com.aiinvestment.shared.domain.broker.BrokerType;

import java.time.Instant;
import java.util.UUID;

public record BrokerConnectionChangedEvent(
        String eventType,
        int version,
        UUID eventId,
        String correlationId,
        Instant occurredAt,
        UUID connectionId,
        UUID userId,
        BrokerType brokerType,
        BrokerConnectionState status
) implements PlatformEvent {
    public BrokerConnectionChangedEvent(UUID eventId, String correlationId, Instant occurredAt, UUID connectionId,
                                        UUID userId, BrokerType brokerType, BrokerConnectionState status) {
        this("broker.connection.changed", 1, eventId, correlationId, occurredAt, connectionId, userId, brokerType, status);
    }
}
