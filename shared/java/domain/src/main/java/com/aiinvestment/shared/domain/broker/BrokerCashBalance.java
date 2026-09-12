package com.aiinvestment.shared.domain.broker;

import com.aiinvestment.shared.domain.Money;

public record BrokerCashBalance(
        String brokerAccountId,
        Money cash,
        Money settledCash,
        Money netLiquidationValue,
        Money stockMarketValue,
        Money unrealizedPnl,
        Money realizedPnl,
        String source
) {
    public BrokerCashBalance(String brokerAccountId, Money cash) {
        this(brokerAccountId, cash, null, null, null, null, null, null);
    }
}
