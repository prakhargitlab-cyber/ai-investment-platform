package com.aiinvestment.broker.api;

import com.aiinvestment.broker.domain.BrokerConnection;
import com.aiinvestment.shared.domain.broker.BrokerProvider;

import java.time.Instant;
import java.util.Set;
import java.util.UUID;

public record BrokerConnectionResponse(
        UUID connectionId,
        String brokerType,
        String displayName,
        String status,
        String accountCurrency,
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
        String providerStatus = connection.providerStatus() == null || connection.providerStatus().isBlank()
                ? provider.connectionStatus().providerStatus().name()
                : connection.providerStatus();
        String dataFreshness = connection.dataFreshness() == null || connection.dataFreshness().isBlank()
                ? (connection.brokerType().name().equals("MOCK") ? "DEMO" : "UNAVAILABLE")
                : connection.dataFreshness();
        return new BrokerConnectionResponse(connection.connectionId(), connection.brokerType().name(),
                connection.displayName(), connection.status().name(),
                connection.accountCurrency(),
                connection.connectedAt(), connection.lastSuccessfulSyncAt(), connection.lastSyncAttemptAt(),
                connection.lastErrorCode(), connection.createdAt(), connection.updatedAt(),
                BrokerProviderResponse.capabilities(provider, connection),
                providerStatus,
                dataFreshness,
                !BrokerProviderResponse.capabilities(provider, connection)
                        .contains(com.aiinvestment.shared.domain.broker.BrokerCapability.ORDER_EXECUTION.name()));
    }
}
