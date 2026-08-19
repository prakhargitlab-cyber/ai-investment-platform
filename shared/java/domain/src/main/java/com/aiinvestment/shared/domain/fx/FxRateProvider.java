package com.aiinvestment.shared.domain.fx;

import java.math.BigDecimal;

public interface FxRateProvider {
    BigDecimal getRate(String fromCurrency, String toCurrency);
}
