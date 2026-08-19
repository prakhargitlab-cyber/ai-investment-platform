package com.aiinvestment.shared.domain;

import java.math.BigDecimal;
import java.math.RoundingMode;
import java.util.Objects;

public record Money(BigDecimal amount, String currency) {
    public Money {
        Objects.requireNonNull(amount, "amount is required");
        if (currency == null || !currency.matches("[A-Z]{3}")) {
            throw new IllegalArgumentException("currency must be a 3-letter ISO currency code");
        }
        amount = amount.setScale(4, RoundingMode.HALF_UP);
    }

    public static Money zero(String currency) {
        return new Money(BigDecimal.ZERO, currency);
    }

    public Money add(Money other) {
        requireSameCurrency(other);
        return new Money(amount.add(other.amount), currency);
    }

    public Money subtract(Money other) {
        requireSameCurrency(other);
        return new Money(amount.subtract(other.amount), currency);
    }

    public Money multiply(BigDecimal multiplier) {
        return new Money(amount.multiply(Objects.requireNonNull(multiplier, "multiplier is required")), currency);
    }

    private void requireSameCurrency(Money other) {
        Objects.requireNonNull(other, "other money is required");
        if (!currency.equals(other.currency)) {
            throw new IllegalArgumentException("currency mismatch: " + currency + " != " + other.currency);
        }
    }
}
