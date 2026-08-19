package com.aiinvestment.portfolio.infrastructure.market;

import com.aiinvestment.shared.domain.Money;
import com.aiinvestment.shared.domain.market.MarketDataFreshness;
import com.aiinvestment.shared.domain.market.MarketStatus;
import com.aiinvestment.shared.domain.market.Quote;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.data.redis.core.ValueOperations;

import java.math.BigDecimal;
import java.time.Duration;
import java.time.Instant;
import java.util.Optional;
import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

class RedisQuoteCacheTest {
    @Test
    void writesFreshAndStaleKeysAndReadsStaleAsStale() throws Exception {
        StringRedisTemplate redis = mock(StringRedisTemplate.class);
        @SuppressWarnings("unchecked")
        ValueOperations<String, String> values = mock(ValueOperations.class);
        ObjectMapper objectMapper = new ObjectMapper().findAndRegisterModules();
        RedisQuoteCache cache = new RedisQuoteCache(redis, objectMapper);
        Quote quote = quote();
        String json = objectMapper.writeValueAsString(quote);

        when(redis.opsForValue()).thenReturn(values);
        when(values.get("market:quote:" + quote.instrumentId())).thenReturn(null);
        when(values.get("market:quote:stale:" + quote.instrumentId())).thenReturn(json);

        cache.put(quote, Duration.ofMinutes(5));
        Optional<Quote> stale = cache.getStale(quote.instrumentId());

        verify(values).set(eq("market:quote:" + quote.instrumentId()), anyString(), eq(Duration.ofMinutes(5)));
        verify(values).set(eq("market:quote:stale:" + quote.instrumentId()), anyString(), eq(Duration.ofDays(1)));
        assertThat(stale).hasValueSatisfying(value -> {
            assertThat(value.freshness()).isEqualTo(MarketDataFreshness.STALE);
            assertThat(value.last().amount()).isEqualByComparingTo("42.00");
        });
    }

    private static Quote quote() {
        UUID instrumentId = UUID.randomUUID();
        Instant sourceTimestamp = Instant.parse("2026-01-01T10:00:00Z");
        Instant receivedAt = Instant.parse("2026-01-01T10:00:01Z");
        return new Quote(instrumentId, null, null, new Money(new BigDecimal("42.00"), "USD"),
                null, "USD", sourceTimestamp, "TestSource", MarketDataFreshness.DELAYED,
                MarketStatus.UNKNOWN, sourceTimestamp, receivedAt);
    }
}
