package com.aiinvestment.shared.domain.market;

import java.time.Duration;
import java.util.Optional;
import java.util.UUID;

public interface QuoteCache {
    Optional<Quote> get(UUID instrumentId);

    default Optional<Quote> getStale(UUID instrumentId) {
        return Optional.empty();
    }

    void put(Quote quote, Duration ttl);
}
