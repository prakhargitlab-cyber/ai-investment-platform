package com.aiinvestment.portfolio.domain;

import com.aiinvestment.shared.domain.Money;

import java.math.BigDecimal;
import java.util.UUID;

public record PortfolioSummary(
        UUID portfolioId,
        String baseCurrency,
        Money totalMarketValue,
        Money totalCostBasis,
        Money unrealizedProfitLoss,
        BigDecimal unrealizedProfitLossPercent,
        Money cash,
        int numberOfPositions,
        AllocationBreakdown allocation
) {
}
