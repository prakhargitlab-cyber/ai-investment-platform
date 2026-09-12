package com.aiinvestment.broker.persistence;

import com.aiinvestment.shared.domain.broker.BrokerConnectionState;
import com.aiinvestment.shared.domain.broker.BrokerType;
import jakarta.persistence.*;

import java.time.Instant;
import java.util.UUID;

@Entity
@Table(name = "broker_connections")
public class BrokerConnectionEntity {
    @Id
    @Column(name = "connection_id", nullable = false)
    private UUID connectionId;
    @Column(name = "user_id", nullable = false)
    private UUID userId;
    @Enumerated(EnumType.STRING)
    @Column(name = "broker_type", nullable = false)
    private BrokerType brokerType;
    @Column(name = "external_account_reference")
    private String externalAccountReference;
    @Column(name = "display_name", nullable = false)
    private String displayName;
    @Enumerated(EnumType.STRING)
    @Column(nullable = false)
    private BrokerConnectionState status;
    @Column(name = "account_currency")
    private String accountCurrency;
    @Column(name = "provider_status")
    private String providerStatus;
    @Column(name = "data_freshness")
    private String dataFreshness;
    @Column(name = "session_reference")
    private String sessionReference;
    @Column(name = "capabilities")
    private String capabilities;
    @Column(name = "connector_id")
    private UUID connectorId;
    @Column(name = "connected_at")
    private Instant connectedAt;
    @Column(name = "last_successful_sync_at")
    private Instant lastSuccessfulSyncAt;
    @Column(name = "last_sync_attempt_at")
    private Instant lastSyncAttemptAt;
    @Column(name = "last_error_code")
    private String lastErrorCode;
    @Column(name = "created_at", nullable = false)
    private Instant createdAt;
    @Column(name = "updated_at", nullable = false)
    private Instant updatedAt;

    protected BrokerConnectionEntity() {
    }

    public BrokerConnectionEntity(UUID connectionId, UUID userId, BrokerType brokerType, String externalAccountReference,
                                  String displayName, BrokerConnectionState status, Instant connectedAt,
                                  Instant lastSuccessfulSyncAt, Instant lastSyncAttemptAt, String lastErrorCode,
                                  Instant createdAt, Instant updatedAt) {
        this.connectionId = connectionId;
        this.userId = userId;
        this.brokerType = brokerType;
        this.externalAccountReference = externalAccountReference;
        this.displayName = displayName;
        this.status = status;
        this.connectedAt = connectedAt;
        this.lastSuccessfulSyncAt = lastSuccessfulSyncAt;
        this.lastSyncAttemptAt = lastSyncAttemptAt;
        this.lastErrorCode = lastErrorCode;
        this.createdAt = createdAt;
        this.updatedAt = updatedAt;
    }

    public BrokerConnectionEntity(UUID connectionId, UUID userId, BrokerType brokerType, String externalAccountReference,
                                  String displayName, BrokerConnectionState status, String accountCurrency,
                                  String providerStatus, String dataFreshness, String sessionReference, String capabilities,
                                  Instant connectedAt, Instant lastSuccessfulSyncAt, Instant lastSyncAttemptAt,
                                  String lastErrorCode, Instant createdAt, Instant updatedAt) {
        this(connectionId, userId, brokerType, externalAccountReference, displayName, status, connectedAt,
                lastSuccessfulSyncAt, lastSyncAttemptAt, lastErrorCode, createdAt, updatedAt);
        this.accountCurrency = accountCurrency;
        this.providerStatus = providerStatus;
        this.dataFreshness = dataFreshness;
        this.sessionReference = sessionReference;
        this.capabilities = capabilities;
    }

    public BrokerConnectionEntity(UUID connectionId, UUID userId, BrokerType brokerType, UUID connectorId,
                                  String externalAccountReference, String displayName, BrokerConnectionState status,
                                  String accountCurrency, String providerStatus, String dataFreshness,
                                  String sessionReference, String capabilities, Instant connectedAt,
                                  Instant lastSuccessfulSyncAt, Instant lastSyncAttemptAt, String lastErrorCode,
                                  Instant createdAt, Instant updatedAt) {
        this(connectionId, userId, brokerType, externalAccountReference, displayName, status, accountCurrency,
                providerStatus, dataFreshness, sessionReference, capabilities, connectedAt, lastSuccessfulSyncAt,
                lastSyncAttemptAt, lastErrorCode, createdAt, updatedAt);
        this.connectorId = connectorId;
    }

    public UUID getConnectionId() { return connectionId; }
    public UUID getUserId() { return userId; }
    public BrokerType getBrokerType() { return brokerType; }
    public String getExternalAccountReference() { return externalAccountReference; }
    public String getDisplayName() { return displayName; }
    public BrokerConnectionState getStatus() { return status; }
    public String getAccountCurrency() { return accountCurrency; }
    public String getProviderStatus() { return providerStatus; }
    public String getDataFreshness() { return dataFreshness; }
    public String getSessionReference() { return sessionReference; }
    public String getCapabilities() { return capabilities; }
    public UUID getConnectorId() { return connectorId; }
    public Instant getConnectedAt() { return connectedAt; }
    public Instant getLastSuccessfulSyncAt() { return lastSuccessfulSyncAt; }
    public Instant getLastSyncAttemptAt() { return lastSyncAttemptAt; }
    public String getLastErrorCode() { return lastErrorCode; }
    public Instant getCreatedAt() { return createdAt; }
    public Instant getUpdatedAt() { return updatedAt; }
    public void setStatus(BrokerConnectionState status) { this.status = status; }
    public void setExternalAccountReference(String externalAccountReference) { this.externalAccountReference = externalAccountReference; }
    public void setDisplayName(String displayName) { this.displayName = displayName; }
    public void setAccountCurrency(String accountCurrency) { this.accountCurrency = accountCurrency; }
    public void setProviderStatus(String providerStatus) { this.providerStatus = providerStatus; }
    public void setDataFreshness(String dataFreshness) { this.dataFreshness = dataFreshness; }
    public void setSessionReference(String sessionReference) { this.sessionReference = sessionReference; }
    public void setCapabilities(String capabilities) { this.capabilities = capabilities; }
    public void setConnectorId(UUID connectorId) { this.connectorId = connectorId; }
    public void setLastSuccessfulSyncAt(Instant lastSuccessfulSyncAt) { this.lastSuccessfulSyncAt = lastSuccessfulSyncAt; }
    public void setLastSyncAttemptAt(Instant lastSyncAttemptAt) { this.lastSyncAttemptAt = lastSyncAttemptAt; }
    public void setLastErrorCode(String lastErrorCode) { this.lastErrorCode = lastErrorCode; }
    public void setUpdatedAt(Instant updatedAt) { this.updatedAt = updatedAt; }
}
