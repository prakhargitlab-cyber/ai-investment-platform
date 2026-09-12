package com.aiinvestment.portfolio.infrastructure.persistence;

import com.aiinvestment.portfolio.application.BrokerSnapshotClientResponse;
import jakarta.persistence.*;
import java.math.BigDecimal;
import java.time.Instant;
import java.util.UUID;

@Entity
@Table(name = "broker_position_snapshots")
public class BrokerPositionSnapshotEntity {
    @Id @Column(name = "snapshot_position_id") private UUID id;
    @Column(name = "sync_generation_id", nullable = false) private UUID syncGenerationId;
    @Column(name = "user_id", nullable = false) private UUID userId;
    @Column(name = "portfolio_id", nullable = false) private UUID portfolioId;
    @Column(name = "connection_id", nullable = false) private UUID connectionId;
    @Column(name = "broker_account_id", nullable = false) private String brokerAccountId;
    @Column(name = "instrument_provider", nullable = false) private String instrumentProvider;
    @Column(name = "provider_instrument_id", nullable = false) private String providerInstrumentId;
    @Column private String isin;
    @Column private String ticker;
    @Column(nullable = false) private BigDecimal quantity;
    @Column(name = "average_cost_amount") private BigDecimal averageCostAmount;
    @Column(name = "average_cost_currency") private String averageCostCurrency;
    @Column(name = "current_price_amount") private BigDecimal currentPriceAmount;
    @Column(name = "current_price_currency") private String currentPriceCurrency;
    @Column(name = "market_value_amount") private BigDecimal marketValueAmount;
    @Column(name = "market_value_currency") private String marketValueCurrency;
    @Column(name = "unrealized_pnl_amount") private BigDecimal unrealizedPnlAmount;
    @Column(name = "unrealized_pnl_currency") private String unrealizedPnlCurrency;
    @Column(name = "effective_at", nullable = false) private Instant effectiveAt;
    @Column(name = "created_at", nullable = false) private Instant createdAt;
    @Column(nullable = false) private String source;

    protected BrokerPositionSnapshotEntity() {}

    public BrokerPositionSnapshotEntity(UUID id, UUID syncGenerationId, UUID userId, UUID portfolioId,
                                        UUID connectionId, String brokerAccountId,
                                        BrokerSnapshotClientResponse.Position position,
                                        Instant effectiveAt, Instant createdAt) {
        var instrument = position.instrument();
        this.id = id; this.syncGenerationId = syncGenerationId; this.userId = userId;
        this.portfolioId = portfolioId; this.connectionId = connectionId; this.brokerAccountId = brokerAccountId;
        this.instrumentProvider = instrument.provider(); this.providerInstrumentId = instrument.providerInstrumentId();
        this.isin = instrument.isin(); this.ticker = instrument.ticker(); this.quantity = position.quantity();
        this.averageCostAmount = position.averageCost() == null ? null : position.averageCost().amount();
        this.averageCostCurrency = position.averageCost() == null ? null : position.averageCost().currency();
        this.currentPriceAmount = position.currentPrice() == null ? null : position.currentPrice().amount();
        this.currentPriceCurrency = position.currentPrice() == null ? null : position.currentPrice().currency();
        this.marketValueAmount = position.marketValue() == null ? null : position.marketValue().amount();
        this.marketValueCurrency = position.marketValue() == null ? null : position.marketValue().currency();
        this.unrealizedPnlAmount = position.unrealizedProfitLoss() == null ? null : position.unrealizedProfitLoss().amount();
        this.unrealizedPnlCurrency = position.unrealizedProfitLoss() == null ? null : position.unrealizedProfitLoss().currency();
        this.effectiveAt = effectiveAt; this.createdAt = createdAt; this.source = "REAL_BROKER";
    }

    public BigDecimal getQuantity() { return quantity; }
    public UUID getSyncGenerationId() { return syncGenerationId; }
    public String getProviderInstrumentId() { return providerInstrumentId; }
}
