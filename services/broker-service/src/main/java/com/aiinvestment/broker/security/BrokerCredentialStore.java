package com.aiinvestment.broker.security;

import com.aiinvestment.shared.domain.broker.BrokerType;

import java.util.Optional;
import java.util.UUID;

/** Secret values are deliberately outside relational broker entities. */
public interface BrokerCredentialStore {
    void store(UUID userId, UUID connectionId, BrokerType provider, String clientKey, String clientSecret);
    Optional<BrokerCredentials> find(UUID userId, UUID connectionId, BrokerType provider);
    void remove(UUID userId, UUID connectionId);

    record BrokerCredentials(String clientKey, String clientSecret) {}
}
