package com.aiinvestment.broker.resilience;

import org.springframework.stereotype.Component;

import java.time.Duration;
import java.util.Optional;

@Component
public class ProviderRetryPolicy {
    private static final int MAX_READ_RETRIES = 2;

    public boolean shouldRetry(String operation, int attempt, Integer httpStatus) {
        if (operation != null && operation.toLowerCase().contains("auth")) {
            return false;
        }
        if (attempt >= MAX_READ_RETRIES) {
            return false;
        }
        return httpStatus == null || httpStatus == 408 || httpStatus == 429 || httpStatus >= 500;
    }

    public Duration backoff(int attempt, Optional<Duration> retryAfter) {
        return retryAfter.orElse(Duration.ofMillis(200L * (attempt + 1)));
    }
}
