package com.aiinvestment.portfolio.application;

import com.aiinvestment.portfolio.api.PortfolioPositionResponse;
import com.aiinvestment.portfolio.api.QuoteResponse;
import com.aiinvestment.portfolio.domain.PortfolioPosition;
import com.aiinvestment.shared.domain.AssetType;
import com.aiinvestment.shared.domain.Instrument;
import com.aiinvestment.shared.domain.Money;
import com.aiinvestment.shared.domain.market.Quote;
import com.aiinvestment.shared.domain.market.QuoteCache;
import com.aiinvestment.shared.domain.market.MarketDataFreshness;
import com.aiinvestment.shared.domain.market.MarketStatus;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;

import java.math.BigDecimal;
import java.time.Duration;
import java.time.Instant;
import java.util.List;
import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.*;

class ManualMarketPriceServiceTest {
    private final PortfolioService portfolios = mock(PortfolioService.class);
    private final StructuredMarketClient client = mock(StructuredMarketClient.class);
    private final QuoteCache cache = mock(QuoteCache.class);
    private final ManualMarketPriceService service = new ManualMarketPriceService(portfolios, client, cache, Duration.ofMinutes(5));
    private final UUID userId = UUID.randomUUID();
    private final UUID portfolioId = UUID.randomUUID();

    @Test
    void iciciRefreshUpdatesOnlySeparateMarketQuoteAndResponseValuation() {
        var position = manualPosition("ICICI_DIRECT", "ZENTEC", "1700", "1200", "100");
        when(portfolios.getPositions(userId, portfolioId)).thenReturn(List.of(position));
        when(client.fetch(position.instrument())).thenReturn(snapshot("ZENTEC.NS", "NSE", "INR", "1825.60"));

        var result = service.refresh(userId, portfolioId);

        assertThat(result.refreshed()).isEqualTo(1);
        var quoteCaptor = ArgumentCaptor.forClass(Quote.class);
        verify(cache).put(quoteCaptor.capture(), any(Duration.class));
        var response = PortfolioPositionResponse.from(position, QuoteResponse.from(quoteCaptor.getValue()));
        assertThat(response.quantity()).isEqualByComparingTo("100");
        assertThat(response.averageCost().amount()).isEqualByComparingTo("1200");
        assertThat(response.importedPrice().amount()).isEqualByComparingTo("1700");
        assertThat(response.currentPrice().amount()).isEqualByComparingTo("1825.60");
        assertThat(response.marketValue().amount()).isEqualByComparingTo("182560");
        assertThat(response.unrealizedProfitLoss().amount()).isEqualByComparingTo("62560");
    }

    @Test
    void hdfcUsesSamePriceContractWhileWrongCurrencyIsRejected() {
        var position = manualPosition("HDFC_SECURITIES", "ZENTEC", "1700", "1200", "80");
        when(portfolios.getPositions(userId, portfolioId)).thenReturn(List.of(position));
        when(client.fetch(position.instrument())).thenReturn(snapshot("ZENTEC.NS", "NSE", "USD", "1825.60"));
        var result = service.refresh(userId, portfolioId);
        assertThat(result.failed()).isEqualTo(1);
        verify(cache, never()).put(any(), any());
    }

    @Test
    void providerFailurePreservesLastKnownGoodCacheAndPositionState() {
        var position = manualPosition("ICICI_DIRECT", "ZENTEC", "1700", "1200", "100");
        when(portfolios.getPositions(userId, portfolioId)).thenReturn(List.of(position));
        when(client.fetch(position.instrument())).thenThrow(new IllegalStateException("STRUCTURED_PROVIDER_UNAVAILABLE"));
        var result = service.refresh(userId, portfolioId);
        assertThat(result.failed()).isEqualTo(1);
        verify(cache, never()).put(any(), any());
        verify(cache, never()).getStale(any());
        assertThat(position.currentPrice()).isNull();
        assertThat(position.importedPrice().amount()).isEqualByComparingTo("1700");
    }

    @Test
    void missingOrZeroLatestQuoteProjectsNullLiveValueAndDerivedProfitLoss() {
        var position = manualPosition("ICICI_DIRECT", "UNKNOWN_LIVE", "148.11", "155.72", "175");
        var zero = new Quote(position.instrument().instrumentId(), null, null,
                new Money(BigDecimal.ZERO, "INR"), new Money(new BigDecimal("147.58"), "INR"),
                "INR", Instant.now(), "HistoricalMock", MarketDataFreshness.STALE, MarketStatus.UNKNOWN);

        var response = PortfolioPositionResponse.from(position, QuoteResponse.from(zero));

        assertThat(response.importedPrice().amount()).isEqualByComparingTo("148.11");
        assertThat(response.currentPrice()).isNull();
        assertThat(response.marketValue()).isNull();
        assertThat(response.unrealizedProfitLoss()).isNull();
        assertThat(response.unrealizedProfitLossPercent()).isNull();
        assertThat(response.quote().previousClose().amount()).isEqualByComparingTo("147.58");
    }

    @Test
    void ibkrIsSkippedAndValidatedEtfUsesManualPublicPriceAuthority() {
        var ibkr = new PortfolioPosition(UUID.randomUUID(), portfolioId,
                instrument("BESI", AssetType.EQUITY, "NL", "EUR"), new BigDecimal("4"), money("80", "EUR"), money("100", "EUR"),
                money("400", "EUR"), money("320", "EUR"), money("80", "EUR"), new BigDecimal("25"),
                "ibkr", "IBKR", "BROKER_API", true, "REAL_BROKER", Instant.now(), null);
        var etf = new PortfolioPosition(UUID.randomUUID(), portfolioId,
                instrument("GOLDBEES", AssetType.ETF, "IN", "INR"), BigDecimal.ONE, money("50", "INR"), money("55", "INR"),
                money("55", "INR"), money("50", "INR"), money("5", "INR"), BigDecimal.TEN,
                "manual", "ICICI_DIRECT", "MANUAL_CSV_IMPORT", true, "IMPORTED_SNAPSHOT", Instant.now(), null);
        when(portfolios.getPositions(userId, portfolioId)).thenReturn(List.of(ibkr, etf));
        when(client.fetch(etf.instrument())).thenReturn(new StructuredMarketClient.Snapshot("GOLDBEES.NS", "NSI", "INR", "ETF",
                new BigDecimal("56"), "INR", null, Instant.now(), "Yahoo Finance", "STRUCTURED_PROVIDER_AVAILABLE"));
        var result = service.refresh(userId, portfolioId);
        assertThat(result.skipped()).isZero();
        assertThat(result.refreshed()).isEqualTo(1);
        verify(client).fetch(etf.instrument());
        verify(cache).put(any(), any());
    }

    private PortfolioPosition manualPosition(String broker, String ticker, String price, String cost, String quantity) {
        var instrument = instrument(ticker, AssetType.EQUITY, "IN", "INR");
        var q = new BigDecimal(quantity);
        var imported = money(price, "INR");
        var average = money(cost, "INR");
        return new PortfolioPosition(UUID.randomUUID(), portfolioId, instrument, q, average, null,
                null, average.multiply(q), null, null,
                "manual", broker, "MANUAL_CSV_IMPORT", true, "IMPORTED_SNAPSHOT", Instant.now(), null, imported);
    }

    private static Instrument instrument(String ticker, AssetType type, String country, String currency) {
        return new Instrument(UUID.randomUUID(), null, null, null, ticker, country.equals("IN") ? "NSE" : "AMS", null,
                ticker, type, country, currency, null, null);
    }

    private static Money money(String amount, String currency) { return new Money(new BigDecimal(amount), currency); }

    private static StructuredMarketClient.Snapshot snapshot(String ticker, String exchange, String currency, String price) {
        return new StructuredMarketClient.Snapshot(ticker, exchange, currency, "EQUITY", new BigDecimal(price), currency,
                Instant.parse("2026-08-30T10:00:00Z"), Instant.parse("2026-08-30T10:00:01Z"), "Yahoo Finance", "STRUCTURED_PROVIDER_PARTIAL");
    }
}
