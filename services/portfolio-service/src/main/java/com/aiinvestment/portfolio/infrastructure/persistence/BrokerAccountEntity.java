package com.aiinvestment.portfolio.infrastructure.persistence;

import com.aiinvestment.shared.domain.broker.BrokerAccountStatus;
import com.aiinvestment.shared.domain.broker.BrokerType;
import jakarta.persistence.*;

import java.util.UUID;

@Entity
@Table(name = "broker_accounts")
public class BrokerAccountEntity {
    @Id
    @Column(name = "broker_account_id", nullable = false)
    private String brokerAccountId;
    @Column(name = "user_id", nullable = false)
    private UUID userId;
    @Column(name = "connection_id")
    private UUID connectionId;
    @Enumerated(EnumType.STRING)
    @Column(name = "broker_type", nullable = false)
    private BrokerType brokerType;
    @Column(name = "source_broker_account_id")
    private String sourceBrokerAccountId;
    @Column(name = "external_account_reference")
    private String externalAccountReference;
    @Column(name = "display_name", nullable = false)
    private String displayName;
    @Column(name = "base_currency", nullable = false)
    private String baseCurrency;
    @Enumerated(EnumType.STRING)
    @Column(nullable = false)
    private BrokerAccountStatus status;

    protected BrokerAccountEntity() {
    }

    public BrokerAccountEntity(String brokerAccountId, UUID userId, BrokerType brokerType, String externalAccountReference,
                               String displayName, String baseCurrency, BrokerAccountStatus status) {
        this(brokerAccountId, userId, null, brokerType, brokerAccountId, externalAccountReference, displayName, baseCurrency, status);
    }

    public BrokerAccountEntity(String brokerAccountId, UUID userId, UUID connectionId, BrokerType brokerType,
                               String sourceBrokerAccountId, String externalAccountReference,
                               String displayName, String baseCurrency, BrokerAccountStatus status) {
        this.brokerAccountId = brokerAccountId;
        this.userId = userId;
        this.connectionId = connectionId;
        this.brokerType = brokerType;
        this.sourceBrokerAccountId = sourceBrokerAccountId == null || sourceBrokerAccountId.isBlank()
                ? brokerAccountId
                : sourceBrokerAccountId;
        this.externalAccountReference = externalAccountReference;
        this.displayName = displayName;
        this.baseCurrency = baseCurrency;
        this.status = status;
    }

    public String getBrokerAccountId() { return brokerAccountId; }
    public UUID getUserId() { return userId; }
    public UUID getConnectionId() { return connectionId; }
    public BrokerType getBrokerType() { return brokerType; }
    public String getSourceBrokerAccountId() { return sourceBrokerAccountId; }
    public String getExternalAccountReference() { return externalAccountReference; }
    public String getDisplayName() { return displayName; }
    public String getBaseCurrency() { return baseCurrency; }
    public BrokerAccountStatus getStatus() { return status; }

    public void adoptConnection(UUID connectionId, BrokerType brokerType, String sourceBrokerAccountId,
                                String externalAccountReference, String displayName, String baseCurrency,
                                BrokerAccountStatus status) {
        this.connectionId = connectionId;
        refreshBrokerMetadata(brokerType, sourceBrokerAccountId, externalAccountReference, displayName, baseCurrency,
                status);
    }

    public void refreshBrokerMetadata(BrokerType brokerType, String sourceBrokerAccountId,
                                      String externalAccountReference, String displayName, String baseCurrency,
                                      BrokerAccountStatus status) {
        this.brokerType = brokerType;
        this.sourceBrokerAccountId = sourceBrokerAccountId == null || sourceBrokerAccountId.isBlank()
                ? this.brokerAccountId
                : sourceBrokerAccountId;
        this.externalAccountReference = externalAccountReference;
        this.displayName = displayName;
        this.baseCurrency = baseCurrency;
        this.status = status;
    }
}
