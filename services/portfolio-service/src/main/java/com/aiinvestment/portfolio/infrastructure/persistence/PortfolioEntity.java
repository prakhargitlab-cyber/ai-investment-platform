package com.aiinvestment.portfolio.infrastructure.persistence;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;

import java.time.Instant;
import java.util.UUID;
import com.aiinvestment.shared.domain.broker.BrokerType;
import jakarta.persistence.EnumType;
import jakarta.persistence.Enumerated;

@Entity
@Table(name = "portfolios")
public class PortfolioEntity {
    @Id
    @Column(name = "portfolio_id", nullable = false)
    private UUID portfolioId;
    @Column(name = "user_id", nullable = false)
    private UUID userId;
    @Column(nullable = false)
    private String name;
    @Column(name = "base_currency", nullable = false)
    private String baseCurrency;
    @Column(name = "created_at", nullable = false)
    private Instant createdAt;
    @Column(name = "updated_at", nullable = false)
    private Instant updatedAt;
    @Column(name = "broker_connection_id")
    private UUID brokerConnectionId;
    @Column(name = "broker_account_id")
    private String brokerAccountId;
    @Enumerated(EnumType.STRING)
    @Column(name = "broker_provider")
    private BrokerType brokerProvider;
    @Column(name = "last_successful_broker_sync_at")
    private Instant lastSuccessfulBrokerSyncAt;
    @Column(name = "last_broker_sync_attempt_at")
    private Instant lastBrokerSyncAttemptAt;
    @Column(name = "last_broker_sync_error_code")
    private String lastBrokerSyncErrorCode;
    @Column(name = "acquisition_source")
    private String acquisitionSource;
    @Column(name = "source_account_reference")
    private String sourceAccountReference;
    @Column(name = "last_imported_at")
    private Instant lastImportedAt;
    @Column(name = "last_imported_filename")
    private String lastImportedFilename;

    protected PortfolioEntity() {
    }

    public PortfolioEntity(UUID portfolioId, UUID userId, String name, String baseCurrency, Instant createdAt, Instant updatedAt) {
        this.portfolioId = portfolioId;
        this.userId = userId;
        this.name = name;
        this.baseCurrency = baseCurrency;
        this.createdAt = createdAt;
        this.updatedAt = updatedAt;
    }

    public UUID getPortfolioId() { return portfolioId; }
    public UUID getUserId() { return userId; }
    public String getName() { return name; }
    public String getBaseCurrency() { return baseCurrency; }
    public Instant getCreatedAt() { return createdAt; }
    public Instant getUpdatedAt() { return updatedAt; }
    public UUID getBrokerConnectionId() { return brokerConnectionId; }
    public String getBrokerAccountId() { return brokerAccountId; }
    public BrokerType getBrokerProvider() { return brokerProvider; }
    public Instant getLastSuccessfulBrokerSyncAt() { return lastSuccessfulBrokerSyncAt; }
    public Instant getLastBrokerSyncAttemptAt() { return lastBrokerSyncAttemptAt; }
    public String getLastBrokerSyncErrorCode() { return lastBrokerSyncErrorCode; }
    public String getAcquisitionSource() { return acquisitionSource; }
    public String getSourceAccountReference() { return sourceAccountReference; }
    public Instant getLastImportedAt() { return lastImportedAt; }
    public String getLastImportedFilename() { return lastImportedFilename; }

    public void attachManualImportSource(BrokerType provider, String accountReference, Instant importedAt,
                                         String filename) {
        this.brokerProvider = java.util.Objects.requireNonNull(provider);
        this.acquisitionSource = "MANUAL_CSV_IMPORT";
        this.sourceAccountReference = accountReference;
        this.lastImportedAt = importedAt;
        this.lastImportedFilename = filename;
        this.updatedAt = importedAt;
    }

    public void setBaseCurrency(String baseCurrency) {
        if (baseCurrency == null || !baseCurrency.matches("[A-Z]{3}")) {
            throw new IllegalArgumentException("baseCurrency must be a 3-letter ISO currency code");
        }
        this.baseCurrency = baseCurrency;
    }

    public void setUpdatedAt(Instant updatedAt) {
        this.updatedAt = updatedAt;
    }

    public void attachBrokerSource(UUID connectionId, String accountId, BrokerType provider) {
        this.brokerConnectionId = java.util.Objects.requireNonNull(connectionId);
        if (accountId == null || accountId.isBlank()) {
            throw new IllegalArgumentException("broker account id is required");
        }
        this.brokerAccountId = accountId;
        this.brokerProvider = java.util.Objects.requireNonNull(provider);
    }

    public void recordBrokerSyncAttempt(Instant at) {
        this.lastBrokerSyncAttemptAt = at;
    }

    public void recordBrokerSyncSuccess(Instant at) {
        this.lastSuccessfulBrokerSyncAt = at;
        this.lastBrokerSyncAttemptAt = at;
        this.lastBrokerSyncErrorCode = null;
        this.updatedAt = at;
    }

    public void recordBrokerSyncFailure(Instant at, String errorCode) {
        this.lastBrokerSyncAttemptAt = at;
        this.lastBrokerSyncErrorCode = errorCode;
    }
}
