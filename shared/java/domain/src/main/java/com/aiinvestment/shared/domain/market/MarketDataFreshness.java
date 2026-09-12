package com.aiinvestment.shared.domain.market;

public enum MarketDataFreshness {
    REAL_BROKER,
    REAL_TIME,
    DELAYED,
    END_OF_DAY,
    STALE,
    MOCK,
    UNAVAILABLE
}
