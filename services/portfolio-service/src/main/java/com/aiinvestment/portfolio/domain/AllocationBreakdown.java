package com.aiinvestment.portfolio.domain;

import java.math.BigDecimal;
import java.util.Map;

public record AllocationBreakdown(
        Map<String, BigDecimal> country,
        Map<String, BigDecimal> currency,
        Map<String, BigDecimal> sector,
        Map<String, BigDecimal> assetType,
        Map<String, BigDecimal> broker
) {
}
