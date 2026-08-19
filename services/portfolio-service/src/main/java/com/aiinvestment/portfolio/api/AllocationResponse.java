package com.aiinvestment.portfolio.api;

import com.aiinvestment.portfolio.domain.AllocationBreakdown;

import java.math.BigDecimal;
import java.util.Map;

public record AllocationResponse(
        Map<String, BigDecimal> country,
        Map<String, BigDecimal> currency,
        Map<String, BigDecimal> sector,
        Map<String, BigDecimal> assetType,
        Map<String, BigDecimal> broker
) {
    public static AllocationResponse from(AllocationBreakdown allocation) {
        return new AllocationResponse(allocation.country(), allocation.currency(), allocation.sector(),
                allocation.assetType(), allocation.broker());
    }
}
