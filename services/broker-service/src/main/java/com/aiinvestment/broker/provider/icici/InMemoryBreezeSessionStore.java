package com.aiinvestment.broker.provider.icici;

import org.springframework.stereotype.Component;

import java.util.Map;
import java.util.Optional;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;

@Component
class InMemoryBreezeSessionStore implements BreezeSessionStore {
    private record Key(UUID userId, UUID connectionId) {}

    private final Map<Key, Boolean> pendingLogins = new ConcurrentHashMap<>();
    private final Map<Key, BreezeSession> sessions = new ConcurrentHashMap<>();

    @Override
    public void markLoginInitiated(UUID userId, UUID connectionId) {
        pendingLogins.put(new Key(userId, connectionId), Boolean.TRUE);
    }

    @Override
    public boolean consumeLoginInitiated(UUID userId, UUID connectionId) {
        return pendingLogins.remove(new Key(userId, connectionId)) != null;
    }

    @Override
    public void store(BreezeSession session) {
        sessions.put(new Key(session.userId(), session.connectionId()), session);
    }

    @Override
    public Optional<BreezeSession> find(UUID userId, UUID connectionId) {
        return Optional.ofNullable(sessions.get(new Key(userId, connectionId)));
    }

    @Override
    public void remove(UUID userId, UUID connectionId) {
        Key key = new Key(userId, connectionId);
        pendingLogins.remove(key);
        sessions.remove(key);
    }
}
