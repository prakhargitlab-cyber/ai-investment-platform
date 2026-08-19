package com.aiinvestment.portfolio.api;

import com.aiinvestment.portfolio.domain.Portfolio;
import com.aiinvestment.portfolio.domain.PortfolioSummary;

import java.time.Instant;
import java.util.UUID;

public record PortfolioListItemResponse(
        UUID portfolioId,
        String name,
        String baseCurrency,
        MoneyResponse totalMarketValue,
        MoneyResponse unrealizedProfitLoss,
        java.math.BigDecimal unrealizedProfitLossPercent,
        int positions,
        Instant updatedAt
) {
    public static PortfolioListItemResponse from(Portfolio portfolio, PortfolioSummary summary) {
        return new PortfolioListItemResponse(portfolio.portfolioId(), portfolio.name(), portfolio.baseCurrency(),
                MoneyResponse.from(summary.totalMarketValue()), MoneyResponse.from(summary.unrealizedProfitLoss()),
                summary.unrealizedProfitLossPercent(), summary.numberOfPositions(), portfolio.updatedAt());
    }
}
