package com.aiinvestment.broker.security;

import com.aiinvestment.shared.domain.broker.BrokerType;
import org.springframework.stereotype.Component;

import java.util.Optional;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;

/** DEV store. PRD replaces this boundary with Key Vault/SecretProvider storage. */
@Component
public class InMemoryBrokerCredentialStore implements BrokerCredentialStore {
    private final ConcurrentHashMap<Key, BrokerCredentials> credentials = new ConcurrentHashMap<>();

    @Override
    public void store(UUID userId, UUID connectionId, BrokerType provider, String clientKey, String clientSecret) {
        if (clientKey == null || clientKey.isBlank() || clientSecret == null || clientSecret.isBlank()) {
            throw new IllegalArgumentException("Both provider API credentials are required");
        }
        credentials.put(new Key(userId, connectionId, provider), new BrokerCredentials(clientKey, clientSecret));
    }

    @Override
    public Optional<BrokerCredentials> find(UUID userId, UUID connectionId, BrokerType provider) {
        return Optional.ofNullable(credentials.get(new Key(userId, connectionId, provider)));
    }

    @Override
    public void remove(UUID userId, UUID connectionId) {
        credentials.keySet().removeIf(key -> key.userId.equals(userId) && key.connectionId.equals(connectionId));
    }

    private record Key(UUID userId, UUID connectionId, BrokerType provider) {}
}
