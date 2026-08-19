package com.aiinvestment.portfolio.infrastructure.market;

import com.aiinvestment.shared.domain.market.Quote;
import com.aiinvestment.shared.domain.market.QuoteCache;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.stereotype.Component;

import java.time.Duration;
import java.time.Instant;
import java.util.Map;
import java.util.Optional;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;

@Component
@ConditionalOnProperty(name = "market.quote-cache.mode", havingValue = "memory", matchIfMissing = true)
public class InMemoryQuoteCache implements QuoteCache {
    private final Map<UUID, CachedQuote> cache = new ConcurrentHashMap<>();

    @Override
    public Optional<Quote> get(UUID instrumentId) {
        CachedQuote cached = cache.get(instrumentId);
        if (cached == null || cached.expiresAt().isBefore(Instant.now())) {
            return Optional.empty();
        }
        return Optional.of(cached.quote());
    }

    @Override
    public Optional<Quote> getStale(UUID instrumentId) {
        CachedQuote cached = cache.get(instrumentId);
        return cached == null ? Optional.empty() : Optional.of(cached.quote());
    }

    @Override
    public void put(Quote quote, Duration ttl) {
        cache.put(quote.instrumentId(), new CachedQuote(quote, Instant.now().plus(ttl)));
    }

    private record CachedQuote(Quote quote, Instant expiresAt) {
    }
}
