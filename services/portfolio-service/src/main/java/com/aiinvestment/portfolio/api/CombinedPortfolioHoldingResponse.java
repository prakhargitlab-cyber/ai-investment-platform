package com.aiinvestment.portfolio.api;

import com.aiinvestment.portfolio.domain.PortfolioPosition;
import java.math.BigDecimal;
import java.math.RoundingMode;
import java.time.Instant;
import java.util.*;

public record CombinedPortfolioHoldingResponse(String securityKey, String isin, String symbol, String companyName,
                                               BigDecimal quantity, MoneyResponse averageCost,
                                               MoneyResponse marketValue, String dataFreshness) {
    public static List<CombinedPortfolioHoldingResponse> aggregate(List<PortfolioPosition> positions) {
        Map<String, List<PortfolioPosition>> groups = new TreeMap<>();
        positions.stream().filter(PortfolioPosition::active)
                .forEach(position -> groups.computeIfAbsent(identity(position) + ":" +
                        position.averageCost().currency(), ignored -> new ArrayList<>()).add(position));
        return groups.values().stream().map(CombinedPortfolioHoldingResponse::aggregateGroup).toList();
    }

    private static CombinedPortfolioHoldingResponse aggregateGroup(List<PortfolioPosition> holdings) {
        BigDecimal quantity = holdings.stream().map(PortfolioPosition::quantity).reduce(BigDecimal.ZERO, BigDecimal::add);
        BigDecimal costBasis = holdings.stream().map(position -> position.averageCost().amount().multiply(position.quantity()))
                .reduce(BigDecimal.ZERO, BigDecimal::add);
        BigDecimal average = quantity.signum() == 0 ? BigDecimal.ZERO
                : costBasis.divide(quantity, 8, RoundingMode.HALF_UP).stripTrailingZeros();
        boolean valuationComplete = holdings.stream().allMatch(position -> position.marketValue() != null);
        BigDecimal marketValue = valuationComplete
                ? holdings.stream().map(position -> position.marketValue().amount()).reduce(BigDecimal.ZERO, BigDecimal::add)
                : null;
        PortfolioPosition latest = holdings.stream().max(Comparator.comparing(PortfolioPosition::lastUpdated)
                .thenComparing(position -> position.positionId().toString())).orElseThrow();
        PortfolioPosition displayHolding = holdings.stream().max(Comparator
                .comparing((PortfolioPosition position) -> position.customDisplayName() != null
                        && !position.customDisplayName().isBlank())
                .thenComparing(PortfolioPosition::lastUpdated)
                .thenComparing(position -> position.positionId().toString())).orElseThrow();
        String freshness = holdings.stream().allMatch(position -> "IMPORTED_SNAPSHOT".equals(position.dataFreshness()))
                ? "IMPORTED_SNAPSHOT" : "MIXED";
        String currency = latest.averageCost().currency();
        return new CombinedPortfolioHoldingResponse(identity(latest), latest.instrument().isin(),
                latest.instrument().ticker(), displayHolding.displayName(), quantity,
                new MoneyResponse(average, currency),
                marketValue == null ? null : new MoneyResponse(marketValue, currency), freshness);
    }

    private static String identity(PortfolioPosition position) {
        if (position.instrument().isin() != null && !position.instrument().isin().isBlank())
            return "ISIN:" + position.instrument().isin();
        return position.instrument().provider() + ":" + position.instrument().providerInstrumentId();
    }
}
