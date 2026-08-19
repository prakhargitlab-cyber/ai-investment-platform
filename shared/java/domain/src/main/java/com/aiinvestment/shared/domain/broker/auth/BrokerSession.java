package com.aiinvestment.shared.domain.broker.auth;

import com.aiinvestment.shared.domain.broker.BrokerType;

import java.time.Instant;
import java.util.UUID;

public record BrokerSession(UUID sessionId, UUID connectionId, BrokerType brokerType, BrokerSessionState state, Instant expiresAt, boolean mock) {
}
