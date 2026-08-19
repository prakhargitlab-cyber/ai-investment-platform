package com.aiinvestment.shared.domain.market;

import java.time.Instant;
import java.util.UUID;

public record MarketDataStatus(UUID instrumentId, String source, MarketDataFreshness freshness, MarketStatus marketStatus, Instant updatedAt) {
}
