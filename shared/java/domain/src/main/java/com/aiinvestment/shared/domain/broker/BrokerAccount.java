package com.aiinvestment.shared.domain.broker;

import java.util.Objects;
import java.util.UUID;

public record BrokerAccount(
        String brokerAccountId,
        UUID userId,
        BrokerType brokerType,
        String externalAccountReference,
        String displayName,
        String baseCurrency,
        BrokerAccountStatus status
) {
    public BrokerAccount {
        requireText(brokerAccountId, "brokerAccountId");
        Objects.requireNonNull(userId, "userId is required");
        Objects.requireNonNull(brokerType, "brokerType is required");
        requireText(displayName, "displayName");
        if (baseCurrency != null) {
            requireCurrency(baseCurrency, "baseCurrency");
        }
        Objects.requireNonNull(status, "status is required");
    }

    private static void requireText(String value, String name) {
        if (value == null || value.isBlank()) {
            throw new IllegalArgumentException(name + " is required");
        }
    }

    private static void requireCurrency(String value, String name) {
        requireText(value, name);
        if (!value.matches("[A-Z]{3}")) {
            throw new IllegalArgumentException(name + " must be a 3-letter ISO currency code");
        }
    }
}
