package com.aiinvestment.portfolio.api;

import com.aiinvestment.portfolio.domain.PortfolioPosition;

import java.math.BigDecimal;
import java.time.Instant;
import java.util.UUID;

public record PortfolioPositionResponse(
        UUID positionId,
        UUID portfolioId,
        InstrumentResponse instrument,
        BigDecimal quantity,
        MoneyResponse averageCost,
        MoneyResponse currentPrice,
        MoneyResponse importedPrice,
        MoneyResponse marketValue,
        MoneyResponse costBasis,
        MoneyResponse unrealizedProfitLoss,
        BigDecimal unrealizedProfitLossPercent,
        String brokerType,
        String sourceType,
        boolean active,
        String dataFreshness,
        Instant lastUpdated,
        QuoteResponse quote,
        String displayName,
        String customDisplayName
) {
    public static PortfolioPositionResponse from(PortfolioPosition position) {
        if ("MANUAL_CSV_IMPORT".equals(position.sourceType())) {
            return unpricedManualPosition(position, null);
        }
        return new PortfolioPositionResponse(position.positionId(), position.portfolioId(), InstrumentResponse.from(position.instrument()),
                position.quantity(), MoneyResponse.from(position.averageCost()), MoneyResponse.from(position.currentPrice()),
                MoneyResponse.from(position.importedPrice()),
                MoneyResponse.from(position.marketValue()), MoneyResponse.from(position.costBasis()),
                MoneyResponse.from(position.unrealizedProfitLoss()), position.unrealizedProfitLossPercent(),
                position.brokerType(), position.sourceType(), position.active(),
                position.dataFreshness(), position.lastUpdated(), null, position.displayName(), position.customDisplayName());
    }

    public static PortfolioPositionResponse from(PortfolioPosition position, QuoteResponse quote) {
        if ("MANUAL_CSV_IMPORT".equals(position.sourceType()) && quote != null && quote.last() != null
                && quote.last().amount() != null && quote.last().amount().signum() > 0
                && quote.last().currency().equalsIgnoreCase(position.averageCost().currency())) {
            var latest = quote.last();
            var marketValue = new MoneyResponse(latest.amount().multiply(position.quantity()), latest.currency());
            var costBasis = MoneyResponse.from(position.costBasis());
            var pnl = new MoneyResponse(marketValue.amount().subtract(costBasis.amount()), marketValue.currency());
            var pnlPercent = costBasis.amount().signum() == 0 ? BigDecimal.ZERO
                    : pnl.amount().divide(costBasis.amount(), 8, java.math.RoundingMode.HALF_UP)
                    .multiply(BigDecimal.valueOf(100)).setScale(4, java.math.RoundingMode.HALF_UP);
            return new PortfolioPositionResponse(position.positionId(), position.portfolioId(), InstrumentResponse.from(position.instrument()),
                    position.quantity(), MoneyResponse.from(position.averageCost()), latest,
                    MoneyResponse.from(position.importedPrice()), marketValue, costBasis, pnl, pnlPercent,
                    position.brokerType(), position.sourceType(), position.active(), quote.freshness(),
                    position.lastUpdated(), quote, position.displayName(), position.customDisplayName());
        }
        if ("MANUAL_CSV_IMPORT".equals(position.sourceType())) {
            return unpricedManualPosition(position, quote);
        }
        return new PortfolioPositionResponse(position.positionId(), position.portfolioId(), InstrumentResponse.from(position.instrument()),
                position.quantity(), MoneyResponse.from(position.averageCost()), MoneyResponse.from(position.currentPrice()),
                MoneyResponse.from(position.importedPrice()),
                MoneyResponse.from(position.marketValue()), MoneyResponse.from(position.costBasis()),
                MoneyResponse.from(position.unrealizedProfitLoss()), position.unrealizedProfitLossPercent(),
                position.brokerType(), position.sourceType(), position.active(),
                position.dataFreshness(), position.lastUpdated(), quote, position.displayName(), position.customDisplayName());
    }

    private static PortfolioPositionResponse unpricedManualPosition(PortfolioPosition position, QuoteResponse quote) {
        return new PortfolioPositionResponse(position.positionId(), position.portfolioId(), InstrumentResponse.from(position.instrument()),
                position.quantity(), MoneyResponse.from(position.averageCost()), null,
                MoneyResponse.from(position.importedPrice()), null, MoneyResponse.from(position.costBasis()),
                null, null, position.brokerType(), position.sourceType(), position.active(),
                quote == null || quote.freshness() == null ? "UNAVAILABLE" : quote.freshness(),
                position.lastUpdated(), quote, position.displayName(), position.customDisplayName());
    }

    public PortfolioPositionResponse withMappings(java.util.List<InstrumentProviderMappingResponse> mappings) {
        return new PortfolioPositionResponse(positionId,portfolioId,instrument.withMappings(mappings),quantity,
                averageCost,currentPrice,importedPrice,marketValue,costBasis,unrealizedProfitLoss,unrealizedProfitLossPercent,
                brokerType,sourceType,active,dataFreshness,lastUpdated,quote,displayName,customDisplayName);
    }
    public PortfolioPositionResponse withMaster(UUID masterId, java.util.List<InstrumentProviderMappingResponse> mappings) {
        return new PortfolioPositionResponse(positionId,portfolioId,instrument.withMaster(masterId,mappings),quantity,
                averageCost,currentPrice,importedPrice,marketValue,costBasis,unrealizedProfitLoss,unrealizedProfitLossPercent,
                brokerType,sourceType,active,dataFreshness,lastUpdated,quote,displayName,customDisplayName);
    }
    public PortfolioPositionResponse withMaster(com.aiinvestment.portfolio.infrastructure.persistence.InstrumentMasterEntity master,
                                                java.util.List<InstrumentProviderMappingResponse> mappings) {
        return new PortfolioPositionResponse(positionId,portfolioId,instrument.withMaster(master,mappings),quantity,
                averageCost,currentPrice,importedPrice,marketValue,costBasis,unrealizedProfitLoss,unrealizedProfitLossPercent,
                brokerType,sourceType,active,dataFreshness,lastUpdated,quote,displayName,customDisplayName);
    }
}
