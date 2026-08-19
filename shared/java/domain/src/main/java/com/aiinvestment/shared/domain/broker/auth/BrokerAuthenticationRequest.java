package com.aiinvestment.shared.domain.broker.auth;

import com.aiinvestment.shared.domain.broker.BrokerType;

import java.util.Map;
import java.util.UUID;

public record BrokerAuthenticationRequest(UUID userId, BrokerType brokerType, Map<String, String> nonSecretMetadata) {
    public BrokerAuthenticationRequest {
        nonSecretMetadata = Map.copyOf(nonSecretMetadata);
    }
}
