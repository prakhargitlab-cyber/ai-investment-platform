package com.aiinvestment.portfolio.domain;

import com.aiinvestment.shared.domain.fx.FxRateProvider;
import org.springframework.stereotype.Component;

import java.math.BigDecimal;
import java.util.Map;

@Component
public class MockFxRateProvider implements FxRateProvider {
    private final Map<String, BigDecimal> rates = Map.of(
            "USD_EUR", new BigDecimal("0.9200"),
            "INR_EUR", new BigDecimal("0.0110"),
            "EUR_USD", new BigDecimal("1.0870"),
            "INR_USD", new BigDecimal("0.0120"),
            "EUR_INR", new BigDecimal("90.9000"),
            "USD_INR", new BigDecimal("83.3000")
    );

    @Override
    public BigDecimal getRate(String fromCurrency, String toCurrency) {
        requireCurrency(fromCurrency);
        requireCurrency(toCurrency);
        if (fromCurrency.equals(toCurrency)) {
            return BigDecimal.ONE;
        }
        BigDecimal rate = rates.get(fromCurrency + "_" + toCurrency);
        if (rate == null) {
            throw new IllegalArgumentException("No mock FX rate configured for " + fromCurrency + " to " + toCurrency);
        }
        return rate;
    }

    private static void requireCurrency(String currency) {
        if (currency == null || !currency.matches("[A-Z]{3}")) {
            throw new IllegalArgumentException("currency must be a 3-letter ISO currency code");
        }
    }
}
