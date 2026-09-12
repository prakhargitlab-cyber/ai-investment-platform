package com.aiinvestment.broker.api;

import com.aiinvestment.shared.domain.Instrument;
import com.aiinvestment.shared.domain.broker.BrokerAccount;
import com.aiinvestment.shared.domain.broker.BrokerCashBalance;
import com.aiinvestment.shared.domain.broker.BrokerPosition;

import java.math.BigDecimal;
import java.time.Instant;
import java.util.List;

public record BrokerSnapshotResponse(
        BrokerConnectionResponse connection,
        List<AccountResponse> accounts,
        List<PositionResponse> positions,
        List<CashResponse> cashBalances
) {
    public record AccountResponse(
            String brokerAccountId,
            String brokerType,
            String externalAccountReference,
            String displayName,
            String baseCurrency,
            String status
    ) {
        public static AccountResponse from(BrokerAccount account) {
            return new AccountResponse(account.brokerAccountId(), account.brokerType().name(), account.externalAccountReference(),
                    account.displayName(), account.baseCurrency(), account.status().name());
        }
    }

    public record PositionResponse(
            String brokerAccountId,
            Instrument instrument,
            BigDecimal quantity,
            MoneyResponse averageCost,
            MoneyResponse currentPrice,
            MoneyResponse marketValue,
            MoneyResponse unrealizedProfitLoss,
            String dataFreshness,
            Instant observedAt
    ) {
        public static PositionResponse from(BrokerPosition position, String dataFreshness) {
            return new PositionResponse(position.brokerAccountId(), position.instrument(), position.quantity(),
                    position.averageCost() == null ? null : MoneyResponse.from(position.averageCost()),
                    position.currentPrice() == null ? null : MoneyResponse.from(position.currentPrice()),
                    position.marketValue() == null ? null : MoneyResponse.from(position.marketValue()),
                    position.unrealizedProfitLoss() == null ? null : MoneyResponse.from(position.unrealizedProfitLoss()),
                    dataFreshness, position.observedAt());
        }
    }

    public record CashResponse(
            String brokerAccountId,
            MoneyResponse cash,
            MoneyResponse settledCash,
            MoneyResponse netLiquidationValue,
            MoneyResponse stockMarketValue,
            MoneyResponse unrealizedPnl,
            MoneyResponse realizedPnl,
            String source
    ) {
        public static CashResponse from(BrokerCashBalance cashBalance) {
            return new CashResponse(
                    cashBalance.brokerAccountId(),
                    MoneyResponse.from(cashBalance.cash()),
                    cashBalance.settledCash() == null ? null : MoneyResponse.from(cashBalance.settledCash()),
                    cashBalance.netLiquidationValue() == null ? null : MoneyResponse.from(cashBalance.netLiquidationValue()),
                    cashBalance.stockMarketValue() == null ? null : MoneyResponse.from(cashBalance.stockMarketValue()),
                    cashBalance.unrealizedPnl() == null ? null : MoneyResponse.from(cashBalance.unrealizedPnl()),
                    cashBalance.realizedPnl() == null ? null : MoneyResponse.from(cashBalance.realizedPnl()),
                    cashBalance.source());
        }
    }
}
