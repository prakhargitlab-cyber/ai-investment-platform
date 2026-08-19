package com.aiinvestment.broker.resilience;

import com.aiinvestment.shared.domain.broker.BrokerType;
import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;

class ProviderResilienceTest {
    @Test
    void rateLimiterIsProviderAndOperationSpecific() {
        InMemoryProviderRateLimiter limiter = new InMemoryProviderRateLimiter();

        assertThat(limiter.tryAcquire(BrokerType.MOCK, "sync")).isTrue();
        assertThat(limiter.tryAcquire(BrokerType.MOCK, "sync")).isFalse();
        assertThat(limiter.tryAcquire(BrokerType.MOCK, "status")).isTrue();
        assertThat(limiter.tryAcquire(BrokerType.IBKR, "sync")).isTrue();
    }

    @Test
    void circuitBreakerOpensAfterBoundedFailuresAndClosesOnSuccess() {
        InMemoryProviderCircuitBreaker breaker = new InMemoryProviderCircuitBreaker();

        breaker.recordFailure(BrokerType.IBKR);
        breaker.recordFailure(BrokerType.IBKR);
        assertThat(breaker.allowRequest(BrokerType.IBKR)).isTrue();

        breaker.recordFailure(BrokerType.IBKR);
        assertThat(breaker.allowRequest(BrokerType.IBKR)).isFalse();

        breaker.recordSuccess(BrokerType.IBKR);
        assertThat(breaker.allowRequest(BrokerType.IBKR)).isTrue();
    }
}
