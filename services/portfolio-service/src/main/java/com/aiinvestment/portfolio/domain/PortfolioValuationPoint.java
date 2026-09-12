package com.aiinvestment.portfolio.domain;

import com.aiinvestment.shared.domain.Money;

import java.time.Instant;
import java.util.UUID;

public record PortfolioValuationPoint(
        UUID id,
        UUID portfolioId,
        UUID userId,
        Instant timestamp,
        String baseCurrency,
        Money investedCapital,
        String investedCapitalStatus,
        Money cash,
        Money positionsMarketValue,
        Money portfolioMarketValue,
        Money unrealizedPnl,
        Money realizedPnl,
        String broker,
        String source,
        String dataFreshness,
        Instant createdAt
) {
}
