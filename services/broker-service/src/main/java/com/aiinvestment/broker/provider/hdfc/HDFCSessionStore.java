package com.aiinvestment.broker.provider.hdfc;

import java.util.Optional;
import java.util.UUID;

public interface HDFCSessionStore {
    void markLoginInitiated(UUID userId, UUID connectionId);
    boolean consumeLoginInitiated(UUID userId, UUID connectionId);
    void store(HDFCConnectorSession session);
    Optional<HDFCConnectorSession> find(UUID userId, UUID connectionId);
    void remove(UUID userId, UUID connectionId);
}
