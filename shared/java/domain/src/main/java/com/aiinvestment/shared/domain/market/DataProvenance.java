package com.aiinvestment.shared.domain.market;

import java.time.Instant;

public record DataProvenance(String source, Instant sourceTimestamp, Instant receivedAt, MarketDataFreshness freshness) {
    public DataProvenance {
        if (source == null || source.isBlank()) {
            throw new IllegalArgumentException("Source is required");
        }
        if (sourceTimestamp == null || receivedAt == null || freshness == null) {
            throw new IllegalArgumentException("Provenance timestamps and freshness are required");
        }
    }
}
