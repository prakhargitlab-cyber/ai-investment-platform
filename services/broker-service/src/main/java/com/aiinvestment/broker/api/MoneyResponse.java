package com.aiinvestment.broker.api;

import com.aiinvestment.shared.domain.Money;

import java.math.BigDecimal;

public record MoneyResponse(BigDecimal amount, String currency) {
    public static MoneyResponse from(Money money) {
        return new MoneyResponse(money.amount(), money.currency());
    }
}
