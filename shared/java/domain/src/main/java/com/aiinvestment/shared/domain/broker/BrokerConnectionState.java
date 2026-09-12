package com.aiinvestment.shared.domain.broker;

public enum BrokerConnectionState {
    CREATED,
    STARTING,
    AUTHENTICATION_REQUIRED,
    AUTHENTICATING,
    CONNECTING,
    CONNECTED,
    SYNCING,
    REFRESH_REQUIRED,
    EXPIRED,
    SESSION_EXPIRED,
    REVOKED,
    DEGRADED,
    STOPPED,
    DISCONNECTED,
    ERROR
}
