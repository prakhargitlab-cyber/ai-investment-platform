package com.aiinvestment.shared.domain.broker;

public enum BrokerConnectionState {
    CREATED,
    AUTHENTICATING,
    CONNECTING,
    CONNECTED,
    SYNCING,
    REFRESH_REQUIRED,
    EXPIRED,
    REVOKED,
    DEGRADED,
    DISCONNECTED,
    ERROR
}
