package com.aiinvestment.portfolio.infrastructure.market;

import com.aiinvestment.shared.domain.AssetType;
import com.aiinvestment.shared.domain.Instrument;
import com.aiinvestment.shared.domain.Money;
import com.aiinvestment.shared.domain.market.MarketDataFreshness;
import com.aiinvestment.shared.domain.market.MarketStatus;
import com.aiinvestment.shared.domain.market.Quote;
import org.junit.jupiter.api.Test;

import java.math.BigDecimal;
import java.time.Duration;
import java.time.Instant;
import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;

class FallbackMarketDataProviderTest {
    @Test
    void fallsBackToDemoOnlyWhenDemoModeIsEnabled() {
        InMemoryQuoteCache cache = new InMemoryQuoteCache();
        FallbackMarketDataProvider provider = new FallbackMarketDataProvider(cache, new MockMarketDataProvider(cache), true);

        Quote quote = provider.getQuote(instrument());

        assertThat(quote.freshness()).isEqualTo(MarketDataFreshness.MOCK);
        assertThat(quote.source()).isEqualTo("MockMarketDataProvider");
    }

    @Test
    void returnsUnavailableInsteadOfInventingPriceWhenDemoModeIsDisabled() {
        InMemoryQuoteCache cache = new InMemoryQuoteCache();
        FallbackMarketDataProvider provider = new FallbackMarketDataProvider(cache, new MockMarketDataProvider(cache), false);

        Quote quote = provider.getQuote(instrument());

        assertThat(quote.freshness()).isEqualTo(MarketDataFreshness.UNAVAILABLE);
        assertThat(quote.last()).isNull();
    }

    @Test
    void ignoresCachedMockQuoteWhenDemoModeIsDisabled() {
        InMemoryQuoteCache cache = new InMemoryQuoteCache();
        Instrument instrument = instrument();
        cache.put(new MockMarketDataProvider(cache).getQuote(instrument), Duration.ofMinutes(5));
        FallbackMarketDataProvider provider = new FallbackMarketDataProvider(cache, new MockMarketDataProvider(cache), false);

        Quote quote = provider.getQuote(instrument);

        assertThat(quote.freshness()).isEqualTo(MarketDataFreshness.UNAVAILABLE);
        assertThat(quote.last()).isNull();
    }

    @Test
    void cacheFailureFallsBackWithoutFailingQuoteRequest() {
        com.aiinvestment.shared.domain.market.QuoteCache failingCache = new com.aiinvestment.shared.domain.market.QuoteCache() {
            @Override
            public java.util.Optional<Quote> get(UUID instrumentId) {
                throw new IllegalStateException("redis unavailable");
            }

            @Override
            public java.util.Optional<Quote> getStale(UUID instrumentId) {
                throw new IllegalStateException("redis unavailable");
            }

            @Override
            public void put(Quote quote, Duration ttl) {
                throw new IllegalStateException("redis unavailable");
            }
        };
        FallbackMarketDataProvider provider = new FallbackMarketDataProvider(failingCache, new MockMarketDataProvider(failingCache), false);

        Quote quote = provider.getQuote(instrument());

        assertThat(quote.freshness()).isEqualTo(MarketDataFreshness.UNAVAILABLE);
    }

    @Test
    void exposesExpiredCacheAsStaleBeforeUsingDemoFallback() {
        InMemoryQuoteCache cache = new InMemoryQuoteCache();
        Instrument instrument = instrument();
        cache.put(new Quote(instrument.instrumentId(), null, null, new Money(new BigDecimal("10.00"), "USD"),
                null, "USD", Instant.now().minusSeconds(60), "TestCache", MarketDataFreshness.DELAYED, MarketStatus.UNKNOWN),
                Duration.ofMillis(-1));
        FallbackMarketDataProvider provider = new FallbackMarketDataProvider(cache, new MockMarketDataProvider(cache), true);

        Quote quote = provider.getQuote(instrument);

        assertThat(quote.freshness()).isEqualTo(MarketDataFreshness.STALE);
        assertThat(quote.source()).isEqualTo("TestCache");
    }

    @Test
    void rejectsHistoricalCachedZeroInsteadOfTreatingItAsLatestPrice() {
        InMemoryQuoteCache cache = new InMemoryQuoteCache();
        Instrument instrument = instrument();
        cache.put(new Quote(instrument.instrumentId(), null, null, new Money(BigDecimal.ZERO, "USD"),
                new Money(new BigDecimal("147.58"), "USD"), "USD", Instant.now(), "OldMock",
                MarketDataFreshness.MOCK, MarketStatus.UNKNOWN), Duration.ofMinutes(5));
        FallbackMarketDataProvider provider = new FallbackMarketDataProvider(
                cache, new MockMarketDataProvider(cache), false);

        Quote quote = provider.getQuote(instrument);

        assertThat(quote.freshness()).isEqualTo(MarketDataFreshness.UNAVAILABLE);
        assertThat(quote.last()).isNull();
    }

    private static Instrument instrument() {
        return new Instrument(UUID.randomUUID(), "US67066G1040", "NVDA", "XNAS", "XNAS",
                "NVIDIA Corporation", AssetType.EQUITY, "US", "USD", "Technology", "Semiconductors");
    }
}
