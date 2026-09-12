package com.aiinvestment.broker.provider.hdfc;

import java.time.Instant;
import java.util.UUID;

public record HDFCConnectorSession(UUID userId, UUID connectionId, String accessToken,
                                   Instant authenticatedAt, Instant expiresAt) {
    public boolean expired(Instant now) { return !expiresAt.isAfter(now); }
}
