package com.aiinvestment.broker.resilience;

import com.aiinvestment.shared.domain.broker.BrokerType;

public interface ProviderRateLimiter {
    boolean tryAcquire(BrokerType brokerType, String operation);
}
