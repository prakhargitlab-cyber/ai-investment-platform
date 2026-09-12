package com.aiinvestment.shared.domain;

import java.util.Objects;
import java.util.UUID;

public record Instrument(
        UUID instrumentId,
        String provider,
        String providerInstrumentId,
        String isin,
        String ticker,
        String exchange,
        String mic,
        String companyName,
        AssetType assetType,
        String country,
        String tradingCurrency,
        String sector,
        String industry,
        String brokerSymbol,
        String brokerDescription,
        String brokerExchange,
        String canonicalSymbol,
        String canonicalName,
        String canonicalExchange,
        String canonicalMic,
        String securityType
) {
    public Instrument(UUID instrumentId, String isin, String ticker, String exchange, String mic, String companyName,
                      AssetType assetType, String country, String tradingCurrency, String sector, String industry) {
        this(instrumentId, null, null, isin, ticker, exchange, mic, companyName, assetType, country, tradingCurrency, sector, industry);
    }

    public Instrument(UUID instrumentId, String provider, String providerInstrumentId, String isin, String ticker,
                      String exchange, String mic, String companyName, AssetType assetType, String country,
                      String tradingCurrency, String sector, String industry) {
        this(instrumentId, provider, providerInstrumentId, isin, ticker, exchange, mic, companyName, assetType,
                country, tradingCurrency, sector, industry, ticker, companyName, exchange, ticker, companyName,
                exchange, mic, assetType == null ? null : assetType.name());
    }

    public Instrument {
        Objects.requireNonNull(instrumentId, "instrumentId is required");
        requireText(ticker, "ticker");
        if (exchange != null) {
            requireText(exchange, "exchange");
        }
        requireText(companyName, "companyName");
        Objects.requireNonNull(assetType, "assetType is required");
        if (tradingCurrency != null) {
            requireCurrency(tradingCurrency, "tradingCurrency");
        }
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
