package com.aiinvestment.portfolio.api;

import java.math.BigDecimal;
import java.util.List;
import java.util.Map;

public record PortfolioDashboardResponse(
        Map<String, BigDecimal> currencyTotals,
        List<PortfolioListItemResponse> portfolios,
        List<PortfolioPositionResponse> holdings,
        List<CombinedPortfolioHoldingResponse> combinedHoldings,
        List<String> incompleteValuationPortfolioIds
) {
}
