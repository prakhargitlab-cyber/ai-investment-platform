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
        MoneyResponse marketValue,
        MoneyResponse costBasis,
        MoneyResponse unrealizedProfitLoss,
        BigDecimal unrealizedProfitLossPercent,
        String brokerAccountId,
        Instant lastUpdated,
        QuoteResponse quote
) {
    public static PortfolioPositionResponse from(PortfolioPosition position) {
        return new PortfolioPositionResponse(position.positionId(), position.portfolioId(), InstrumentResponse.from(position.instrument()),
                position.quantity(), MoneyResponse.from(position.averageCost()), MoneyResponse.from(position.currentPrice()),
                MoneyResponse.from(position.marketValue()), MoneyResponse.from(position.costBasis()),
                MoneyResponse.from(position.unrealizedProfitLoss()), position.unrealizedProfitLossPercent(),
                position.brokerAccountId(), position.lastUpdated(), null);
    }

    public static PortfolioPositionResponse from(PortfolioPosition position, QuoteResponse quote) {
        return new PortfolioPositionResponse(position.positionId(), position.portfolioId(), InstrumentResponse.from(position.instrument()),
                position.quantity(), MoneyResponse.from(position.averageCost()), MoneyResponse.from(position.currentPrice()),
                MoneyResponse.from(position.marketValue()), MoneyResponse.from(position.costBasis()),
                MoneyResponse.from(position.unrealizedProfitLoss()), position.unrealizedProfitLossPercent(),
                position.brokerAccountId(), position.lastUpdated(), quote);
    }
}
