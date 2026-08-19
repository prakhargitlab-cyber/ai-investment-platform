package com.aiinvestment.shared.domain.broker.auth;

public enum BrokerSessionState {
    CREATED,
    AUTHENTICATING,
    CONNECTED,
    REFRESH_REQUIRED,
    EXPIRED,
    REVOKED,
    ERROR
}
