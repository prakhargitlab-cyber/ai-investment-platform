package com.aiinvestment.broker.provider.hdfc;

import org.springframework.stereotype.Component;
import java.util.Optional;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;

@Component
public class InMemoryHDFCSessionStore implements HDFCSessionStore {
    private final ConcurrentHashMap<Key, HDFCConnectorSession> sessions = new ConcurrentHashMap<>();
    private final Set<Key> logins = ConcurrentHashMap.newKeySet();
    public void markLoginInitiated(UUID userId, UUID connectionId) { logins.add(new Key(userId, connectionId)); }
    public boolean consumeLoginInitiated(UUID userId, UUID connectionId) { return logins.remove(new Key(userId, connectionId)); }
    public void store(HDFCConnectorSession session) { sessions.put(new Key(session.userId(), session.connectionId()), session); }
    public Optional<HDFCConnectorSession> find(UUID userId, UUID connectionId) { return Optional.ofNullable(sessions.get(new Key(userId, connectionId))); }
    public void remove(UUID userId, UUID connectionId) { sessions.remove(new Key(userId, connectionId)); logins.remove(new Key(userId, connectionId)); }
    private record Key(UUID userId, UUID connectionId) {}
}
