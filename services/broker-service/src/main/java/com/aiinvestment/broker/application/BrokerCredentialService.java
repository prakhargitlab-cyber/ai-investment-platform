package com.aiinvestment.broker.application;

import com.aiinvestment.broker.api.BrokerCredentialStatusResponse;
import com.aiinvestment.broker.persistence.BrokerConnectionRepository;
import com.aiinvestment.broker.security.BrokerCredentialStore;
import com.aiinvestment.shared.domain.broker.BrokerType;
import org.springframework.stereotype.Service;

import java.util.List;
import java.util.UUID;

@Service
public class BrokerCredentialService {
    private final BrokerConnectionRepository connections;
    private final BrokerCredentialStore store;

    public BrokerCredentialService(BrokerConnectionRepository connections, BrokerCredentialStore store) {
        this.connections = connections;
        this.store = store;
    }

    public BrokerCredentialStatusResponse configure(UUID userId, UUID connectionId, String key, String secret) {
        var connection = connections.findByConnectionIdAndUserId(connectionId, userId)
                .orElseThrow(() -> new BrokerConnectionNotFoundException(connectionId));
        if (connection.getBrokerType() != BrokerType.ICICI_DIRECT
                && connection.getBrokerType() != BrokerType.HDFC_SECURITIES) {
            throw new IllegalArgumentException("This provider does not use individual API credentials");
        }
        store.store(userId, connectionId, connection.getBrokerType(), key, secret);
        return status(userId, connectionId);
    }

    public BrokerCredentialStatusResponse status(UUID userId, UUID connectionId) {
        var connection = connections.findByConnectionIdAndUserId(connectionId, userId)
                .orElseThrow(() -> new BrokerConnectionNotFoundException(connectionId));
        boolean configured = store.find(userId, connectionId, connection.getBrokerType()).isPresent();
        return new BrokerCredentialStatusResponse(connectionId, connection.getBrokerType().name(), configured,
                List.of("CLIENT_KEY", "CLIENT_SECRET"),
                configured ? "API access is configured." : "Configure API access to continue with the broker.");
    }
}
