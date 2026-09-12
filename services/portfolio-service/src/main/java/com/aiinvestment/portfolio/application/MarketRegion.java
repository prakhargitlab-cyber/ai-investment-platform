package com.aiinvestment.portfolio.application;

import com.aiinvestment.portfolio.infrastructure.persistence.InstrumentMasterEntity;

import java.util.Locale;
import java.util.Set;

public enum MarketRegion {
    INDIA("WATCHLIST-IND"),
    EUROPE("WATCHLIST-EU"),
    USA("WATCHLIST-USA");

    private static final Set<String> EUROPE_COUNTRIES = Set.of(
            "AT", "BE", "CH", "DE", "DK", "ES", "FI", "FR", "GB", "IE", "IT", "NL", "NO", "PT", "SE"
    );
    private static final Set<String> EUROPE_EXCHANGES = Set.of(
            "XETR", "XAMS", "AEB", "XLON", "XPAR", "XSWX", "XMIL", "XMAD", "XSTO", "XHEL", "XCSE", "XOSL"
    );

    private final String defaultWatchlistName;

    MarketRegion(String defaultWatchlistName) {
        this.defaultWatchlistName = defaultWatchlistName;
    }

    public String defaultWatchlistName() {
        return defaultWatchlistName;
    }

    public static MarketRegion parse(String value) {
        if (value == null || value.isBlank()) {
            throw new IllegalArgumentException("WATCHLIST_REGION_REQUIRED");
        }
        try {
            return valueOf(value.trim().toUpperCase(Locale.ROOT));
        } catch (IllegalArgumentException exception) {
            throw new IllegalArgumentException("WATCHLIST_REGION_UNSUPPORTED");
        }
    }

    public static MarketRegion from(InstrumentMasterEntity instrument) {
        String country = upper(instrument.getCountry());
        String exchange = upper(instrument.getPrimaryExchange());
        if (Set.of("IN", "IND", "INDIA").contains(country)
                || Set.of("XNSE", "NSE", "XBOM", "BSE").contains(exchange)) {
            return INDIA;
        }
        if (Set.of("US", "USA").contains(country)
                || Set.of("XNAS", "XNYS", "ARCX", "BATS").contains(exchange)) {
            return USA;
        }
        if (EUROPE_COUNTRIES.contains(country) || EUROPE_EXCHANGES.contains(exchange)) {
            return EUROPE;
        }
        throw new IllegalArgumentException("WATCHLIST_INSTRUMENT_REGION_UNSUPPORTED");
    }

    private static String upper(String value) {
        return value == null ? "" : value.trim().toUpperCase(Locale.ROOT);
    }
}
