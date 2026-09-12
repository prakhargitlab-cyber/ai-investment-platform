package com.aiinvestment.portfolio.application;

import com.aiinvestment.shared.domain.AssetType;
import com.aiinvestment.shared.domain.Money;
import com.aiinvestment.shared.domain.market.*;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

import java.time.Duration;
import java.time.Instant;
import java.util.List;
import java.util.UUID;

@Service
public class ManualMarketPriceService {
    private final PortfolioService portfolios;
    private final StructuredMarketClient client;
    private final QuoteCache cache;
    private final Duration ttl;

    public ManualMarketPriceService(PortfolioService portfolios, StructuredMarketClient client, QuoteCache cache,
            @Value("${market.quote-cache.ttl:PT5M}") Duration ttl) {
        this.portfolios = portfolios; this.client = client; this.cache = cache; this.ttl = ttl;
    }

    public RefreshResult refresh(UUID userId, UUID portfolioId) {
        var positions = portfolios.getPositions(userId, portfolioId).stream().filter(position -> position.active()
                && "MANUAL_CSV_IMPORT".equals(position.sourceType())).toList();
        int refreshed = 0, failed = 0, skipped = 0;
        for (var position : positions) {
            var instrument = position.instrument();
            if (!(instrument.assetType() == AssetType.EQUITY || instrument.assetType() == AssetType.ETF)
                    || !"IN".equalsIgnoreCase(instrument.country())
                    || !("ICICI_DIRECT".equals(position.brokerType()) || "HDFC_SECURITIES".equals(position.brokerType()))) {
                skipped++; continue;
            }
            try {
                var snapshot = client.fetch(instrument);
                validate(instrument.tradingCurrency(), snapshot);
                Quote quote = new Quote(instrument.instrumentId(), null, null,
                        new Money(snapshot.price(), snapshot.priceCurrency()), null, snapshot.priceCurrency(),
                        snapshot.retrievedAt(), snapshot.source(), MarketDataFreshness.DELAYED, MarketStatus.UNKNOWN,
                        snapshot.marketAsOf(), snapshot.retrievedAt());
                cache.put(quote, ttl); refreshed++;
            } catch (RuntimeException exception) {
                // Preserve the stale last-known-good cache entry; position/import state is never mutated.
                failed++;
            }
        }
        return new RefreshResult(portfolioId, refreshed, failed, skipped, Instant.now());
    }

    private static void validate(String expectedCurrency, StructuredMarketClient.Snapshot snapshot) {
        if (!"EQUITY".equalsIgnoreCase(snapshot.quoteType()) && !"STOCK".equalsIgnoreCase(snapshot.quoteType())
                && !"ETF".equalsIgnoreCase(snapshot.quoteType()))
            throw new IllegalArgumentException("QUOTE_TYPE_MISMATCH");
        if (expectedCurrency != null && !expectedCurrency.equalsIgnoreCase(snapshot.priceCurrency()))
            throw new IllegalArgumentException("QUOTE_CURRENCY_MISMATCH");
        String ticker = snapshot.providerTicker() == null ? "" : snapshot.providerTicker().toUpperCase();
        String exchange = snapshot.exchange() == null ? "" : snapshot.exchange().toUpperCase();
        if (!(ticker.endsWith(".NS") || ticker.endsWith(".BO") || exchange.contains("NSE") || exchange.contains("BSE")))
            throw new IllegalArgumentException("QUOTE_EXCHANGE_MISMATCH");
    }

    public record RefreshResult(UUID portfolioId, int refreshed, int failed, int skipped, Instant completedAt) {}
}
