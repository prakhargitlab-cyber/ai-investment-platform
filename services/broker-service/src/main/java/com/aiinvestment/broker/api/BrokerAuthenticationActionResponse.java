package com.aiinvestment.broker.api;

import java.util.UUID;

public record BrokerAuthenticationActionResponse(
        UUID connectionId,
        String provider,
        String status,
        String action,
        String authenticationUrl,
        String message
) {
}
