package com.aiinvestment.shared.domain.broker.auth;

import com.aiinvestment.shared.domain.broker.BrokerType;

public interface BrokerAuthenticationStrategy {
    BrokerType supportedBroker();

    BrokerSession authenticate(BrokerAuthenticationRequest request);

    BrokerSession refresh(BrokerSession session);
}
