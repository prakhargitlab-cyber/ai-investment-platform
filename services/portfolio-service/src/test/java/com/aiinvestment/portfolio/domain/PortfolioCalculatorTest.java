package com.aiinvestment.portfolio.domain;

import com.aiinvestment.shared.domain.AssetType;
import com.aiinvestment.shared.domain.Instrument;
import com.aiinvestment.shared.domain.Money;
import com.aiinvestment.shared.domain.broker.BrokerCashBalance;
import org.junit.jupiter.api.Test;

import java.math.BigDecimal;
import java.time.Instant;
import java.util.List;
import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

class PortfolioCalculatorTest {
    private final MockFxRateProvider fx = new MockFxRateProvider();
    private final PortfolioCalculator calculator = new PortfolioCalculator(fx);

    @Test
    void sameCurrencyFxRateIsOne() {
        assertThat(fx.getRate("EUR", "EUR")).isEqualByComparingTo(BigDecimal.ONE);
    }

    @Test
    void moneyRequiresIsoStyleCurrency() {
        assertThatThrownBy(() -> new Money(BigDecimal.ONE, "EURO"))
                .isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void positionCalculatesMarketValueCostBasisAndProfitLoss() {
        PortfolioPosition position = position("NVDA", "USD", "8", "500.00", "920.00", "US", "Technology", "MOCK_EU");

        assertThat(position.marketValue().amount()).isEqualByComparingTo("7360.0000");
        assertThat(position.costBasis().amount()).isEqualByComparingTo("4000.0000");
        assertThat(position.unrealizedProfitLoss().amount()).isEqualByComparingTo("3360.0000");
        assertThat(position.unrealizedProfitLossPercent()).isEqualByComparingTo("84.0000");
    }

    @Test
    void zeroCostBasisProfitLossPercentIsZero() {
        PortfolioPosition position = position("BESI", "EUR", "10", "0.00", "12.00", "NL", "Technology", "MOCK_EU");

        assertThat(position.unrealizedProfitLossPercent()).isEqualByComparingTo(BigDecimal.ZERO);
    }

    @Test
    void negativeQuantityIsRejected() {
        assertThatThrownBy(() -> position("BESI", "EUR", "-1", "10.00", "11.00", "NL", "Technology", "MOCK_EU"))
                .isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void summaryNormalizesMultiCurrencyValuesAndAllocations() {
        UUID portfolioId = UUID.randomUUID();
        Portfolio portfolio = new Portfolio(portfolioId, UUID.randomUUID(), "Global", "EUR", Instant.now(), Instant.now());
        List<PortfolioPosition> positions = List.of(
                position(portfolioId, "BESI", "EUR", "10", "100.00", "110.00", "NL", "Technology", "MOCK_EU"),
                position(portfolioId, "NVDA", "USD", "2", "500.00", "1000.00", "US", "Technology", "MOCK_EU"),
                position(portfolioId, "RELIANCE", "INR", "5", "1000.00", "1200.00", "IN", "Energy", "MOCK_INDIA")
        );

        PortfolioSummary summary = calculator.summarize(portfolio, positions,
                List.of(new BrokerCashBalance("MOCK_EU", new Money(new BigDecimal("10.00"), "EUR"))));

        assertThat(summary.totalMarketValue().currency()).isEqualTo("EUR");
        assertThat(summary.totalMarketValue().amount()).isEqualByComparingTo("3006.0000");
        assertThat(summary.totalCostBasis().amount()).isEqualByComparingTo("1975.0000");
        assertThat(summary.numberOfPositions()).isEqualTo(3);
        assertThat(summary.allocation().country()).containsKeys("NL", "US", "IN");
        assertThat(summary.allocation().broker()).containsKeys("MOCK_EU", "MOCK_INDIA");
    }

    private static PortfolioPosition position(String ticker, String currency, String quantity, String averageCost,
                                              String currentPrice, String country, String sector, String broker) {
        return position(UUID.randomUUID(), ticker, currency, quantity, averageCost, currentPrice, country, sector, broker);
    }

    private static PortfolioPosition position(UUID portfolioId, String ticker, String currency, String quantity,
                                              String averageCost, String currentPrice, String country, String sector, String broker) {
        Instrument instrument = new Instrument(UUID.randomUUID(), ticker + "ISIN", ticker, "XNAS", "XNAS",
                ticker + " Corp", AssetType.EQUITY, country, currency, sector, "Industry");
        return PortfolioPosition.priced(UUID.randomUUID(), portfolioId, instrument, new BigDecimal(quantity),
                new Money(new BigDecimal(averageCost), currency), new Money(new BigDecimal(currentPrice), currency),
                broker, Instant.now());
    }
}
