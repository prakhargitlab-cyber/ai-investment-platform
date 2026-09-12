package com.aiinvestment.broker.api;

import java.util.UUID;

public record BrokerConnectorLoginResponse(
        UUID connectorId,
        String loginUrl,
        String authStatus
) {
}
