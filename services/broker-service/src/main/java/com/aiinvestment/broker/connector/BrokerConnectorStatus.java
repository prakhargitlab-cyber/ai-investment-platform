package com.aiinvestment.broker.connector;

import java.time.Instant;

public record BrokerConnectorStatus(
        BrokerConnectorState runtimeStatus,
        BrokerConnectorState authStatus,
        Instant heartbeatAt,
        Instant authenticatedAt,
        String loginUrl,
        String code,
        String message
) {
    public BrokerConnectorStatus(BrokerConnectorState runtimeStatus, BrokerConnectorState authStatus,
                                 Instant heartbeatAt, Instant authenticatedAt, String code, String message) {
        this(runtimeStatus, authStatus, heartbeatAt, authenticatedAt, null, code, message);
    }

    public boolean authenticated() {
        return authStatus == BrokerConnectorState.CONNECTED;
    }
}
