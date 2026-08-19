package com.aiinvestment.shared.domain.event;

import com.aiinvestment.shared.domain.market.MarketDataFreshness;

import java.time.Instant;
import java.util.UUID;

public record MarketQuoteUpdatedEvent(
        String eventType,
        int version,
        UUID eventId,
        String correlationId,
        Instant occurredAt,
        UUID instrumentId,
        MarketDataFreshness freshness
) implements PlatformEvent {
    public MarketQuoteUpdatedEvent(UUID eventId, String correlationId, Instant occurredAt, UUID instrumentId,
                                   MarketDataFreshness freshness) {
        this("market.quote.updated", 1, eventId, correlationId, occurredAt, instrumentId, freshness);
    }
}
