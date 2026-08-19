package com.aiinvestment.portfolio.infrastructure.persistence;

import jakarta.persistence.*;

import java.math.BigDecimal;
import java.time.Instant;
import java.util.UUID;

@Entity
@Table(name = "portfolio_positions")
public class PortfolioPositionEntity {
    @Id
    @Column(name = "position_id", nullable = false)
    private UUID positionId;
    @ManyToOne(optional = false)
    @JoinColumn(name = "portfolio_id")
    private PortfolioEntity portfolio;
    @ManyToOne(optional = false, cascade = CascadeType.MERGE)
    @JoinColumn(name = "instrument_id")
    private InstrumentEntity instrument;
    @Column(nullable = false)
    private BigDecimal quantity;
    @Column(name = "average_cost_amount", nullable = false)
    private BigDecimal averageCostAmount;
    @Column(name = "average_cost_currency", nullable = false)
    private String averageCostCurrency;
    @Column(name = "current_price_amount", nullable = false)
    private BigDecimal currentPriceAmount;
    @Column(name = "current_price_currency", nullable = false)
    private String currentPriceCurrency;
    @ManyToOne(optional = false)
    @JoinColumn(name = "broker_account_id")
    private BrokerAccountEntity brokerAccount;
    @Column(name = "last_updated", nullable = false)
    private Instant lastUpdated;

    protected PortfolioPositionEntity() {
    }

    public PortfolioPositionEntity(UUID positionId, PortfolioEntity portfolio, InstrumentEntity instrument, BigDecimal quantity,
                                   BigDecimal averageCostAmount, String averageCostCurrency, BigDecimal currentPriceAmount,
                                   String currentPriceCurrency, BrokerAccountEntity brokerAccount, Instant lastUpdated) {
        this.positionId = positionId;
        this.portfolio = portfolio;
        this.instrument = instrument;
        this.quantity = quantity;
        this.averageCostAmount = averageCostAmount;
        this.averageCostCurrency = averageCostCurrency;
        this.currentPriceAmount = currentPriceAmount;
        this.currentPriceCurrency = currentPriceCurrency;
        this.brokerAccount = brokerAccount;
        this.lastUpdated = lastUpdated;
    }

    public UUID getPositionId() { return positionId; }
    public PortfolioEntity getPortfolio() { return portfolio; }
    public InstrumentEntity getInstrument() { return instrument; }
    public BigDecimal getQuantity() { return quantity; }
    public BigDecimal getAverageCostAmount() { return averageCostAmount; }
    public String getAverageCostCurrency() { return averageCostCurrency; }
    public BigDecimal getCurrentPriceAmount() { return currentPriceAmount; }
    public String getCurrentPriceCurrency() { return currentPriceCurrency; }
    public BrokerAccountEntity getBrokerAccount() { return brokerAccount; }
    public Instant getLastUpdated() { return lastUpdated; }
}
