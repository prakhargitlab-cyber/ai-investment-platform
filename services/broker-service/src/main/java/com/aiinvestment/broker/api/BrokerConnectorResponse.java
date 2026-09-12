package com.aiinvestment.broker.api;

import com.aiinvestment.broker.persistence.BrokerConnectorInstanceEntity;

import java.time.Instant;
import java.util.UUID;

public record BrokerConnectorResponse(
        UUID connectorId,
        UUID userId,
        String brokerType,
        String runtimeMode,
        String runtimeStatus,
        String authStatus,
        Instant lastHeartbeatAt,
        Instant lastAuthenticatedAt,
        int idleTimeoutSeconds,
        int sessionTimeoutSeconds,
        Instant createdAt,
        Instant updatedAt
) {
    public static BrokerConnectorResponse from(BrokerConnectorInstanceEntity entity) {
        return new BrokerConnectorResponse(entity.getConnectorId(), entity.getUserId(), entity.getBrokerType().name(),
                entity.getRuntimeMode().name(), entity.getRuntimeStatus().name(), entity.getAuthStatus().name(),
                entity.getLastHeartbeatAt(), entity.getLastAuthenticatedAt(), entity.getIdleTimeoutSeconds(),
                entity.getSessionTimeoutSeconds(), entity.getCreatedAt(), entity.getUpdatedAt());
    }
}
