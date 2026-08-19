package com.aiinvestment.shared.domain.broker;

public enum BrokerProviderStatus {
    NOT_CONFIGURED,
    DOCUMENTATION_REQUIRED,
    AUTHENTICATION_REQUIRED,
    CONNECTED,
    DEGRADED,
    UNAVAILABLE,
    ERROR
}
