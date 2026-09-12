package com.aiinvestment.portfolio.domain;

import java.time.Instant;
import java.util.List;
import java.util.UUID;

public record PortfolioHistory(
        UUID portfolioId,
        String baseCurrency,
        PortfolioHistoryRange range,
        Instant from,
        Instant to,
        String investedCapitalStatus,
        boolean backfillAvailable,
        List<PortfolioValuationPoint> points
) {
}
