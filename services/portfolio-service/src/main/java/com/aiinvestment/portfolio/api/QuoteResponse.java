package com.aiinvestment.portfolio.api;

import com.aiinvestment.shared.domain.market.Quote;

import java.time.Instant;

public record QuoteResponse(
        MoneyResponse bid,
        MoneyResponse ask,
        MoneyResponse last,
        MoneyResponse previousClose,
        String currency,
        Instant timestamp,
        String source,
        String freshness,
        String marketStatus,
        Instant sourceTimestamp,
        Instant receivedAt
) {
    public static QuoteResponse from(Quote quote) {
        return new QuoteResponse(MoneyResponse.from(quote.bid()), MoneyResponse.from(quote.ask()), MoneyResponse.from(quote.last()),
                MoneyResponse.from(quote.previousClose()), quote.currency(), quote.timestamp(), quote.source(),
                quote.freshness().name(), quote.marketStatus().name(), quote.sourceTimestamp(), quote.receivedAt());
    }
}
