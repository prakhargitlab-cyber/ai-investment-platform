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
        String brokerType,
        String sourceType,
        Boolean active,
        String dataFreshness,
        Instant lastUpdated,
        String customDisplayName,
        Money importedPrice
) {
    public PortfolioPosition(
            UUID positionId, UUID portfolioId, Instrument instrument, BigDecimal quantity,
            Money averageCost, Money currentPrice, Money marketValue, Money costBasis,
            Money unrealizedProfitLoss, BigDecimal unrealizedProfitLossPercent,
            String brokerAccountId, String brokerType, String sourceType, Boolean active,
            String dataFreshness, Instant lastUpdated, String customDisplayName
    ) {
        this(positionId, portfolioId, instrument, quantity, averageCost, currentPrice, marketValue,
                costBasis, unrealizedProfitLoss, unrealizedProfitLossPercent, brokerAccountId,
                brokerType, sourceType, active, dataFreshness, lastUpdated, customDisplayName, null);
    }

    public PortfolioPosition {
        Objects.requireNonNull(positionId, "positionId is required");
        Objects.requireNonNull(portfolioId, "portfolioId is required");
        Objects.requireNonNull(instrument, "instrument is required");
        Objects.requireNonNull(quantity, "quantity is required");
        if (quantity.signum() < 0) {
            throw new IllegalArgumentException("quantity cannot be negative");
        }
        Objects.requireNonNull(averageCost, "averageCost is required");
        Objects.requireNonNull(costBasis, "costBasis is required");
        if (brokerAccountId == null || brokerAccountId.isBlank()) {
            throw new IllegalArgumentException("brokerAccountId is required");
        }
        if (brokerType == null || brokerType.isBlank()) {
            throw new IllegalArgumentException("brokerType is required");
        }
        if (sourceType == null || sourceType.isBlank()) {
            sourceType = "BROKER";
        }
        if (active == null) {
            active = true;
        }
        if (dataFreshness == null || dataFreshness.isBlank()) {
            throw new IllegalArgumentException("dataFreshness is required");
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
        return priced(positionId, portfolioId, instrument, quantity, averageCost, currentPrice, brokerAccountId,
                "MOCK", "DEMO", lastUpdated);
    }

    public static PortfolioPosition priced(
            UUID positionId,
            UUID portfolioId,
            Instrument instrument,
            BigDecimal quantity,
            Money averageCost,
            Money currentPrice,
            String brokerAccountId,
            String brokerType,
            String dataFreshness,
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
                marketValue, costBasis, profitLoss, profitLossPercent, brokerAccountId, brokerType, "BROKER", true,
                dataFreshness, lastUpdated, null, null);
    }

    public static PortfolioPosition brokerReported(
            UUID positionId,
            UUID portfolioId,
            Instrument instrument,
            BigDecimal quantity,
            Money averageCost,
            Money currentPrice,
            Money brokerMarketValue,
            Money brokerUnrealizedProfitLoss,
            String brokerAccountId,
            String brokerType,
            String dataFreshness,
            Instant lastUpdated
    ) {
        Money costBasis = averageCost.multiply(quantity);
        Money marketValue = brokerMarketValue == null ? currentPrice.multiply(quantity) : brokerMarketValue;
        Money profitLoss = brokerUnrealizedProfitLoss == null ? marketValue.subtract(costBasis) : brokerUnrealizedProfitLoss;
        BigDecimal profitLossPercent = BigDecimal.ZERO;
        if (costBasis.amount().compareTo(BigDecimal.ZERO) != 0) {
            profitLossPercent = profitLoss.amount()
                    .divide(costBasis.amount(), 8, RoundingMode.HALF_UP)
                    .multiply(BigDecimal.valueOf(100))
                    .setScale(4, RoundingMode.HALF_UP);
        }
        return new PortfolioPosition(positionId, portfolioId, instrument, quantity, averageCost, currentPrice,
                marketValue, costBasis, profitLoss, profitLossPercent, brokerAccountId, brokerType, "BROKER", true,
                dataFreshness, lastUpdated, null, null);
    }

    public static PortfolioPosition brokerReported(
            UUID positionId,
            UUID portfolioId,
            Instrument instrument,
            BigDecimal quantity,
            Money averageCost,
            Money currentPrice,
            Money brokerMarketValue,
            Money brokerUnrealizedProfitLoss,
            String brokerAccountId,
            String brokerType,
            String sourceType,
            boolean active,
            String dataFreshness,
            Instant lastUpdated,
            String customDisplayName
    ) {
        return brokerReported(positionId, portfolioId, instrument, quantity, averageCost, currentPrice,
                brokerMarketValue, brokerUnrealizedProfitLoss, brokerAccountId, brokerType, sourceType,
                active, dataFreshness, lastUpdated, customDisplayName, null);
    }

    public static PortfolioPosition brokerReported(
            UUID positionId,
            UUID portfolioId,
            Instrument instrument,
            BigDecimal quantity,
            Money averageCost,
            Money currentPrice,
            Money brokerMarketValue,
            Money brokerUnrealizedProfitLoss,
            String brokerAccountId,
            String brokerType,
            String sourceType,
            boolean active,
            String dataFreshness,
            Instant lastUpdated,
            String customDisplayName,
            Money importedPrice
    ) {
        Money costBasis = averageCost.multiply(quantity);
        Money marketValue = brokerMarketValue == null
                ? currentPrice == null ? null : currentPrice.multiply(quantity)
                : brokerMarketValue;
        Money profitLoss = brokerUnrealizedProfitLoss == null
                ? marketValue == null ? null : marketValue.subtract(costBasis)
                : brokerUnrealizedProfitLoss;
        BigDecimal profitLossPercent = profitLoss == null ? null : BigDecimal.ZERO;
        if (profitLoss != null && costBasis.amount().compareTo(BigDecimal.ZERO) != 0) {
            profitLossPercent = profitLoss.amount()
                    .divide(costBasis.amount(), 8, RoundingMode.HALF_UP)
                    .multiply(BigDecimal.valueOf(100))
                    .setScale(4, RoundingMode.HALF_UP);
        }
        return new PortfolioPosition(positionId, portfolioId, instrument, quantity, averageCost, currentPrice,
                marketValue, costBasis, profitLoss, profitLossPercent, brokerAccountId, brokerType, sourceType, active,
                dataFreshness, lastUpdated, customDisplayName, importedPrice);
    }

    public String displayName() {
        if (customDisplayName != null && !customDisplayName.isBlank()) return customDisplayName;
        if (instrument.brokerDescription() != null && !instrument.brokerDescription().isBlank()) return instrument.brokerDescription();
        if (instrument.companyName() != null && !instrument.companyName().isBlank()
                && !instrument.companyName().equalsIgnoreCase(instrument.ticker())
                && (instrument.brokerSymbol() == null
                    || !instrument.companyName().equalsIgnoreCase(instrument.brokerSymbol()))) return instrument.companyName();
        if (instrument.canonicalName() != null && !instrument.canonicalName().isBlank()) return instrument.canonicalName();
        if (instrument.brokerSymbol() != null && !instrument.brokerSymbol().isBlank()) return instrument.brokerSymbol();
        return instrument.ticker();
    }
}
