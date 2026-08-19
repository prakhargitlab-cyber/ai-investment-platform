package com.aiinvestment.broker.resilience;

import org.junit.jupiter.api.Test;

import java.time.Duration;
import java.util.Optional;

import static org.assertj.core.api.Assertions.assertThat;

class ProviderRetryPolicyTest {
    @Test
    void retriesOnlyBoundedTransientReadFailuresAndHonorsRetryAfter() {
        ProviderRetryPolicy policy = new ProviderRetryPolicy();

        assertThat(policy.shouldRetry("fetchPositions", 0, 429)).isTrue();
        assertThat(policy.shouldRetry("fetchPositions", 2, 429)).isFalse();
        assertThat(policy.shouldRetry("authenticate", 0, 500)).isFalse();
        assertThat(policy.backoff(0, Optional.of(Duration.ofSeconds(3)))).isEqualTo(Duration.ofSeconds(3));
    }
}
