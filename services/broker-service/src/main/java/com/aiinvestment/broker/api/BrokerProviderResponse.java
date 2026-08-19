package com.aiinvestment.broker.api;

import com.aiinvestment.shared.domain.broker.BrokerProvider;
import com.aiinvestment.shared.domain.broker.BrokerType;

import java.util.Set;
import java.util.stream.Collectors;

public record BrokerProviderResponse(
        BrokerType brokerType,
        String status,
        String providerStatus,
        String code,
        String message,
        Set<String> capabilities,
        String connectionMethod,
        String dataFreshness,
        boolean readOnly,
        boolean officialProviderSetupRequired
) {
    public static BrokerProviderResponse from(BrokerProvider provider) {
        boolean mock = provider.supportedBroker() == BrokerType.MOCK;
        boolean setupRequired = !mock && provider.connectionStatus().providerStatus().name().matches("NOT_CONFIGURED|DOCUMENTATION_REQUIRED");
        return new BrokerProviderResponse(provider.supportedBroker(), provider.connectionStatus().state().name(),
                provider.connectionStatus().providerStatus().name(), provider.connectionStatus().code(), provider.connectionStatus().message(),
                provider.connectionCapabilities().capabilities().stream().map(Enum::name).collect(Collectors.toUnmodifiableSet()),
                mock ? "Local demo provider" : "Official provider setup required",
                mock ? "DEMO" : "UNAVAILABLE",
                !provider.connectionCapabilities().capabilities().contains(com.aiinvestment.shared.domain.broker.BrokerCapability.ORDER_EXECUTION),
                setupRequired);
    }
}
