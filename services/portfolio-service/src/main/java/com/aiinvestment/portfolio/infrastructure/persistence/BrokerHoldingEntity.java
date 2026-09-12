package com.aiinvestment.portfolio.infrastructure.persistence;

import jakarta.persistence.*;
import java.math.BigDecimal;
import java.time.Instant;
import java.util.UUID;

@Entity
@Table(name="broker_holdings")
public class BrokerHoldingEntity {
    @Id @Column(name="holding_id") private UUID holdingId;
    @Column(name="user_id",nullable=false) private UUID userId;
    @ManyToOne(optional=false) @JoinColumn(name="portfolio_id") private PortfolioEntity portfolio;
    @Column(name="connection_id",nullable=false) private UUID connectionId;
    @Column(name="broker_account_id",nullable=false) private String brokerAccountId;
    @Column(nullable=false) private String provider;
    @Column(name="provider_instrument_id",nullable=false) private String providerInstrumentId;
    private String isin;
    private String symbol;
    @Column(nullable=false) private BigDecimal quantity;
    @Column(name="average_cost_amount") private BigDecimal averageCostAmount;
    @Column(name="current_price_amount") private BigDecimal currentPriceAmount;
    @Column(name="market_value_amount") private BigDecimal marketValueAmount;
    private String currency;
    private String exchange;
    @Column(name="valuation_completeness",nullable=false) private String valuationCompleteness;
    @Column(nullable=false) private boolean active;
    @Column(name="observed_at",nullable=false) private Instant observedAt;
    @Column(name="sync_generation_id",nullable=false) private UUID syncGenerationId;
    @Column(name="created_at",nullable=false) private Instant createdAt;
    @Column(name="updated_at",nullable=false) private Instant updatedAt;
    protected BrokerHoldingEntity() {}
    public BrokerHoldingEntity(UUID holdingId, UUID userId, PortfolioEntity portfolio, UUID connectionId,
            String brokerAccountId, String provider, String providerInstrumentId, String isin, String symbol,
            BigDecimal quantity, BigDecimal averageCostAmount, BigDecimal currentPriceAmount,
            BigDecimal marketValueAmount, String currency, String exchange, Instant observedAt, UUID generation) {
        this.holdingId=holdingId; this.userId=userId; this.portfolio=portfolio; this.connectionId=connectionId;
        this.brokerAccountId=brokerAccountId; this.provider=provider; this.providerInstrumentId=providerInstrumentId;
        this.createdAt=observedAt; replace(isin,symbol,quantity,averageCostAmount,currentPriceAmount,marketValueAmount,currency,exchange,observedAt,generation);
    }
    public void replace(String isin,String symbol,BigDecimal quantity,BigDecimal average,BigDecimal current,
                        BigDecimal market,String currency,String exchange,Instant observed,UUID generation){
        this.isin=isin;this.symbol=symbol;this.quantity=quantity;this.averageCostAmount=average;this.currentPriceAmount=current;
        this.marketValueAmount=market;this.currency=currency;this.exchange=exchange;this.observedAt=observed;
        this.syncGenerationId=generation;this.active=true;this.valuationCompleteness="INCOMPLETE";this.updatedAt=observed;
    }
    public void markStale(UUID generation){ active=false;syncGenerationId=generation;updatedAt=Instant.now(); }
    public String getProviderInstrumentId(){return providerInstrumentId;}
}
