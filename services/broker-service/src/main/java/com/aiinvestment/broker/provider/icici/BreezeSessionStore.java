package com.aiinvestment.broker.provider.icici;

import java.util.Optional;
import java.util.UUID;

interface BreezeSessionStore {
    void markLoginInitiated(UUID userId, UUID connectionId);
    boolean consumeLoginInitiated(UUID userId, UUID connectionId);
    void store(BreezeSession session);
    Optional<BreezeSession> find(UUID userId, UUID connectionId);
    void remove(UUID userId, UUID connectionId);
}
