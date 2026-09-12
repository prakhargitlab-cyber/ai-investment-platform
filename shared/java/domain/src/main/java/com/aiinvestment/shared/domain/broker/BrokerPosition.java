package com.aiinvestment.shared.domain.broker;

import com.aiinvestment.shared.domain.Instrument;
import com.aiinvestment.shared.domain.Money;

import java.math.BigDecimal;
import java.time.Instant;
import java.util.Objects;

public record BrokerPosition(
        String brokerAccountId,
        Instrument instrument,
        BigDecimal quantity,
        Money averageCost,
        Money currentPrice,
        Money marketValue,
        Money unrealizedProfitLoss,
        Instant observedAt
) {
    public BrokerPosition {
        if (brokerAccountId == null || brokerAccountId.isBlank()) {
            throw new IllegalArgumentException("brokerAccountId is required");
        }
        Objects.requireNonNull(instrument, "instrument is required");
        Objects.requireNonNull(quantity, "quantity is required");
        if (quantity.signum() < 0) {
            throw new IllegalArgumentException("quantity cannot be negative");
        }
        Objects.requireNonNull(observedAt, "observedAt is required");
    }

    public BrokerPosition(String brokerAccountId, Instrument instrument, BigDecimal quantity, Money averageCost,
                          Money currentPrice, Instant observedAt) {
        this(brokerAccountId, instrument, quantity, averageCost, currentPrice, null, null, observedAt);
    }
}
