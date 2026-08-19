package com.aiinvestment.portfolio.infrastructure.market;

import com.aiinvestment.shared.domain.market.Quote;
import com.aiinvestment.shared.domain.market.QuoteCache;
import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.stereotype.Component;

import java.time.Duration;
import java.util.Optional;
import java.util.UUID;

@Component
@ConditionalOnProperty(name = "market.quote-cache.mode", havingValue = "redis")
public class RedisQuoteCache implements QuoteCache {
    private final StringRedisTemplate redisTemplate;
    private final ObjectMapper objectMapper;

    public RedisQuoteCache(StringRedisTemplate redisTemplate, ObjectMapper objectMapper) {
        this.redisTemplate = redisTemplate;
        this.objectMapper = objectMapper;
    }

    @Override
    public Optional<Quote> get(UUID instrumentId) {
        String json = redisTemplate.opsForValue().get(freshKey(instrumentId));
        if (json == null) {
            return Optional.empty();
        }
        return deserialize(instrumentId, json, false);
    }

    @Override
    public Optional<Quote> getStale(UUID instrumentId) {
        String json = redisTemplate.opsForValue().get(staleKey(instrumentId));
        if (json == null) {
            return Optional.empty();
        }
        return deserialize(instrumentId, json, true);
    }

    @Override
    public void put(Quote quote, Duration ttl) {
        try {
            String json = objectMapper.writeValueAsString(quote);
            redisTemplate.opsForValue().set(freshKey(quote.instrumentId()), json, ttl);
            redisTemplate.opsForValue().set(staleKey(quote.instrumentId()), json, staleTtl(ttl));
        } catch (JsonProcessingException exception) {
            throw new IllegalStateException("Quote cache serialization failed", exception);
        }
    }

    private Optional<Quote> deserialize(UUID instrumentId, String json, boolean stale) {
        try {
            Quote quote = objectMapper.readValue(json, Quote.class);
            if (!stale) {
                return Optional.of(quote);
            }
            return Optional.of(new Quote(quote.instrumentId(), quote.bid(), quote.ask(), quote.last(), quote.previousClose(),
                    quote.currency(), quote.timestamp(), quote.source(), com.aiinvestment.shared.domain.market.MarketDataFreshness.STALE,
                    quote.marketStatus(), quote.sourceTimestamp(), quote.receivedAt()));
        } catch (JsonProcessingException exception) {
            redisTemplate.delete(stale ? staleKey(instrumentId) : freshKey(instrumentId));
            return Optional.empty();
        }
    }

    private static Duration staleTtl(Duration ttl) {
        Duration minimum = Duration.ofDays(1);
        if (ttl == null || ttl.isNegative() || ttl.isZero()) {
            return minimum;
        }
        Duration expanded = ttl.multipliedBy(12);
        return expanded.compareTo(minimum) > 0 ? expanded : minimum;
    }

    private static String freshKey(UUID instrumentId) {
        return "market:quote:" + instrumentId;
    }

    private static String staleKey(UUID instrumentId) {
        return "market:quote:stale:" + instrumentId;
    }
}
