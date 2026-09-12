package com.aiinvestment.portfolio.infrastructure.persistence;

import jakarta.persistence.*;

import java.math.BigDecimal;
import java.time.Instant;
import java.util.UUID;

@Entity
@Table(name = "portfolio_valuation_snapshots",
        uniqueConstraints = @UniqueConstraint(name = "uk_portfolio_valuation_source_sync", columnNames = {"portfolio_id", "source_sync_id"}))
public class PortfolioValuationSnapshotEntity {
    @Id
    private UUID id;
    @Column(name = "portfolio_id", nullable = false)
    private UUID portfolioId;
    @Column(name = "user_id", nullable = false)
    private UUID userId;
    @Column(name = "snapshot_timestamp", nullable = false)
    private Instant snapshotTimestamp;
    @Column(name = "base_currency", nullable = false)
    private String baseCurrency;
    @Column(name = "invested_capital_amount")
    private BigDecimal investedCapitalAmount;
    @Column(name = "invested_capital_currency")
    private String investedCapitalCurrency;
    @Column(name = "invested_capital_status", nullable = false)
    private String investedCapitalStatus;
    @Column(name = "cash_amount", nullable = false)
    private BigDecimal cashAmount;
    @Column(name = "cash_currency", nullable = false)
    private String cashCurrency;
    @Column(name = "positions_market_value_amount", nullable = false)
    private BigDecimal positionsMarketValueAmount;
    @Column(name = "positions_market_value_currency", nullable = false)
    private String positionsMarketValueCurrency;
    @Column(name = "portfolio_market_value_amount", nullable = false)
    private BigDecimal portfolioMarketValueAmount;
    @Column(name = "portfolio_market_value_currency", nullable = false)
    private String portfolioMarketValueCurrency;
    @Column(name = "unrealized_pnl_amount", nullable = false)
    private BigDecimal unrealizedPnlAmount;
    @Column(name = "unrealized_pnl_currency", nullable = false)
    private String unrealizedPnlCurrency;
    @Column(name = "realized_pnl_amount")
    private BigDecimal realizedPnlAmount;
    @Column(name = "realized_pnl_currency")
    private String realizedPnlCurrency;
    @Column(nullable = false)
    private String broker;
    @Column(nullable = false)
    private String source;
    @Column(name = "data_freshness", nullable = false)
    private String dataFreshness;
    @Column(name = "source_sync_id", nullable = false)
    private String sourceSyncId;
    @Column(name = "created_at", nullable = false)
    private Instant createdAt;

    protected PortfolioValuationSnapshotEntity() {
    }

    public PortfolioValuationSnapshotEntity(UUID id, UUID portfolioId, UUID userId, Instant snapshotTimestamp,
                                            String baseCurrency, BigDecimal investedCapitalAmount,
                                            String investedCapitalCurrency, String investedCapitalStatus,
                                            BigDecimal cashAmount, String cashCurrency,
                                            BigDecimal positionsMarketValueAmount, String positionsMarketValueCurrency,
                                            BigDecimal portfolioMarketValueAmount, String portfolioMarketValueCurrency,
                                            BigDecimal unrealizedPnlAmount, String unrealizedPnlCurrency,
                                            BigDecimal realizedPnlAmount, String realizedPnlCurrency, String broker,
                                            String source, String dataFreshness, String sourceSyncId, Instant createdAt) {
        this.id = id;
        this.portfolioId = portfolioId;
        this.userId = userId;
        this.snapshotTimestamp = snapshotTimestamp;
        this.baseCurrency = baseCurrency;
        this.investedCapitalAmount = investedCapitalAmount;
        this.investedCapitalCurrency = investedCapitalCurrency;
        this.investedCapitalStatus = investedCapitalStatus;
        this.cashAmount = cashAmount;
        this.cashCurrency = cashCurrency;
        this.positionsMarketValueAmount = positionsMarketValueAmount;
        this.positionsMarketValueCurrency = positionsMarketValueCurrency;
        this.portfolioMarketValueAmount = portfolioMarketValueAmount;
        this.portfolioMarketValueCurrency = portfolioMarketValueCurrency;
        this.unrealizedPnlAmount = unrealizedPnlAmount;
        this.unrealizedPnlCurrency = unrealizedPnlCurrency;
        this.realizedPnlAmount = realizedPnlAmount;
        this.realizedPnlCurrency = realizedPnlCurrency;
        this.broker = broker;
        this.source = source;
        this.dataFreshness = dataFreshness;
        this.sourceSyncId = sourceSyncId;
        this.createdAt = createdAt;
    }

    public UUID getId() { return id; }
    public UUID getPortfolioId() { return portfolioId; }
    public UUID getUserId() { return userId; }
    public Instant getSnapshotTimestamp() { return snapshotTimestamp; }
    public String getBaseCurrency() { return baseCurrency; }
    public BigDecimal getInvestedCapitalAmount() { return investedCapitalAmount; }
    public String getInvestedCapitalCurrency() { return investedCapitalCurrency; }
    public String getInvestedCapitalStatus() { return investedCapitalStatus; }
    public BigDecimal getCashAmount() { return cashAmount; }
    public String getCashCurrency() { return cashCurrency; }
    public BigDecimal getPositionsMarketValueAmount() { return positionsMarketValueAmount; }
    public String getPositionsMarketValueCurrency() { return positionsMarketValueCurrency; }
    public BigDecimal getPortfolioMarketValueAmount() { return portfolioMarketValueAmount; }
    public String getPortfolioMarketValueCurrency() { return portfolioMarketValueCurrency; }
    public BigDecimal getUnrealizedPnlAmount() { return unrealizedPnlAmount; }
    public String getUnrealizedPnlCurrency() { return unrealizedPnlCurrency; }
    public BigDecimal getRealizedPnlAmount() { return realizedPnlAmount; }
    public String getRealizedPnlCurrency() { return realizedPnlCurrency; }
    public String getBroker() { return broker; }
    public String getSource() { return source; }
    public String getDataFreshness() { return dataFreshness; }
    public String getSourceSyncId() { return sourceSyncId; }
    public Instant getCreatedAt() { return createdAt; }
}
