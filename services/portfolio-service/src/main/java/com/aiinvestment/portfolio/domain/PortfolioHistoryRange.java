package com.aiinvestment.portfolio.domain;

import java.time.Duration;
import java.time.Instant;

public enum PortfolioHistoryRange {
    ONE_DAY("1D", Duration.ofDays(1), 288),
    FIVE_DAYS("5D", Duration.ofDays(5), 480),
    ONE_WEEK("1W", Duration.ofDays(7), 336),
    ONE_MONTH("1M", Duration.ofDays(31), 370),
    ONE_YEAR("1Y", Duration.ofDays(365), 370),
    TWO_YEARS("2Y", Duration.ofDays(365L * 2), 260),
    THREE_YEARS("3Y", Duration.ofDays(365L * 3), 260),
    FOUR_YEARS("4Y", Duration.ofDays(365L * 4), 260),
    FIVE_YEARS("5Y", Duration.ofDays(365L * 5), 260),
    MAX("MAX", null, 500);

    private final String code;
    private final Duration lookback;
    private final int maxPoints;

    PortfolioHistoryRange(String code, Duration lookback, int maxPoints) {
        this.code = code;
        this.lookback = lookback;
        this.maxPoints = maxPoints;
    }

    public String code() {
        return code;
    }

    public Instant from(Instant to) {
        return lookback == null ? null : to.minus(lookback);
    }

    public int maxPoints() {
        return maxPoints;
    }

    public static PortfolioHistoryRange fromCode(String value) {
        for (PortfolioHistoryRange range : values()) {
            if (range.code.equalsIgnoreCase(value)) {
                return range;
            }
        }
        throw new IllegalArgumentException("Unsupported portfolio history range: " + value);
    }
}
