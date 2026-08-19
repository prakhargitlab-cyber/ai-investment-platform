package com.aiinvestment.portfolio.api;

import com.aiinvestment.shared.domain.Money;

import java.math.BigDecimal;

public record MoneyResponse(BigDecimal amount, String currency) {
    public static MoneyResponse from(Money money) {
        if (money == null) {
            return null;
        }
        return new MoneyResponse(money.amount(), money.currency());
    }
}
