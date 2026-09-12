package com.aiinvestment.portfolio.api;

import java.time.Instant;
import java.util.List;
import java.util.UUID;

public record BrokerPortfolioSyncResponse(
        UUID connectionId,
        String provider,
        String status,
        String action,
        String message,
        Instant completedAt,
        List<PortfolioListItemResponse> portfolios
) {
}
