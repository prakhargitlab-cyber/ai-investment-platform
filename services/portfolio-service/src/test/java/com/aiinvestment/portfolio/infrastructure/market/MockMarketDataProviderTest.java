package com.aiinvestment.portfolio.infrastructure.market;

import com.aiinvestment.shared.domain.AssetType;
import com.aiinvestment.shared.domain.Instrument;
import com.aiinvestment.shared.domain.market.MarketDataFreshness;
import org.junit.jupiter.api.Test;

import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;

class MockMarketDataProviderTest {
    @Test
    void returnsMockFreshnessAndCachesQuote() {
        InMemoryQuoteCache cache = new InMemoryQuoteCache();
        MockMarketDataProvider provider = new MockMarketDataProvider(cache);
        Instrument instrument = new Instrument(UUID.randomUUID(), "US67066G1040", "NVDA", "XNAS", "XNAS",
                "NVIDIA Corporation", AssetType.EQUITY, "US", "USD", "Technology", "Semiconductors");

        var quote = provider.getQuote(instrument);

        assertThat(quote.freshness()).isEqualTo(MarketDataFreshness.MOCK);
        assertThat(quote.last().amount()).isPositive();
        assertThat(cache.get(instrument.instrumentId())).contains(quote);
        assertThat(provider.getMarketDataStatus(instrument).source()).isEqualTo("MockMarketDataProvider");
    }
}
