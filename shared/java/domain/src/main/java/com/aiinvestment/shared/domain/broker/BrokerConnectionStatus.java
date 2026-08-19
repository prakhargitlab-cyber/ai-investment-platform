package com.aiinvestment.shared.domain.broker;

public record BrokerConnectionStatus(BrokerType brokerType, BrokerConnectionState state, BrokerProviderStatus providerStatus, String code, String message) {
    public static BrokerConnectionStatus notConfigured(BrokerType brokerType) {
        return new BrokerConnectionStatus(brokerType, BrokerConnectionState.DISCONNECTED, BrokerProviderStatus.NOT_CONFIGURED, "NOT_CONFIGURED", "Provider is not configured");
    }

    public static BrokerConnectionStatus documentationRequired(BrokerType brokerType) {
        return new BrokerConnectionStatus(brokerType, BrokerConnectionState.DISCONNECTED, BrokerProviderStatus.DOCUMENTATION_REQUIRED,
                "DOCUMENTATION_REQUIRED", "Official provider documentation is required before this adapter can be configured");
    }

    public static BrokerConnectionStatus authenticationRequired(BrokerType brokerType) {
        return new BrokerConnectionStatus(brokerType, BrokerConnectionState.AUTHENTICATING, BrokerProviderStatus.AUTHENTICATION_REQUIRED,
                "AUTHENTICATION_REQUIRED", "Provider configuration is present, but a supported authentication flow must be completed");
    }

    public static BrokerConnectionStatus unavailable(BrokerType brokerType, String message) {
        return new BrokerConnectionStatus(brokerType, BrokerConnectionState.DISCONNECTED, BrokerProviderStatus.UNAVAILABLE,
                "UNAVAILABLE", message);
    }
}
