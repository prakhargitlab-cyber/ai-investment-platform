package com.aiinvestment.broker.resilience;

import com.aiinvestment.shared.domain.broker.BrokerType;
import org.springframework.stereotype.Component;

import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

@Component
public class InMemoryProviderCircuitBreaker implements ProviderCircuitBreaker {
    private final Map<BrokerType, Integer> failures = new ConcurrentHashMap<>();

    @Override
    public boolean allowRequest(BrokerType brokerType) {
        return failures.getOrDefault(brokerType, 0) < 3;
    }

    @Override
    public void recordSuccess(BrokerType brokerType) {
        failures.remove(brokerType);
    }

    @Override
    public void recordFailure(BrokerType brokerType) {
        failures.merge(brokerType, 1, Integer::sum);
    }
}
