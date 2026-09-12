package com.aiinvestment.portfolio.api;

import com.aiinvestment.portfolio.domain.Portfolio;

import java.time.Instant;
import java.util.UUID;

public record PortfolioResponse(
        UUID portfolioId,
        String name,
        String baseCurrency,
        Instant createdAt,
        Instant updatedAt,
        String provider,
        UUID brokerConnectionId,
        Instant lastBrokerSyncAt,
        Instant lastBrokerSyncAttemptAt,
        String lastBrokerSyncErrorCode,
        String acquisitionSource,
        String sourceAccountReference,
        Instant lastImportedAt,
        String lastImportedFilename
) {
    public static PortfolioResponse from(Portfolio portfolio) {
        return new PortfolioResponse(portfolio.portfolioId(), PortfolioPresentationName.forPortfolio(portfolio),
                portfolio.baseCurrency(), portfolio.createdAt(), portfolio.updatedAt(),
                portfolio.brokerProvider() == null ? null : portfolio.brokerProvider().name(),
                portfolio.brokerConnectionId(), portfolio.lastSuccessfulBrokerSyncAt(),
                portfolio.lastBrokerSyncAttemptAt(), portfolio.lastBrokerSyncErrorCode(), portfolio.acquisitionSource(),
                portfolio.sourceAccountReference(), portfolio.lastImportedAt(), portfolio.lastImportedFilename());
    }
}
