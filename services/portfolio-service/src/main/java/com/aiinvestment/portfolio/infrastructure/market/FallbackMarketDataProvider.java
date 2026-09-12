package com.aiinvestment.portfolio.infrastructure.market;

import com.aiinvestment.shared.domain.Instrument;
import com.aiinvestment.shared.domain.market.*;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Primary;
import org.springframework.stereotype.Component;

import java.time.Instant;
import java.util.List;

@Primary
@Component
public class FallbackMarketDataProvider implements MarketDataProvider {
    private final QuoteCache quoteCache;
    private final MockMarketDataProvider mockMarketDataProvider;
    private final boolean demoMode;

    public FallbackMarketDataProvider(QuoteCache quoteCache,
                                      MockMarketDataProvider mockMarketDataProvider,
                                      @Value("${market.demo-mode:false}") boolean demoMode) {
        this.quoteCache = quoteCache;
        this.mockMarketDataProvider = mockMarketDataProvider;
        this.demoMode = demoMode;
    }

    @Override
    public Quote getQuote(Instrument instrument) {
        return safeGet(instrument, false)
                .or(() -> safeGet(instrument, true).map(FallbackMarketDataProvider::markStale))
                .orElseGet(() -> demoMode ? mockMarketDataProvider.getQuote(instrument) : unavailableQuote(instrument));
    }

    @Override
    public List<Quote> getQuotes(List<Instrument> instruments) {
        return instruments.stream().map(this::getQuote).toList();
    }

    @Override
    public MarketDataStatus getMarketDataStatus(Instrument instrument) {
        Quote quote = getQuote(instrument);
        return new MarketDataStatus(instrument.instrumentId(), quote.source(), quote.freshness(), quote.marketStatus(), quote.timestamp());
    }

    private static Quote markStale(Quote quote) {
        return new Quote(quote.instrumentId(), quote.bid(), quote.ask(), quote.last(), quote.previousClose(), quote.currency(),
                quote.timestamp(), quote.source(), MarketDataFreshness.STALE, quote.marketStatus(), quote.sourceTimestamp(), quote.receivedAt());
    }

    private java.util.Optional<Quote> safeGet(Instrument instrument, boolean stale) {
        try {
            java.util.Optional<Quote> quote = stale ? quoteCache.getStale(instrument.instrumentId()) : quoteCache.get(instrument.instrumentId());
            return quote.filter(this::isAllowedInCurrentMode).filter(FallbackMarketDataProvider::hasNoFabricatedZeroPrice);
        } catch (RuntimeException exception) {
            return java.util.Optional.empty();
        }
    }

    private boolean isAllowedInCurrentMode(Quote quote) {
        return demoMode || quote.freshness() != MarketDataFreshness.MOCK;
    }

    private static boolean hasNoFabricatedZeroPrice(Quote quote) {
        return quote.last() == null || quote.last().amount().signum() > 0;
    }

    private static Quote unavailableQuote(Instrument instrument) {
        return new Quote(instrument.instrumentId(), null, null, null, null, instrument.tradingCurrency(), Instant.now(),
                "NoVerifiedMarketDataProvider", MarketDataFreshness.UNAVAILABLE, MarketStatus.UNKNOWN);
    }
}
