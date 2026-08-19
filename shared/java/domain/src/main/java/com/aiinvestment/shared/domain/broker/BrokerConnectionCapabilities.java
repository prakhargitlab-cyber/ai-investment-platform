package com.aiinvestment.shared.domain.broker;

import java.util.EnumSet;
import java.util.Set;

public record BrokerConnectionCapabilities(Set<BrokerCapability> capabilities) {
    public BrokerConnectionCapabilities {
        capabilities = Set.copyOf(capabilities);
        if (capabilities.contains(BrokerCapability.ORDER_EXECUTION)) {
            throw new IllegalArgumentException("ORDER_EXECUTION is not enabled in Phase 2B");
        }
    }

    public static BrokerConnectionCapabilities none() {
        return new BrokerConnectionCapabilities(Set.of());
    }

    public static BrokerConnectionCapabilities mockReadOnly() {
        return new BrokerConnectionCapabilities(EnumSet.of(
                BrokerCapability.ACCOUNTS_READ,
                BrokerCapability.ACCOUNT_METADATA_READ,
                BrokerCapability.PORTFOLIO_READ,
                BrokerCapability.POSITIONS_READ,
                BrokerCapability.CASH_READ,
                BrokerCapability.MARKET_DATA_READ,
                BrokerCapability.DELAYED_QUOTES
        ));
    }
}
