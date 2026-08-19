package com.aiinvestment.broker.security;

import com.aiinvestment.shared.domain.broker.auth.BrokerSession;
import com.aiinvestment.shared.domain.broker.auth.SensitiveTokenReference;

import java.util.Optional;
import java.util.UUID;

public interface BrokerTokenStore {
    SensitiveTokenReference storeSessionToken(UUID connectionId, String secretValue);

    Optional<SensitiveTokenReference> getSessionTokenReference(UUID connectionId);

    void attachSession(BrokerSession session);

    Optional<BrokerSession> getSession(UUID connectionId);

    void revoke(UUID connectionId);
}
