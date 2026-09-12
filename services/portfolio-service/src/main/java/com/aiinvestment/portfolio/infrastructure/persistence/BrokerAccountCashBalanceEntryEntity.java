package com.aiinvestment.portfolio.infrastructure.persistence;

import jakarta.persistence.*;

import java.math.BigDecimal;
import java.time.Instant;
import java.util.UUID;

@Entity
@Table(name = "broker_account_cash_balance_entries")
public class BrokerAccountCashBalanceEntryEntity {
    @Id
    @Column(name = "cash_balance_id", nullable = false)
    private UUID cashBalanceId;
    @Column(name = "user_id", nullable = false)
    private UUID userId;
    @Column(name = "connection_id")
    private UUID connectionId;
    @Column(name = "broker_account_id", nullable = false)
    private String brokerAccountId;
    @Column(name = "cash_amount", nullable = false)
    private BigDecimal cashAmount;
    @Column(name = "cash_currency", nullable = false)
    private String cashCurrency;
    @Column(name = "settled_cash_amount")
    private BigDecimal settledCashAmount;
    @Column(name = "settled_cash_currency")
    private String settledCashCurrency;
    @Column(name = "net_liquidation_value_amount")
    private BigDecimal netLiquidationValueAmount;
    @Column(name = "net_liquidation_value_currency")
    private String netLiquidationValueCurrency;
    @Column(name = "stock_market_value_amount")
    private BigDecimal stockMarketValueAmount;
    @Column(name = "stock_market_value_currency")
    private String stockMarketValueCurrency;
    @Column(name = "unrealized_pnl_amount")
    private BigDecimal unrealizedPnlAmount;
    @Column(name = "unrealized_pnl_currency")
    private String unrealizedPnlCurrency;
    @Column(name = "realized_pnl_amount")
    private BigDecimal realizedPnlAmount;
    @Column(name = "realized_pnl_currency")
    private String realizedPnlCurrency;
    @Column(nullable = false)
    private String source;
    @Column(name = "observed_at", nullable = false)
    private Instant observedAt;

    protected BrokerAccountCashBalanceEntryEntity() {
    }

    public BrokerAccountCashBalanceEntryEntity(UUID cashBalanceId, UUID userId, UUID connectionId,
                                               String brokerAccountId, BigDecimal cashAmount, String cashCurrency,
                                               BigDecimal settledCashAmount, String settledCashCurrency,
                                               BigDecimal netLiquidationValueAmount, String netLiquidationValueCurrency,
                                               BigDecimal stockMarketValueAmount, String stockMarketValueCurrency,
                                               BigDecimal unrealizedPnlAmount, String unrealizedPnlCurrency,
                                               BigDecimal realizedPnlAmount, String realizedPnlCurrency,
                                               String source, Instant observedAt) {
        this.cashBalanceId = cashBalanceId;
        this.userId = userId;
        this.connectionId = connectionId;
        this.brokerAccountId = brokerAccountId;
        this.cashAmount = cashAmount;
        this.cashCurrency = cashCurrency;
        this.settledCashAmount = settledCashAmount;
        this.settledCashCurrency = settledCashCurrency;
        this.netLiquidationValueAmount = netLiquidationValueAmount;
        this.netLiquidationValueCurrency = netLiquidationValueCurrency;
        this.stockMarketValueAmount = stockMarketValueAmount;
        this.stockMarketValueCurrency = stockMarketValueCurrency;
        this.unrealizedPnlAmount = unrealizedPnlAmount;
        this.unrealizedPnlCurrency = unrealizedPnlCurrency;
        this.realizedPnlAmount = realizedPnlAmount;
        this.realizedPnlCurrency = realizedPnlCurrency;
        this.source = source;
        this.observedAt = observedAt;
    }

    public UUID getCashBalanceId() { return cashBalanceId; }
    public UUID getUserId() { return userId; }
    public UUID getConnectionId() { return connectionId; }
    public String getBrokerAccountId() { return brokerAccountId; }
    public BigDecimal getCashAmount() { return cashAmount; }
    public String getCashCurrency() { return cashCurrency; }
    public BigDecimal getSettledCashAmount() { return settledCashAmount; }
    public String getSettledCashCurrency() { return settledCashCurrency; }
    public BigDecimal getNetLiquidationValueAmount() { return netLiquidationValueAmount; }
    public String getNetLiquidationValueCurrency() { return netLiquidationValueCurrency; }
    public BigDecimal getStockMarketValueAmount() { return stockMarketValueAmount; }
    public String getStockMarketValueCurrency() { return stockMarketValueCurrency; }
    public BigDecimal getUnrealizedPnlAmount() { return unrealizedPnlAmount; }
    public String getUnrealizedPnlCurrency() { return unrealizedPnlCurrency; }
    public BigDecimal getRealizedPnlAmount() { return realizedPnlAmount; }
    public String getRealizedPnlCurrency() { return realizedPnlCurrency; }
    public String getSource() { return source; }
    public Instant getObservedAt() { return observedAt; }
}
