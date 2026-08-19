package com.aiinvestment.broker.domain;

import com.aiinvestment.shared.domain.broker.BrokerConnectionState;
import com.aiinvestment.shared.domain.broker.BrokerType;

import java.time.Instant;
import java.util.UUID;

public record BrokerConnection(
        UUID connectionId,
        UUID userId,
        BrokerType brokerType,
        String externalAccountReference,
        String displayName,
        BrokerConnectionState status,
        Instant connectedAt,
        Instant lastSuccessfulSyncAt,
        Instant lastSyncAttemptAt,
        String lastErrorCode,
        Instant createdAt,
        Instant updatedAt
) {
}
