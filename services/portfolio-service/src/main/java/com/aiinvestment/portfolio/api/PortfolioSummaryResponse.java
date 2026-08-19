package com.aiinvestment.portfolio.api;

import com.aiinvestment.portfolio.domain.PortfolioSummary;

import java.math.BigDecimal;
import java.util.UUID;

public record PortfolioSummaryResponse(
        UUID portfolioId,
        String baseCurrency,
        MoneyResponse totalMarketValue,
        MoneyResponse totalCostBasis,
        MoneyResponse unrealizedProfitLoss,
        BigDecimal unrealizedProfitLossPercent,
        MoneyResponse cash,
        int positions,
        AllocationResponse allocation
) {
    public static PortfolioSummaryResponse from(PortfolioSummary summary) {
        return new PortfolioSummaryResponse(summary.portfolioId(), summary.baseCurrency(),
                MoneyResponse.from(summary.totalMarketValue()), MoneyResponse.from(summary.totalCostBasis()),
                MoneyResponse.from(summary.unrealizedProfitLoss()), summary.unrealizedProfitLossPercent(),
                MoneyResponse.from(summary.cash()), summary.numberOfPositions(), AllocationResponse.from(summary.allocation()));
    }
}
