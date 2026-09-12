package com.aiinvestment.broker.persistence;

import com.aiinvestment.broker.domain.BrokerConnection;

public final class BrokerConnectionMapper {
    private BrokerConnectionMapper() {
    }

    public static BrokerConnection toDomain(BrokerConnectionEntity entity) {
        return new BrokerConnection(entity.getConnectionId(), entity.getUserId(), entity.getBrokerType(),
                entity.getExternalAccountReference(), entity.getDisplayName(), entity.getStatus(),
                entity.getConnectorId(), entity.getAccountCurrency(), entity.getProviderStatus(), entity.getDataFreshness(),
                entity.getSessionReference(), entity.getCapabilities(),
                entity.getConnectedAt(), entity.getLastSuccessfulSyncAt(), entity.getLastSyncAttemptAt(),
                entity.getLastErrorCode(), entity.getCreatedAt(), entity.getUpdatedAt());
    }
}
