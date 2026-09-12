package com.aiinvestment.portfolio.application;

import java.time.Instant;
import java.util.Set;
import java.util.UUID;

public record BrokerConnectionClientResponse(
        UUID connectionId,
        String brokerType,
        String displayName,
        String status,
        Instant lastSuccessfulSyncAt,
        Instant lastSyncAttemptAt,
        String lastErrorCode,
        Set<String> capabilities,
        String providerStatus
) {
    public boolean connected() {
        return "CONNECTED".equals(status) && "CONNECTED".equals(providerStatus);
    }
}
