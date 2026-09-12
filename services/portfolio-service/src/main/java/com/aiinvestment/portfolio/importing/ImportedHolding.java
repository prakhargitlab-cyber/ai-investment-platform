package com.aiinvestment.portfolio.importing;

import java.math.BigDecimal;

public record ImportedHolding(String securityKey, String isin, String symbol, String companyName,
                              BigDecimal quantity, BigDecimal longTermQuantity,
                              BigDecimal averageCost, BigDecimal importedCurrentPrice,
                              BigDecimal valueAtCost, BigDecimal importedMarketValue, BigDecimal realizedPnl,
                              BigDecimal unrealizedPnl, BigDecimal unrealizedPnlPercent,
                              BigDecimal totalPnl, BigDecimal todayPnl, String currency) {
}
