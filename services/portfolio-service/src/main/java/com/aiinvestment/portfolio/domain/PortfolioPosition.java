package com.aiinvestment.portfolio.domain;

import com.aiinvestment.shared.domain.Instrument;
import com.aiinvestment.shared.domain.Money;

import java.math.BigDecimal;
import java.math.RoundingMode;
import java.time.Instant;
import java.util.Objects;
import java.util.UUID;

public record PortfolioPosition(
        UUID positionId,
        UUID portfolioId,
        Instrument instrument,
        BigDecimal quantity,
        Money averageCost,
        Money currentPrice,
        Money marketValue,
        Money costBasis,
        Money unrealizedProfitLoss,
        BigDecimal unrealizedProfitLossPercent,
        String brokerAccountId,
        Instant lastUpdated
) {
    public PortfolioPosition {
        Objects.requireNonNull(positionId, "positionId is required");
        Objects.requireNonNull(portfolioId, "portfolioId is required");
        Objects.requireNonNull(instrument, "instrument is required");
        Objects.requireNonNull(quantity, "quantity is required");
        if (quantity.signum() < 0) {
            throw new IllegalArgumentException("quantity cannot be negative");
        }
        Objects.requireNonNull(averageCost, "averageCost is required");
        Objects.requireNonNull(currentPrice, "currentPrice is required");
        Objects.requireNonNull(marketValue, "marketValue is required");
        Objects.requireNonNull(costBasis, "costBasis is required");
        Objects.requireNonNull(unrealizedProfitLoss, "unrealizedProfitLoss is required");
        Objects.requireNonNull(unrealizedProfitLossPercent, "unrealizedProfitLossPercent is required");
        if (brokerAccountId == null || brokerAccountId.isBlank()) {
            throw new IllegalArgumentException("brokerAccountId is required");
        }
        Objects.requireNonNull(lastUpdated, "lastUpdated is required");
    }

    public static PortfolioPosition priced(
            UUID positionId,
            UUID portfolioId,
            Instrument instrument,
            BigDecimal quantity,
            Money averageCost,
            Money currentPrice,
            String brokerAccountId,
            Instant lastUpdated
    ) {
        Money marketValue = currentPrice.multiply(quantity);
        Money costBasis = averageCost.multiply(quantity);
        Money profitLoss = marketValue.subtract(costBasis);
        BigDecimal profitLossPercent = BigDecimal.ZERO;
        if (costBasis.amount().compareTo(BigDecimal.ZERO) != 0) {
            profitLossPercent = profitLoss.amount()
                    .divide(costBasis.amount(), 8, RoundingMode.HALF_UP)
                    .multiply(BigDecimal.valueOf(100))
                    .setScale(4, RoundingMode.HALF_UP);
        }
        return new PortfolioPosition(positionId, portfolioId, instrument, quantity, averageCost, currentPrice,
                marketValue, costBasis, profitLoss, profitLossPercent, brokerAccountId, lastUpdated);
    }
}
