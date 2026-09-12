package com.aiinvestment.broker.api;

import java.util.UUID;

public record BrokerAuthStatusResponse(UUID connectionId, String state, boolean authenticated) {
}
