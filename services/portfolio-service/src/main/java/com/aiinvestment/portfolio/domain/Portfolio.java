package com.aiinvestment.portfolio.domain;

import java.time.Instant;
import java.util.Objects;
import java.util.UUID;
import com.aiinvestment.shared.domain.broker.BrokerType;

public record Portfolio(
        UUID portfolioId,
        UUID userId,
        String name,
        String baseCurrency,
        Instant createdAt,
        Instant updatedAt,
        UUID brokerConnectionId,
        String brokerAccountId,
        BrokerType brokerProvider,
        Instant lastSuccessfulBrokerSyncAt,
        Instant lastBrokerSyncAttemptAt,
        String lastBrokerSyncErrorCode,
        String acquisitionSource,
        String sourceAccountReference,
        Instant lastImportedAt,
        String lastImportedFilename
) {
    public Portfolio(UUID portfolioId, UUID userId, String name, String baseCurrency, Instant createdAt, Instant updatedAt) {
        this(portfolioId, userId, name, baseCurrency, createdAt, updatedAt, null, null, null, null, null, null,
                null, null, null, null);
    }
    public Portfolio {
        Objects.requireNonNull(portfolioId, "portfolioId is required");
        Objects.requireNonNull(userId, "userId is required");
        if (name == null || name.isBlank()) {
            throw new IllegalArgumentException("name is required");
        }
        if (baseCurrency == null || !baseCurrency.matches("[A-Z]{3}")) {
            throw new IllegalArgumentException("baseCurrency must be a 3-letter ISO currency code");
        }
        Objects.requireNonNull(createdAt, "createdAt is required");
        Objects.requireNonNull(updatedAt, "updatedAt is required");
    }
}
