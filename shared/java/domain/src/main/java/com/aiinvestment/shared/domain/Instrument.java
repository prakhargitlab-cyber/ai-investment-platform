package com.aiinvestment.shared.domain;

import java.util.Objects;
import java.util.UUID;

public record Instrument(
        UUID instrumentId,
        String isin,
        String ticker,
        String exchange,
        String mic,
        String companyName,
        AssetType assetType,
        String country,
        String tradingCurrency,
        String sector,
        String industry
) {
    public Instrument {
        Objects.requireNonNull(instrumentId, "instrumentId is required");
        requireText(ticker, "ticker");
        requireText(exchange, "exchange");
        requireText(companyName, "companyName");
        Objects.requireNonNull(assetType, "assetType is required");
        requireCurrency(tradingCurrency, "tradingCurrency");
    }

    private static void requireText(String value, String name) {
        if (value == null || value.isBlank()) {
            throw new IllegalArgumentException(name + " is required");
        }
    }

    private static void requireCurrency(String value, String name) {
        requireText(value, name);
        if (!value.matches("[A-Z]{3}")) {
            throw new IllegalArgumentException(name + " must be a 3-letter ISO currency code");
        }
    }
}
