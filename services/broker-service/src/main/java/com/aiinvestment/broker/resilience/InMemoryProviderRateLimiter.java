package com.aiinvestment.broker.resilience;

import com.aiinvestment.shared.domain.broker.BrokerType;
import org.springframework.stereotype.Component;

import java.time.Instant;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

@Component
public class InMemoryProviderRateLimiter implements ProviderRateLimiter {
    private final Map<String, Instant> nextAllowedAt = new ConcurrentHashMap<>();

    @Override
    public boolean tryAcquire(BrokerType brokerType, String operation) {
        String key = brokerType + ":" + operation;
        Instant now = Instant.now();
        Instant allowed = nextAllowedAt.get(key);
        if (allowed != null && allowed.isAfter(now)) {
            return false;
        }
        nextAllowedAt.put(key, now.plusMillis(100));
        return true;
    }
}
