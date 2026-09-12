package com.aiinvestment.portfolio.application;

import com.aiinvestment.shared.domain.Instrument;

import java.math.BigDecimal;
import java.time.Instant;
import java.util.List;

public record BrokerSnapshotClientResponse(
        Connection connection,
        List<Account> accounts,
        List<Position> positions,
        List<CashBalance> cashBalances
) {
    public BrokerSnapshotClientResponse(List<Account> accounts, List<Position> positions, List<CashBalance> cashBalances) {
        this(null, accounts, positions, cashBalances);
    }

    public record Connection(String connectionId, String brokerType, String displayName, String status,
                             List<String> capabilities, String providerStatus, String dataFreshness) {
    }
    public record Account(
            String brokerAccountId,
            String brokerType,
            String externalAccountReference,
            String displayName,
            String baseCurrency,
            String status
    ) {
    }

    public record Position(
            String brokerAccountId,
            Instrument instrument,
            BigDecimal quantity,
            MoneyValue averageCost,
            MoneyValue currentPrice,
            MoneyValue marketValue,
            MoneyValue unrealizedProfitLoss,
            String dataFreshness,
            Instant observedAt
    ) {
    }

    public record CashBalance(
            String brokerAccountId,
            MoneyValue cash,
            MoneyValue settledCash,
            MoneyValue netLiquidationValue,
            MoneyValue stockMarketValue,
            MoneyValue unrealizedPnl,
            MoneyValue realizedPnl,
            String source
    ) {
    }

    public record MoneyValue(BigDecimal amount, String currency) {
    }
}
