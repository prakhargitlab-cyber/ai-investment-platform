package com.aiinvestment.broker.provider.icici;

import java.time.Instant;
import java.util.UUID;

record BreezeSession(UUID userId, UUID connectionId, String sessionToken,
                     String providerUserId, String displayName,
                     Instant authenticatedAt, Instant expiresAt) {
    boolean expired(Instant now) {
        return !now.isBefore(expiresAt);
    }
}
