package com.aiinvestment.portfolio.api;

import com.aiinvestment.portfolio.domain.Portfolio;

import java.time.Instant;
import java.util.UUID;

public record PortfolioResponse(
        UUID portfolioId,
        UUID userId,
        String name,
        String baseCurrency,
        Instant createdAt,
        Instant updatedAt
) {
    public static PortfolioResponse from(Portfolio portfolio) {
        return new PortfolioResponse(portfolio.portfolioId(), portfolio.userId(), portfolio.name(),
                portfolio.baseCurrency(), portfolio.createdAt(), portfolio.updatedAt());
    }
}
