package com.aiinvestment.portfolio.infrastructure.market;

import com.aiinvestment.shared.domain.Instrument;
import com.aiinvestment.shared.domain.Money;
import com.aiinvestment.shared.domain.market.*;
import org.springframework.stereotype.Component;

import java.math.BigDecimal;
import java.time.Duration;
import java.time.Instant;
import java.util.List;

@Component
public class MockMarketDataProvider implements MarketDataProvider {
    private static final Duration MOCK_TTL = Duration.ofMinutes(5);

    private final QuoteCache quoteCache;

    public MockMarketDataProvider(QuoteCache quoteCache) {
        this.quoteCache = quoteCache;
    }

    @Override
    public Quote getQuote(Instrument instrument) {
        return quoteCache.get(instrument.instrumentId()).orElseGet(() -> {
            Quote quote = buildMockQuote(instrument);
            try {
                quoteCache.put(quote, MOCK_TTL);
            } catch (RuntimeException exception) {
                // Quote caching is an infrastructure optimization; demo quote generation must remain usable without it.
            }
            return quote;
        });
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

    private static Quote buildMockQuote(Instrument instrument) {
        Instant timestamp = Instant.parse("2026-01-01T00:00:00Z");
        BigDecimal last = switch (instrument.ticker()) {
            case "BESI" -> new BigDecimal("132.50");
            case "AIXA" -> new BigDecimal("24.10");
            case "NVDA" -> new BigDecimal("920.00");
            case "RELIANCE" -> new BigDecimal("2850.00");
            case "ZENTEC" -> new BigDecimal("1025.00");
            default -> BigDecimal.ZERO;
        };
        String currency = instrument.tradingCurrency();
        return new Quote(instrument.instrumentId(), new Money(last.subtract(new BigDecimal("0.10")), currency),
                new Money(last.add(new BigDecimal("0.10")), currency), new Money(last, currency),
                new Money(last.multiply(new BigDecimal("0.98")), currency), currency, timestamp,
                "MockMarketDataProvider", MarketDataFreshness.MOCK, MarketStatus.UNKNOWN);
    }
}
