package com.aiinvestment.broker.auth;

import com.aiinvestment.shared.domain.broker.BrokerType;
import com.aiinvestment.shared.domain.broker.auth.BrokerAuthenticationRequest;
import com.aiinvestment.shared.domain.broker.auth.BrokerAuthenticationStrategy;
import com.aiinvestment.shared.domain.broker.auth.BrokerSession;
import com.aiinvestment.shared.domain.broker.auth.BrokerSessionState;
import org.springframework.stereotype.Component;

import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.UUID;

@Component
public class MockBrokerAuthenticationStrategy implements BrokerAuthenticationStrategy {
    @Override
    public BrokerType supportedBroker() {
        return BrokerType.MOCK;
    }

    @Override
    public BrokerSession authenticate(BrokerAuthenticationRequest request) {
        return new BrokerSession(UUID.randomUUID(), UUID.randomUUID(), BrokerType.MOCK, BrokerSessionState.CONNECTED, Instant.now().plus(1, ChronoUnit.HOURS), true);
    }

    @Override
    public BrokerSession refresh(BrokerSession session) {
        return new BrokerSession(session.sessionId(), session.connectionId(), session.brokerType(), BrokerSessionState.CONNECTED, Instant.now().plus(1, ChronoUnit.HOURS), true);
    }
}
