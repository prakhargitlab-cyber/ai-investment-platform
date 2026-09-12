package com.aiinvestment.broker.connector;

public enum BrokerConnectorState {
    NOT_CONFIGURED,
    STARTING,
    AUTHENTICATION_REQUIRED,
    AUTHENTICATING,
    CONNECTED,
    SESSION_EXPIRED,
    DEGRADED,
    STOPPING,
    STOPPED,
    ERROR
}
