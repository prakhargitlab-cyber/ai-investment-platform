package com.aiinvestment.broker.api;

import com.aiinvestment.broker.domain.BrokerConnection;
import com.aiinvestment.shared.domain.broker.BrokerProvider;

import java.time.Instant;
import java.util.Set;
import java.util.UUID;
import java.util.stream.Collectors;

public record BrokerConnectionResponse(
        UUID connectionId,
        UUID userId,
        String brokerType,
        String externalAccountReference,
        String displayName,
        String status,
        Instant connectedAt,
        Instant lastSuccessfulSyncAt,
        Instant lastSyncAttemptAt,
        String lastErrorCode,
        Instant createdAt,
        Instant updatedAt,
        Set<String> capabilities,
        String providerStatus,
        String dataFreshness,
        boolean readOnly
) {
    public static BrokerConnectionResponse from(BrokerConnection connection, BrokerProvider provider) {
        return new BrokerConnectionResponse(connection.connectionId(), connection.userId(), connection.brokerType().name(),
                connection.externalAccountReference(), connection.displayName(), connection.status().name(),
                connection.connectedAt(), connection.lastSuccessfulSyncAt(), connection.lastSyncAttemptAt(),
                connection.lastErrorCode(), connection.createdAt(), connection.updatedAt(),
                provider.connectionCapabilities().capabilities().stream().map(Enum::name).collect(Collectors.toUnmodifiableSet()),
                provider.connectionStatus().providerStatus().name(),
                connection.brokerType().name().equals("MOCK") ? "DEMO" : "UNAVAILABLE",
                !provider.connectionCapabilities().capabilities().contains(com.aiinvestment.shared.domain.broker.BrokerCapability.ORDER_EXECUTION));
    }
}
