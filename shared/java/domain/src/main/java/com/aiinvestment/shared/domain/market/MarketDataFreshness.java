package com.aiinvestment.shared.domain.market;

public enum MarketDataFreshness {
    REAL_TIME,
    DELAYED,
    END_OF_DAY,
    STALE,
    MOCK,
    UNAVAILABLE
}
