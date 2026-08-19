package com.aiinvestment.broker.resilience;

import com.aiinvestment.shared.domain.broker.BrokerType;

public interface ProviderCircuitBreaker {
    boolean allowRequest(BrokerType brokerType);

    void recordSuccess(BrokerType brokerType);

    void recordFailure(BrokerType brokerType);
}
