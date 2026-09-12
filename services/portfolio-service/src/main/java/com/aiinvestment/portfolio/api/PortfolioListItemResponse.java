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
        Instant updatedAt,
        String provider,
        UUID brokerConnectionId,
        Instant lastBrokerSyncAt,
        Instant lastBrokerSyncAttemptAt,
        String lastBrokerSyncErrorCode,
        boolean valuationComplete,
        String acquisitionSource,
        String sourceAccountReference,
        Instant lastImportedAt,
        String lastImportedFilename
) {
    public static PortfolioListItemResponse from(Portfolio portfolio, PortfolioSummary summary) {
        return new PortfolioListItemResponse(portfolio.portfolioId(), PortfolioPresentationName.forPortfolio(portfolio), portfolio.baseCurrency(),
                MoneyResponse.from(summary.totalMarketValue()), MoneyResponse.from(summary.unrealizedProfitLoss()),
                summary.unrealizedProfitLossPercent(), summary.numberOfPositions(), portfolio.updatedAt(),
                portfolio.brokerProvider() == null ? null : portfolio.brokerProvider().name(),
                portfolio.brokerConnectionId(), portfolio.lastSuccessfulBrokerSyncAt(),
                portfolio.lastBrokerSyncAttemptAt(), portfolio.lastBrokerSyncErrorCode(),
                summary.totalMarketValue() != null && !"XXX".equals(summary.totalMarketValue().currency()),
                portfolio.acquisitionSource(), portfolio.sourceAccountReference(), portfolio.lastImportedAt(),
                portfolio.lastImportedFilename());
    }
}
