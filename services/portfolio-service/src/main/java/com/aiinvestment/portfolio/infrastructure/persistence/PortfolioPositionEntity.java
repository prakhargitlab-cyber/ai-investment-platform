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
    @Column(name = "current_price_amount")
    private BigDecimal currentPriceAmount;
    @Column(name = "current_price_currency")
    private String currentPriceCurrency;
    @Column(name = "imported_price_amount")
    private BigDecimal importedPriceAmount;
    @Column(name = "imported_price_currency")
    private String importedPriceCurrency;
    @Column(name = "imported_market_value_amount")
    private BigDecimal importedMarketValueAmount;
    @Column(name = "imported_market_value_currency")
    private String importedMarketValueCurrency;
    @Column(name = "imported_unrealized_pnl_amount")
    private BigDecimal importedUnrealizedPnlAmount;
    @Column(name = "imported_unrealized_pnl_currency")
    private String importedUnrealizedPnlCurrency;
    @Column(name = "imported_unrealized_pnl_percent")
    private BigDecimal importedUnrealizedPnlPercent;
    @Column(name = "market_value_amount")
    private BigDecimal marketValueAmount;
    @Column(name = "market_value_currency")
    private String marketValueCurrency;
    @Column(name = "unrealized_profit_loss_amount")
    private BigDecimal unrealizedProfitLossAmount;
    @Column(name = "unrealized_profit_loss_currency")
    private String unrealizedProfitLossCurrency;
    @ManyToOne(optional = false)
    @JoinColumn(name = "broker_account_id")
    private BrokerAccountEntity brokerAccount;
    @Column(name = "source_type", nullable = false)
    private String sourceType = "BROKER";
    @Column(name = "source_connection_id")
    private UUID sourceConnectionId;
    @Column(name = "source_broker_type")
    private String sourceBrokerType;
    @Column(name = "source_broker_account_id")
    private String sourceBrokerAccountId;
    @Column(name = "external_instrument_provider")
    private String externalInstrumentProvider;
    @Column(name = "external_instrument_id")
    private String externalInstrumentId;
    @Column(name = "observed_at")
    private Instant observedAt;
    @Column(name = "sync_generation_id")
    private UUID syncGenerationId;
    @Column(nullable = false)
    private boolean active = true;
    @Column(name = "data_freshness", nullable = false)
    private String dataFreshness = "DEMO";
    @Column(name = "last_updated", nullable = false)
    private Instant lastUpdated;
    @Column(name = "custom_display_name", length = 160)
    private String customDisplayName;

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
        this.sourceBrokerType = brokerAccount == null ? null : brokerAccount.getBrokerType().name();
        this.sourceBrokerAccountId = brokerAccount == null ? null : brokerAccount.getSourceBrokerAccountId();
        this.externalInstrumentProvider = instrument == null ? null : instrument.getProvider();
        this.externalInstrumentId = instrument == null ? null : instrument.getProviderInstrumentId();
        this.observedAt = lastUpdated;
        this.lastUpdated = lastUpdated;
    }

    public PortfolioPositionEntity(UUID positionId, PortfolioEntity portfolio, InstrumentEntity instrument, BigDecimal quantity,
                                   BigDecimal averageCostAmount, String averageCostCurrency, BigDecimal currentPriceAmount,
                                   String currentPriceCurrency, BrokerAccountEntity brokerAccount, String dataFreshness,
                                   Instant lastUpdated) {
        this(positionId, portfolio, instrument, quantity, averageCostAmount, averageCostCurrency, currentPriceAmount,
                currentPriceCurrency, brokerAccount, lastUpdated);
        this.dataFreshness = dataFreshness == null || dataFreshness.isBlank() ? "DEMO" : dataFreshness;
    }

    public PortfolioPositionEntity(UUID positionId, PortfolioEntity portfolio, InstrumentEntity instrument, BigDecimal quantity,
                                   BigDecimal averageCostAmount, String averageCostCurrency, BigDecimal currentPriceAmount,
                                   String currentPriceCurrency, BigDecimal marketValueAmount, String marketValueCurrency,
                                   BigDecimal unrealizedProfitLossAmount, String unrealizedProfitLossCurrency,
                                   BrokerAccountEntity brokerAccount, String dataFreshness, Instant lastUpdated) {
        this(positionId, portfolio, instrument, quantity, averageCostAmount, averageCostCurrency, currentPriceAmount,
                currentPriceCurrency, brokerAccount, dataFreshness, lastUpdated);
        this.marketValueAmount = marketValueAmount;
        this.marketValueCurrency = marketValueCurrency;
        this.unrealizedProfitLossAmount = unrealizedProfitLossAmount;
        this.unrealizedProfitLossCurrency = unrealizedProfitLossCurrency;
    }

    public PortfolioPositionEntity(UUID positionId, PortfolioEntity portfolio, InstrumentEntity instrument, BigDecimal quantity,
                                   BigDecimal averageCostAmount, String averageCostCurrency, BigDecimal currentPriceAmount,
                                   String currentPriceCurrency, BigDecimal marketValueAmount, String marketValueCurrency,
                                   BigDecimal unrealizedProfitLossAmount, String unrealizedProfitLossCurrency,
                                   BrokerAccountEntity brokerAccount, String sourceType, UUID sourceConnectionId,
                                   String sourceBrokerType, String sourceBrokerAccountId, String externalInstrumentProvider,
                                   String externalInstrumentId, Instant observedAt, UUID syncGenerationId,
                                   boolean active, String dataFreshness, Instant lastUpdated) {
        this(positionId, portfolio, instrument, quantity, averageCostAmount, averageCostCurrency, currentPriceAmount,
                currentPriceCurrency, marketValueAmount, marketValueCurrency, unrealizedProfitLossAmount,
                unrealizedProfitLossCurrency, brokerAccount, dataFreshness, lastUpdated);
        this.sourceType = sourceType == null || sourceType.isBlank() ? "BROKER" : sourceType;
        this.sourceConnectionId = sourceConnectionId;
        this.sourceBrokerType = sourceBrokerType;
        this.sourceBrokerAccountId = sourceBrokerAccountId;
        this.externalInstrumentProvider = externalInstrumentProvider;
        this.externalInstrumentId = externalInstrumentId;
        this.observedAt = observedAt;
        this.syncGenerationId = syncGenerationId;
        this.active = active;
    }

    public UUID getPositionId() { return positionId; }
    public PortfolioEntity getPortfolio() { return portfolio; }
    public InstrumentEntity getInstrument() { return instrument; }
    public BigDecimal getQuantity() { return quantity; }
    public BigDecimal getAverageCostAmount() { return averageCostAmount; }
    public String getAverageCostCurrency() { return averageCostCurrency; }
    public BigDecimal getCurrentPriceAmount() { return currentPriceAmount; }
    public String getCurrentPriceCurrency() { return currentPriceCurrency; }
    public BigDecimal getImportedPriceAmount() { return importedPriceAmount; }
    public String getImportedPriceCurrency() { return importedPriceCurrency; }
    public BigDecimal getImportedMarketValueAmount() { return importedMarketValueAmount; }
    public String getImportedMarketValueCurrency() { return importedMarketValueCurrency; }
    public BigDecimal getImportedUnrealizedPnlAmount() { return importedUnrealizedPnlAmount; }
    public String getImportedUnrealizedPnlCurrency() { return importedUnrealizedPnlCurrency; }
    public BigDecimal getImportedUnrealizedPnlPercent() { return importedUnrealizedPnlPercent; }
    public BigDecimal getMarketValueAmount() { return marketValueAmount; }
    public String getMarketValueCurrency() { return marketValueCurrency; }
    public BigDecimal getUnrealizedProfitLossAmount() { return unrealizedProfitLossAmount; }
    public String getUnrealizedProfitLossCurrency() { return unrealizedProfitLossCurrency; }
    public BrokerAccountEntity getBrokerAccount() { return brokerAccount; }
    public String getSourceType() { return sourceType; }
    public UUID getSourceConnectionId() { return sourceConnectionId; }
    public String getSourceBrokerType() { return sourceBrokerType; }
    public String getSourceBrokerAccountId() { return sourceBrokerAccountId; }
    public String getExternalInstrumentProvider() { return externalInstrumentProvider; }
    public String getExternalInstrumentId() { return externalInstrumentId; }
    public Instant getObservedAt() { return observedAt; }
    public UUID getSyncGenerationId() { return syncGenerationId; }
    public boolean isActive() { return active; }
    public String getDataFreshness() { return dataFreshness; }
    public Instant getLastUpdated() { return lastUpdated; }
    public String getCustomDisplayName() { return customDisplayName; }

    public void setCustomDisplayName(String customDisplayName) {
        this.customDisplayName = customDisplayName;
    }

    public void replaceBrokerValues(InstrumentEntity instrument, BigDecimal quantity, BigDecimal averageCostAmount,
                                    String averageCostCurrency, BigDecimal currentPriceAmount, String currentPriceCurrency,
                                    BigDecimal marketValueAmount, String marketValueCurrency,
                                    BigDecimal unrealizedProfitLossAmount, String unrealizedProfitLossCurrency,
                                    UUID syncGenerationId, Instant observedAt, String dataFreshness) {
        this.instrument = instrument;
        this.quantity = quantity;
        this.averageCostAmount = averageCostAmount;
        this.averageCostCurrency = averageCostCurrency;
        this.currentPriceAmount = currentPriceAmount;
        this.currentPriceCurrency = currentPriceCurrency;
        this.marketValueAmount = marketValueAmount;
        this.marketValueCurrency = marketValueCurrency;
        this.unrealizedProfitLossAmount = unrealizedProfitLossAmount;
        this.unrealizedProfitLossCurrency = unrealizedProfitLossCurrency;
        this.externalInstrumentProvider = instrument == null ? null : instrument.getProvider();
        this.externalInstrumentId = instrument == null ? null : instrument.getProviderInstrumentId();
        this.syncGenerationId = syncGenerationId;
        this.observedAt = observedAt;
        this.dataFreshness = dataFreshness == null || dataFreshness.isBlank() ? "REAL_BROKER" : dataFreshness;
        this.active = true;
        this.lastUpdated = Instant.now();
    }

    public void replaceManualImportValues(InstrumentEntity instrument, BigDecimal quantity,
                                          BigDecimal averageCostAmount, String currency,
                                          BigDecimal importedPriceAmount,
                                          BigDecimal importedMarketValueAmount,
                                          BigDecimal importedUnrealizedPnlAmount,
                                          BigDecimal importedUnrealizedPnlPercent,
                                          UUID syncGenerationId, Instant observedAt) {
        this.instrument = instrument;
        this.quantity = quantity;
        this.averageCostAmount = averageCostAmount;
        this.averageCostCurrency = currency;
        this.currentPriceAmount = null;
        this.currentPriceCurrency = null;
        this.marketValueAmount = null;
        this.marketValueCurrency = null;
        this.unrealizedProfitLossAmount = null;
        this.unrealizedProfitLossCurrency = null;
        this.importedPriceAmount = positiveOrNull(importedPriceAmount);
        this.importedPriceCurrency = this.importedPriceAmount == null ? null : currency;
        this.importedMarketValueAmount = importedMarketValueAmount;
        this.importedMarketValueCurrency = importedMarketValueAmount == null ? null : currency;
        this.importedUnrealizedPnlAmount = importedUnrealizedPnlAmount;
        this.importedUnrealizedPnlCurrency = importedUnrealizedPnlAmount == null ? null : currency;
        this.importedUnrealizedPnlPercent = importedUnrealizedPnlPercent;
        this.externalInstrumentProvider = instrument == null ? null : instrument.getProvider();
        this.externalInstrumentId = instrument == null ? null : instrument.getProviderInstrumentId();
        this.syncGenerationId = syncGenerationId;
        this.observedAt = observedAt;
        this.dataFreshness = "IMPORTED_SNAPSHOT";
        this.active = true;
        this.lastUpdated = Instant.now();
    }

    private static BigDecimal positiveOrNull(BigDecimal value) {
        return value != null && value.signum() > 0 ? value : null;
    }

    public void adoptBrokerSource(UUID sourceConnectionId, String sourceBrokerType, String sourceBrokerAccountId,
                                  String externalInstrumentProvider, String externalInstrumentId,
                                  BrokerAccountEntity brokerAccount) {
        this.sourceType = "BROKER";
        this.sourceConnectionId = sourceConnectionId;
        this.sourceBrokerType = sourceBrokerType;
        this.sourceBrokerAccountId = sourceBrokerAccountId;
        this.externalInstrumentProvider = externalInstrumentProvider;
        this.externalInstrumentId = externalInstrumentId;
        this.brokerAccount = brokerAccount;
    }

    public void markStale(UUID syncGenerationId) {
        this.active = false;
        this.syncGenerationId = syncGenerationId;
        this.dataFreshness = "STALE_BROKER";
        this.lastUpdated = Instant.now();
    }
}
