package com.aiinvestment.broker.security;

import com.aiinvestment.shared.domain.broker.auth.BrokerSession;
import com.aiinvestment.shared.domain.broker.auth.SensitiveTokenReference;
import org.springframework.stereotype.Component;

import java.util.Map;
import java.util.Optional;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;

@Component
public class InMemoryBrokerTokenStore implements BrokerTokenStore {
    private final Map<UUID, SensitiveTokenReference> tokenReferences = new ConcurrentHashMap<>();
    private final Map<UUID, BrokerSession> sessions = new ConcurrentHashMap<>();

    @Override
    public SensitiveTokenReference storeSessionToken(UUID connectionId, String secretValue) {
        if (secretValue == null || secretValue.isBlank()) {
            throw new IllegalArgumentException("secretValue is required");
        }
        SensitiveTokenReference reference = new SensitiveTokenReference("env-or-vault:" + connectionId);
        tokenReferences.put(connectionId, reference);
        return reference;
    }

    @Override
    public Optional<SensitiveTokenReference> getSessionTokenReference(UUID connectionId) {
        return Optional.ofNullable(tokenReferences.get(connectionId));
    }

    @Override
    public void attachSession(BrokerSession session) {
        sessions.put(session.connectionId(), session);
    }

    @Override
    public Optional<BrokerSession> getSession(UUID connectionId) {
        return Optional.ofNullable(sessions.get(connectionId));
    }

    @Override
    public void revoke(UUID connectionId) {
        tokenReferences.remove(connectionId);
        sessions.remove(connectionId);
    }
}
