package com.aiinvestment.shared.domain.market;

import com.aiinvestment.shared.domain.Money;

import java.time.Instant;
import java.util.UUID;

public record Quote(
        UUID instrumentId,
        Money bid,
        Money ask,
        Money last,
        Money previousClose,
        String currency,
        Instant timestamp,
        String source,
        MarketDataFreshness freshness,
        MarketStatus marketStatus,
        Instant sourceTimestamp,
        Instant receivedAt
) {
    public Quote(UUID instrumentId, Money bid, Money ask, Money last, Money previousClose, String currency,
                 Instant timestamp, String source, MarketDataFreshness freshness, MarketStatus marketStatus) {
        this(instrumentId, bid, ask, last, previousClose, currency, timestamp, source, freshness, marketStatus, timestamp, Instant.now());
    }

    public Quote {
        if (sourceTimestamp == null) {
            sourceTimestamp = timestamp;
        }
        if (receivedAt == null) {
            receivedAt = Instant.now();
        }
    }

    public DataProvenance provenance() {
        return new DataProvenance(source, sourceTimestamp, receivedAt, freshness);
    }
}
