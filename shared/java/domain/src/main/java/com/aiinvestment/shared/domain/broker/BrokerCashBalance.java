package com.aiinvestment.shared.domain.broker;

import com.aiinvestment.shared.domain.Money;

public record BrokerCashBalance(String brokerAccountId, Money cash) {
}
